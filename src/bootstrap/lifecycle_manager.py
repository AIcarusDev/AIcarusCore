# src/bootstrap/lifecycle_manager.py
import asyncio
import contextlib
import json
import os
import time
from dataclasses import dataclass
from enum import Enum, auto
from typing import TYPE_CHECKING, Literal

from src.common.custom_logging.logging_config import get_logger
from src.common.time_utils import format_relative_time_for_context
from src.config.config_paths import LIFECYCLE_STATE_FILE_PATH

if TYPE_CHECKING:
    from src.bootstrap.container import ServiceContainer

logger = get_logger(__name__)

# 定义状态文件的路径和心跳间隔
STATE_FILE_PATH = LIFECYCLE_STATE_FILE_PATH / ".running.json"
HEARTBEAT_INTERVAL_SECONDS = 60.0


class ShutdownType(Enum):
    """定义上一次关闭的类型."""
    CLEAN = auto()
    CRASH = auto()


@dataclass
class LifecycleEvent:
    """封装启动时检测到的生命周期事件信息."""
    shutdown_type: ShutdownType
    time_since_shutdown: int  # in seconds
    was_os_connected: bool

    def generate_meta_info(self) -> str:
        """根据事件信息生成给LLM的提示."""
        time_ago_str = format_relative_time_for_context(
            (int(time.time()) - self.time_since_shutdown) * 1000,
            int(time.time() * 1000)
        )

        if self.shutdown_type == ShutdownType.CLEAN:
            base_message = f"你的意识在约 {time_ago_str} 前被外部中断。"
        else:  # CRASH
            base_message = f"你的意识在约 {time_ago_str} 前因意外中断。"

        if self.was_os_connected:
            connection_message = "与OS的连接因此中断。你可以发送指令重新连接。"
            full_message = f"{base_message} {connection_message}"
        else:
            full_message = base_message

        return f"<meta_info>{full_message}</meta_info>"


class LifecycleManager:
    """管理应用生命周期、心跳和中断恢复状态."""
    _State = Literal["initializing", "running", "shutting_down", "clean_exit"]

    def __init__(self, container: "ServiceContainer") -> None:
        self.container = container
        self._pid = os.getpid()
        self._heartbeat_task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()

    def start(self) -> str:
        """在应用启动时调用.

        分析上一次的状态，写入新状态，并启动心跳。
        返回给LLM的meta_info。
        """
        previous_state = self._read_state_file()
        event = self._analyze_previous_state(previous_state)
        meta_info = event.generate_meta_info() if event else ""

        self._write_state_file("running")
        self._heartbeat_task = asyncio.create_task(self._heartbeat_writer())

        logger.info("LifecycleManager已启动，心跳任务已部署。")
        return meta_info

    async def stop(self) -> None:
        """在应用关闭时调用，确保写入干净的退出状态."""
        logger.info("LifecycleManager正在停止...")
        self._stop_event.set()
        if self._heartbeat_task:
            with contextlib.suppress(asyncio.CancelledError):
                await self._heartbeat_task
        self._write_state_file("clean_exit")
        logger.info("生命周期状态已标记为干净退出。")

    def _read_state_file(self) -> dict | None:
        if not STATE_FILE_PATH.exists():
            return None
        try:
            with open(STATE_FILE_PATH, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"读取状态文件失败: {e}，将视为首次启动。")
            return None

    def _analyze_previous_state(self, state: dict | None) -> LifecycleEvent | None:
        if not state or "status" not in state or "last_heartbeat" not in state:
            logger.info("未找到有效的先前状态文件，视为首次或全新启动。")
            return None

        status = state.get("status")
        last_heartbeat = state.get("last_heartbeat", 0)
        was_os_connected = state.get("os_connection_status", False)
        time_since_shutdown = int(time.time() - last_heartbeat)

        if status == "clean_exit":
            logger.info("检测到上一次为正常关闭。")
            return LifecycleEvent(ShutdownType.CLEAN, time_since_shutdown, was_os_connected)
        else:
            logger.warning(f"检测到上次可能为异常关闭 (状态: {status})。")
            return LifecycleEvent(ShutdownType.CRASH, time_since_shutdown, was_os_connected)

    def _write_state_file(self, status: _State) -> None:
        try:
            is_connected = (
                self.container.aicos_state_generator.is_connected
                if self.container and hasattr(self.container, 'aicos_state_generator')
                else False
            )
            state_data = {
                "status": status,
                "pid": self._pid,
                "last_heartbeat": time.time(),
                "os_connection_status": is_connected
            }
            with open(STATE_FILE_PATH, "w", encoding="utf-8") as f:
                json.dump(state_data, f, indent=2)
        except OSError as e:
            logger.error(f"写入状态文件失败: {e}")

    async def _heartbeat_writer(self) -> None:
        """后台任务，定期更新状态文件的心跳."""
        while not self._stop_event.is_set():
            try:
                await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
                if self._stop_event.is_set():
                    break
                self._write_state_file("running")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"心跳任务发生错误: {e}", exc_info=True)
