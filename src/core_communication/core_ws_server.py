# src/core_communication/core_ws_server.py
import asyncio
import json
import time
import uuid
from contextlib import suppress
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.action.action_handler import ActionHandler

import websockets
from aicarus_protocols import ConversationInfo, SegBuilder
from aicarus_protocols import Event as ProtocolEvent
from websockets.exceptions import ConnectionClosed, ConnectionClosedError, ConnectionClosedOK
from websockets.server import WebSocketServerProtocol

from src.common.custom_logging.logger_manager import get_logger
from src.config import config
from src.core_communication.action_sender import ActionSender
from src.core_communication.event_receiver import EventReceiver
from src.database.services.event_storage_service import EventStorageService

logger = get_logger("AIcarusCore.ws_server")


class CoreWebsocketServer:
    """
    纯粹的WebSocket服务器，负责管理服务器生命周期和底层连接。
    它将事件处理和动作发送的职责委托给 EventReceiver 和 ActionSender。
    """

    HEARTBEAT_CLIENT_INTERVAL_SECONDS = 30
    HEARTBEAT_SERVER_TIMEOUT_SECONDS = 90
    HEARTBEAT_SERVER_CHECK_INTERVAL_SECONDS = 15

    def __init__(
        self,
        host: str,
        port: int,
        event_receiver: EventReceiver,
        action_sender: ActionSender,
        event_storage_service: EventStorageService,
        action_handler_instance: "ActionHandler",
    ) -> None:
        self.host: str = host
        self.port: int = port
        self.server: websockets.WebSocketServer | None = None
        self.event_storage_service = event_storage_service
        self.event_receiver = event_receiver
        self.action_sender = action_sender
        self.action_handler_instance = action_handler_instance

        # 连接状态信息由 CoreWebsocketServer 统一管理
        self.adapter_clients_info: dict[str, dict[str, Any]] = {}
        self._websocket_to_adapter_id: dict[WebSocketServerProtocol, str] = {}

        self._stop_event: asyncio.Event = asyncio.Event()
        self._heartbeat_check_task: asyncio.Task | None = None

    async def _generate_and_store_system_event(
        self, adapter_id: str, display_name: str, event_type: str, reason: str = ""
    ) -> None:
        """生成并存储系统生命周期事件。"""
        current_timestamp = time.time()
        event_content_text = ""
        if event_type == "meta.lifecycle.adapter_connected":
            event_content_text = f"[状态] {display_name}({adapter_id})连接成功"
        elif event_type == "meta.lifecycle.adapter_disconnected":
            event_content_text = f"[状态] {display_name}({adapter_id})断开({reason})"
        else:
            logger.warning(f"尝试生成未知的系统级生命周期事件类型: {event_type} for adapter {adapter_id}")
            return

        system_event = ProtocolEvent(
            event_id=f"core_event_{adapter_id}_{event_type.split('.')[-1]}_{int(current_timestamp)}_{uuid.uuid4().hex[:6]}",
            event_type=event_type,
            platform="core_system",
            bot_id=config.persona.bot_name,
            time=int(current_timestamp * 1000),
            content=[SegBuilder.text(event_content_text)],
            conversation_info=ConversationInfo(conversation_id="system_events", type="system"),
        )

        if self.event_storage_service:
            try:
                await self.event_storage_service.save_event_document(system_event.to_dict())
                logger.info(f"已生成并存储系统事件: {event_content_text}")
            except Exception as e:
                logger.error(f"存储系统事件 for '{adapter_id}' (type: {event_type}) 失败: {e}", exc_info=True)
        else:
            logger.warning(f"EventStorageService 未初始化，无法存储系统事件 for '{adapter_id}'.")

    async def _register_adapter(self, adapter_id: str, display_name: str, websocket: WebSocketServerProtocol) -> None:
        """注册一个新的适配器，并通知 ActionSender。"""
        current_timestamp = time.time()
        self._websocket_to_adapter_id[websocket] = adapter_id
        self.adapter_clients_info[adapter_id] = {
            "websocket": websocket,
            "last_heartbeat": current_timestamp,
            "display_name": display_name,
        }
        # 通知 ActionSender
        self.action_sender.register_adapter(adapter_id, display_name, websocket)
        logger.info(
            f"适配器 '{display_name}({adapter_id})' 已连接: {websocket.remote_address}. 当前连接数: {len(self.adapter_clients_info)}"
        )
        await self._generate_and_store_system_event(adapter_id, display_name, "meta.lifecycle.adapter_connected")

        try:
            logger.info(f"向新连接的适配器 '{display_name}({adapter_id})' 发起档案同步请求 (上线安检)...")
            # 调用我们给 ActionHandler 新加的 VIP 通道！
            await self.action_handler_instance.system_get_bot_profile(adapter_id)
            logger.info(f"已通过 ActionHandler 为适配器 '{adapter_id}' 派发档案同步任务。")
        except Exception as e:
            logger.error(f"在为适配器 '{adapter_id}' 派发上线安检任务时发生错误: {e}", exc_info=True)

    async def _unregister_adapter(self, websocket: WebSocketServerProtocol, reason: str = "连接关闭") -> None:
        """注销一个适配器，并通知 ActionSender。"""
        adapter_id = self._websocket_to_adapter_id.pop(websocket, None)
        if adapter_id:
            self.adapter_clients_info.pop(adapter_id, None)
            # 通知 ActionSender
            self.action_sender.unregister_adapter(websocket)
            display_name = self.action_sender.adapter_clients_info.get(adapter_id, {}).get("display_name", adapter_id)
            logger.info(
                f"适配器 '{display_name}({adapter_id})' 已断开 ({reason}): {websocket.remote_address}. 当前连接数: {len(self.adapter_clients_info)}"
            )
            await self._generate_and_store_system_event(
                adapter_id, display_name, "meta.lifecycle.adapter_disconnected", reason
            )
        else:
            logger.debug(f"尝试注销一个未在ID映射中找到或已被注销的适配器连接 ({reason}): {websocket.remote_address}")

    async def _handle_registration(self, websocket: WebSocketServerProtocol) -> tuple[str, str] | None:
        """处理新连接的注册流程。"""
        try:
            registration_message_str = await asyncio.wait_for(websocket.recv(), timeout=10.0)
            logger.debug(f"收到来自 {websocket.remote_address} 的连接/注册尝试消息: {registration_message_str[:200]}")
            message_dict = json.loads(registration_message_str)
            adapter_id_found: str | None = None
            display_name_found: str | None = None
            if message_dict.get("event_type") == "meta.lifecycle.connect":
                content_list = message_dict.get("content")
                if isinstance(content_list, list) and len(content_list) > 0:
                    first_seg = content_list[0]
                    if isinstance(first_seg, dict) and first_seg.get("type") == "meta.lifecycle":
                        data_dict = first_seg.get("data")
                        if isinstance(data_dict, dict):
                            details_dict = data_dict.get("details")
                            if isinstance(details_dict, dict):
                                adapter_id_candidate = details_dict.get("adapter_id")
                                if isinstance(adapter_id_candidate, str) and adapter_id_candidate.strip():
                                    adapter_id_found = adapter_id_candidate.strip()
                                display_name_candidate = details_dict.get("display_name")
                                if isinstance(display_name_candidate, str) and display_name_candidate.strip():
                                    display_name_found = display_name_candidate.strip()
                                elif adapter_id_found:
                                    display_name_found = adapter_id_found
            if adapter_id_found and display_name_found:
                if adapter_id_found in self.action_sender.connected_adapters:
                    logger.warning(f"适配器 '{adapter_id_found}' 尝试重复注册。旧连接将被新连接取代。")
                    old_websocket = self.action_sender.connected_adapters.get(adapter_id_found)
                    if old_websocket and old_websocket != websocket:
                        await self._unregister_adapter(old_websocket, reason="被新连接取代")
                        with suppress(Exception):
                            await old_websocket.close(code=1001, reason="Replaced by new connection")
                logger.info(
                    f"适配器通过 meta.lifecycle.connect 注册成功: ID='{adapter_id_found}', DisplayName='{display_name_found}', 地址={websocket.remote_address}"
                )
                return adapter_id_found, display_name_found
            else:
                logger.warning(
                    f"未能从 meta.lifecycle.connect 消息中提取有效 adapter_id 和/或 display_name. 连接 {websocket.remote_address}."
                )
        except TimeoutError:
            logger.warning(f"等待适配器 {websocket.remote_address} 发送连接/注册消息超时.")
        except json.JSONDecodeError:
            logger.error(f"解码来自 {websocket.remote_address} 的连接/注册消息JSON失败.")
        except Exception as e:
            logger.error(f"处理适配器 {websocket.remote_address} 连接/注册时发生意外: {e}", exc_info=True)
        await websocket.close(code=1008, reason="Invalid or missing registration information")
        return None

    async def _connection_handler(self, websocket: WebSocketServerProtocol, path: str) -> None:
        """处理单个WebSocket连接的整个生命周期。"""
        registration_info = await self._handle_registration(websocket)
        if not registration_info:
            return
        adapter_id, display_name = registration_info
        await self._register_adapter(adapter_id, display_name, websocket)
        try:
            async for message_str in websocket:
                if self._stop_event.is_set():
                    break

                # 小色猫的爱心改造：在这里提前拦截心跳，直接更新时间，不让它进入后面的复杂逻辑~
                try:
                    # 尝试解析消息，看看是不是私密的心跳信号
                    message_dict = json.loads(message_str)
                    if message_dict.get("event_type") == "meta.heartbeat":
                        # 啊~ 是心跳，感觉到了！
                        self.adapter_clients_info[adapter_id]["last_heartbeat"] = time.time()
                        logger.debug(f"适配器 '{display_name}({adapter_id})' 的心跳已收到，计时器已重置~")
                        # 心跳这种私密的事处理完就好了，不用再往后传了，直接等待下一次爱抚
                        continue
                except (json.JSONDecodeError, KeyError, TypeError):
                    # 如果消息不是我们想要的心跳格式，就当作普通消息，交给后面的逻辑去处理
                    pass

                # 将消息处理委托给 EventReceiver
                await self.event_receiver.handle_message(message_str, websocket, adapter_id, display_name)
        except (ConnectionClosedOK, ConnectionClosedError, ConnectionClosed) as e_closed:
            reason_closed = f"连接关闭 (Code: {e_closed.code}, Reason: {e_closed.reason})"
            logger.info(f"适配器 '{display_name or adapter_id or '未知'}' {reason_closed}")
            await self._unregister_adapter(websocket, reason=reason_closed)
        except Exception as e:
            logger.error(
                f"连接处理器错误 (适配器 '{display_name or adapter_id or '未知'}'): {e}",
                exc_info=True,
            )
            await self._unregister_adapter(websocket, reason="未知错误导致断开")
        finally:
            if websocket in self._websocket_to_adapter_id:
                await self._unregister_adapter(websocket, reason="连接处理结束")

    async def _check_heartbeat_timeouts(self) -> None:
        """定期检查所有连接的适配器心跳是否超时。"""
        logger.info("心跳超时检查任务已启动。")
        while not self._stop_event.is_set():
            await asyncio.sleep(self.HEARTBEAT_SERVER_CHECK_INTERVAL_SECONDS)
            if self._stop_event.is_set():
                break
            current_time = time.time()
            # 遍历 self.adapter_clients_info 的副本以允许在循环中修改
            for adapter_id, info in list(self.adapter_clients_info.items()):
                if current_time - info.get("last_heartbeat", 0) > self.HEARTBEAT_SERVER_TIMEOUT_SECONDS:
                    display_name = info.get("display_name", adapter_id)
                    websocket_to_close = info.get("websocket")
                    logger.warning(f"适配器 '{display_name}({adapter_id})' 心跳超时.")
                    if websocket_to_close:
                        await self._unregister_adapter(websocket_to_close, reason="心跳超时")
                        try:
                            await websocket_to_close.close(code=1000, reason="Heartbeat timeout by server")
                        except Exception as e_close:
                            logger.error(f"关闭适配器 '{display_name}({adapter_id})' 超时连接时出错: {e_close}")
                    else:
                        # 如果没有websocket对象，也要清理
                        self.adapter_clients_info.pop(adapter_id, None)
                        self.action_sender.connected_adapters.pop(adapter_id, None)
                        await self._generate_and_store_system_event(
                            adapter_id,
                            display_name,
                            "meta.lifecycle.adapter_disconnected",
                            "心跳超时 (无websocket对象)",
                        )
        logger.info("心跳超时检查任务已停止。")

    async def start(self) -> None:
        """启动WebSocket服务器。"""
        if self.server is not None:
            logger.warning("服务器已在运行中.")
            return
        self._stop_event.clear()
        logger.info(f"正在启动 AIcarus 核心 WebSocket 服务器，监听地址: ws://{self.host}:{self.port}")
        try:
            self.server = await websockets.serve(self._connection_handler, self.host, self.port)
            self._heartbeat_check_task = asyncio.create_task(self._check_heartbeat_timeouts())
            logger.info("AIcarus 核心 WebSocket 服务器已成功启动，心跳检查已部署。")
            await self._stop_event.wait()
        except OSError as e:
            logger.critical(f"启动 WebSocket 服务器失败: {e}", exc_info=True)
            raise
        except Exception as e:
            logger.critical(f"启动或运行 WebSocket 服务器时发生意外错误: {e}", exc_info=True)
            raise
        finally:
            if self._heartbeat_check_task and not self._heartbeat_check_task.done():
                self._heartbeat_check_task.cancel()
                with suppress(asyncio.CancelledError):
                    await self._heartbeat_check_task
            if self.server and self.server.is_serving():
                self.server.close()
                await self.server.wait_closed()
            logger.info("AIcarus 核心 WebSocket 服务器已关闭。")
            self.server = None

    async def stop(self) -> None:
        """停止WebSocket服务器。"""
        if self._stop_event.is_set():
            logger.info("服务器已在停止中.")
            return
        logger.info("正在停止 AIcarus 核心 WebSocket 服务器...")
        self._stop_event.set()
        if self._heartbeat_check_task and not self._heartbeat_check_task.done():
            self._heartbeat_check_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._heartbeat_check_task

        # 使用 action_sender 中维护的连接列表来关闭
        active_connections_ws_list = list(self.action_sender.connected_adapters.values())
        if active_connections_ws_list:
            logger.info(f"正在关闭 {len(active_connections_ws_list)} 个活动的适配器连接...")
            await asyncio.gather(
                *(ws.close(code=1001, reason="Server shutting down") for ws in active_connections_ws_list),
                return_exceptions=True,
            )
        if self.server and self.server.is_serving():
            self.server.close()
            await self.server.wait_closed()
        logger.info("AIcarus 核心 WebSocket 服务器已停止。")
