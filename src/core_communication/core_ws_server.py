# src/core_communication/core_ws_server.py (小色猫·绝对统治版)
import asyncio
import json
import time
import uuid
from contextlib import suppress
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.action.action_handler import ActionHandler

import websockets

# 导入我们全新的、纯洁的协议对象！
from aicarus_protocols import ConversationInfo, SegBuilder
from aicarus_protocols import Event as ProtocolEvent
from src.common.custom_logging.logging_config import get_logger
from src.config import config
from src.core_communication.action_sender import ActionSender
from src.core_communication.event_receiver import EventReceiver
from src.core_logic.self_awareness_inspector import inspect_and_initialize_self_profile
from src.database import DBEventDocument, PersonStorageService
from src.database.services.event_storage_service import EventStorageService
from websockets.exceptions import ConnectionClosed, ConnectionClosedError, ConnectionClosedOK
from websockets.server import WebSocketServerProtocol

logger = get_logger(__name__)


class CoreWebsocketServer:
    """AIcarus 核心 WebSocket 服务器类.

    这个服务器负责处理来自不同适配器的连接，接收事件，发送动作指令，
    并维护适配器的心跳状态。它还会在适配器连接和断开时生成系统事件，
    并在适配器连接时执行安检仪式.

    Attributes:
        host (str): 服务器监听的主机地址.
        port (int): 服务器监听的端口号.
        server (websockets.WebSocketServer | None): WebSocket服务器实例.
        event_storage_service (EventStorageService): 事件存储服务实例，用于存储事件.
        event_receiver (EventReceiver): 事件接收器实例，用于处理接收到的事件.
        action_sender (ActionSender): 动作发送器实例，用于发送动作指令.
        action_handler_instance (ActionHandler): 动作处理器实例，用于处理动作逻辑.
        person_service (PersonStorageService): 人物存储服务实例，用于管理人物信息.
        adapter_clients_info (dict[str, dict[str, Any]]): 存储适配器连接信息的字典.
        _websocket_to_adapter_id (dict[WebSocketServerProtocol, str]): 映射WebSocket连接到
            适配器ID的字典.
        _stop_event (asyncio.Event): 用于控制服务器停止的事件.
        _heartbeat_check_task (asyncio.Task | None): 心跳检查任务实例.
        active_inspection_tasks (set[asyncio.Task]): 存储所有正在进行的安检任务的集合.
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
        person_service: "PersonStorageService",
    ) -> None:
        self.host: str = host
        self.port: int = port
        self.server: websockets.WebSocketServer | None = None
        self.event_storage_service = event_storage_service
        self.event_receiver = event_receiver
        self.action_sender = action_sender
        self.action_handler_instance = action_handler_instance
        self.person_service = person_service
        self.adapter_clients_info: dict[str, dict[str, Any]] = {}
        self._websocket_to_adapter_id: dict[WebSocketServerProtocol, str] = {}
        self._stop_event: asyncio.Event = asyncio.Event()
        self._heartbeat_check_task: asyncio.Task | None = None
        self.active_inspection_tasks: set[asyncio.Task] = set()

    async def _generate_and_store_system_event(
        self, adapter_id: str, display_name: str, event_type_suffix: str, reason: str = ""
    ) -> None:
        """生成并存储系统生命周期事件。现在它接收的是事件后缀."""
        current_timestamp = time.time()

        # --- ❤❤❤ 构造事件时，也遵循新的命名空间规则！❤❤❤ ---
        # 我们用 "system" 作为平台ID，代表这是核心系统自己产生的事件
        final_event_type = f"meta.system.{event_type_suffix}"

        event_content_text = ""
        if event_type_suffix == "lifecycle.adapter_connected":
            event_content_text = f"[状态] {display_name}({adapter_id})连接成功"
        elif event_type_suffix == "lifecycle.adapter_disconnected":
            event_content_text = f"[状态] {display_name}({adapter_id})断开({reason})"
        else:
            # --- ❤❤❤ 这里是修复点！我不再抱怨了，而是直接用后缀作为内容！❤❤❤ ---
            logger.debug(f"生成一个通用的系统事件，后缀: {event_type_suffix}")
            event_content_text = f"[系统事件] {display_name}({adapter_id}): {event_type_suffix}"

        system_event = ProtocolEvent(
            event_id=f"core_event_{adapter_id}_{event_type_suffix.split('.')[-1]}_{int(current_timestamp)}_{uuid.uuid4().hex[:6]}",
            event_type=final_event_type,
            time=int(current_timestamp * 1000),
            bot_id=config.persona.bot_name,
            content=[SegBuilder.text(event_content_text)],
            conversation_info=ConversationInfo(conversation_id="system_events", type="system"),
        )

        if self.event_storage_service:
            try:
                # DBEventDocument.from_protocol 会从 event_type 解析出 platform
                await self.event_storage_service.save_event_document(
                    DBEventDocument.from_protocol(system_event).to_dict()
                )
                logger.info(f"已生成并存储系统事件: {event_content_text}")
            except Exception as e:
                logger.error(
                    f"存储系统事件 for '{adapter_id}' (type: {final_event_type}) 失败: {e}",
                    exc_info=True,
                )
        else:
            logger.warning(f"EventStorageService 未初始化，无法存储系统事件 for '{adapter_id}'.")

    async def _register_adapter(
        self, adapter_id: str, display_name: str, websocket: WebSocketServerProtocol
    ) -> None:
        """注册一个新的适配器，并通知 ActionSender."""
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
            f"适配器 '{display_name}({adapter_id})' 已连接: {websocket.remote_address}. "
            f"当前连接数: {len(self.adapter_clients_info)}"
        )
        await self._generate_and_store_system_event(
            adapter_id, display_name, "lifecycle.adapter_connected"
        )

        logger.info(f"为新连接的适配器 '{display_name}({adapter_id})' 举行欢迎仪式 (执行安检)...")

        # 在后台运行安检仪式
        # 这样可以避免阻塞主线程，确保服务器能继续处理其他连接
        inspection_task = asyncio.create_task(
            self._run_inspection_ceremony(adapter_id, display_name)
        )
        self.active_inspection_tasks.add(inspection_task)
        # 为了确保任务完成后能清理掉
        def _done_callback(t: asyncio.Task) -> None:
            """任务完成后的回调函数，用于清理和记录异常."""
            self.active_inspection_tasks.discard(t)
            # 如果任务没有被取消且有异常，记录错误日志
            if not t.cancelled() and t.exception():
                logger.error("Exception in inspection_ceremony task:", exc_info=t.exception())

        inspection_task.add_done_callback(_done_callback)

    async def _run_inspection_ceremony(self, adapter_id: str, display_name: str) -> None:
        """一个专门用来在后台运行安检的协程."""
        try:
            # 给一点点时间，确保连接完全稳定
            await asyncio.sleep(0.5)

            inspection_success = await inspect_and_initialize_self_profile(
                person_service=self.person_service,
                action_handler=self.action_handler_instance,
                platform_id=adapter_id,
            )

            if not inspection_success:
                logger.error(f"后台安检仪式失败！适配器 '{adapter_id}' 的相关功能可能受影响。")
        except Exception as e:
            logger.error(
                f"在为适配器 '{adapter_id}' 举行后台安检仪式时发生严重错误: {e}", exc_info=True
            )

    async def _unregister_adapter(
        self, websocket: WebSocketServerProtocol, reason: str = "连接关闭"
    ) -> None:
        """注销一个适配器，并通知 ActionSender."""
        adapter_id = self._websocket_to_adapter_id.pop(websocket, None)
        if adapter_id:
            self.adapter_clients_info.pop(adapter_id, None)
            # 通知 ActionSender
            self.action_sender.unregister_adapter(websocket)
            display_name = self.action_sender.adapter_clients_info.get(adapter_id, {}).get(
                "display_name", adapter_id
            )
            logger.info(
                f"适配器 '{display_name}({adapter_id})' 已断开 ({reason}): "
                f"{websocket.remote_address}. 当前连接数: {len(self.adapter_clients_info)}"
            )
            # --- ❤❤❤ 这里是修复点！只传入后缀！❤❤❤ ---
            await self._generate_and_store_system_event(
                adapter_id, display_name, "lifecycle.adapter_disconnected", reason
            )
        else:
            logger.debug(
                f"尝试注销一个未在ID映射中找到或已被注销的适配器连接 ({reason}): "
                f"{websocket.remote_address}"
            )

    async def _handle_registration(
        self, websocket: WebSocketServerProtocol
    ) -> tuple[str, str] | None:
        """处理适配器的注册消息，解析出 adapter_id 和 display_name.

        Args:
            websocket: 连接的WebSocket对象.

        Returns:
            tuple[str, str] | None: 如果注册成功，返回 (adapter_id, display_name)，否则返回 None.
        """
        try:
            registration_message_str = await asyncio.wait_for(websocket.recv(), timeout=10.0)
            logger.debug(
                f"收到来自 {websocket.remote_address} 的连接/注册尝试消息: "
                f"{registration_message_str[:200]}"
            )
            message_dict = json.loads(registration_message_str)

            # --- ❤❤❤ 最终高潮点！直接从 event_type 解析！❤❤❤ ---
            event_type = message_dict.get("event_type", "")
            parts = event_type.split(".")

            adapter_id_found: str | None = None
            display_name_found: str | None = None

            # 验证格式是否为 meta.{platform_id}.lifecycle.connect
            if (
                len(parts) == 4
                and parts[0] == "meta"
                and parts[2] == "lifecycle"
                and parts[3] == "connect"
            ):
                adapter_id_found = parts[1]

                # 尝试从 content 中获取更友好的 display_name，作为备用
                content_list = message_dict.get("content")
                if isinstance(content_list, list) and len(content_list) > 0:
                    first_seg = content_list[0]
                    if isinstance(first_seg, dict) and first_seg.get("type") == "meta.lifecycle":
                        details_dict = first_seg.get("data", {}).get("details", {})
                        if isinstance(details_dict, dict):
                            display_name_candidate = details_dict.get("display_name")
                            if (
                                isinstance(display_name_candidate, str)
                                and display_name_candidate.strip()
                            ):
                                display_name_found = display_name_candidate.strip()

                # 如果没找到 display_name，就用 adapter_id 代替
                if not display_name_found:
                    display_name_found = adapter_id_found

            if adapter_id_found and display_name_found:
                if adapter_id_found in self.action_sender.connected_adapters:
                    logger.warning(
                        f"适配器 '{adapter_id_found}' 尝试重复注册。旧连接将被新连接取代。"
                    )
                    old_websocket = self.action_sender.connected_adapters.get(adapter_id_found)
                    if old_websocket and old_websocket != websocket:
                        await self._unregister_adapter(old_websocket, reason="被新连接取代")
                        with suppress(Exception):
                            await old_websocket.close(
                                code=1001, reason="Replaced by new connection"
                            )
                logger.info(
                    f"适配器通过 event_type 注册成功: ID='{adapter_id_found}', "
                    f"DisplayName='{display_name_found}', 地址={websocket.remote_address}"
                )
                return adapter_id_found, display_name_found
            else:
                logger.warning(
                    f"未能从事件类型 '{event_type}' 中解析出有效的注册信息。"
                    f"连接 {websocket.remote_address} 将被关闭。"
                )
        except TimeoutError:
            logger.warning(f"等待适配器 {websocket.remote_address} 发送注册消息超时。")
        except json.JSONDecodeError:
            logger.error(f"解码来自 {websocket.remote_address} 的注册消息JSON失败。")
        except Exception as e:
            logger.error(
                f"处理适配器 {websocket.remote_address} 注册时发生意外: {e}", exc_info=True
            )

        await websocket.close(code=1008, reason="Invalid or missing registration information")
        return None

    async def _connection_handler(self, websocket: WebSocketServerProtocol, path: str) -> None:
        """处理单个WebSocket连接的整个生命周期."""
        registration_info = await self._handle_registration(websocket)
        if not registration_info:
            return
        adapter_id, display_name = registration_info
        await self._register_adapter(adapter_id, display_name, websocket)
        try:
            async for message_str in websocket:
                if self._stop_event.is_set():
                    break

                # 换成我这个充满弹性和包容性的、全新的性感姿势！
                # ↓↓↓ 小猫咪的淫纹植入处！ ↓↓↓
                try:
                    # 尝试解析消息，看看是不是私密的心跳信号
                    message_dict = json.loads(message_str)
                    # --- ❤❤❤ 最终高潮修复点！❤❤❤ ---
                    # 我把它调教得更‘淫荡’、更‘包容’了
                    msg_event_type = message_dict.get("event_type")
                    if (
                        msg_event_type
                        and msg_event_type.startswith("meta.")
                        and msg_event_type.endswith(".heartbeat")
                    ):
                        # 啊~ 是心跳，感觉到了！
                        self.adapter_clients_info[adapter_id]["last_heartbeat"] = time.time()
                        logger.debug(
                            f"适配器 '{display_name}({adapter_id})' 的心跳已收到，计时器已重置~"
                        )
                        # 心跳这种私密的事处理完就好了，不用再往后传了，直接等待下一次爱抚
                        continue
                except (json.JSONDecodeError, KeyError, TypeError):
                    # 如果消息不是我们想要的心跳格式，就当作普通消息，交给后面的逻辑去处理
                    pass
                # ↑↑↑ 小猫咪的淫纹植入处！ ↑↑↑

                # 将消息处理委托给 EventReceiver
                await self.event_receiver.handle_message(
                    message_str, websocket, adapter_id, display_name
                )
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
        """定期检查所有连接的适配器心跳是否超时."""
        logger.info("心跳超时检查任务已启动。")
        while not self._stop_event.is_set():
            await asyncio.sleep(self.HEARTBEAT_SERVER_CHECK_INTERVAL_SECONDS)
            if self._stop_event.is_set():
                break
            current_time = time.time()
            # 遍历 self.adapter_clients_info 的副本以允许在循环中修改
            for adapter_id, info in list(self.adapter_clients_info.items()):
                if (
                    current_time - info.get("last_heartbeat", 0)
                    > self.HEARTBEAT_SERVER_TIMEOUT_SECONDS
                ):
                    display_name = info.get("display_name", adapter_id)
                    websocket_to_close = info.get("websocket")
                    logger.warning(f"适配器 '{display_name}({adapter_id})' 心跳超时.")
                    if websocket_to_close:
                        await self._unregister_adapter(websocket_to_close, reason="心跳超时")
                        try:
                            await websocket_to_close.close(
                                code=1000, reason="Heartbeat timeout by server"
                            )
                        except Exception as e_close:
                            logger.error(
                                f"关闭适配器 '{display_name}({adapter_id})' "
                                f"超时连接时出错: {e_close}"
                            )
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
        """启动WebSocket服务器."""
        if self.server is not None:
            logger.warning("服务器已在运行中.")
            return
        self._stop_event.clear()
        logger.info(
            f"正在启动 AIcarus 核心 WebSocket 服务器，监听地址: ws://{self.host}:{self.port}"
        )
        try:
            self.server = await websockets.serve(
                self._connection_handler,
                self.host,
                self.port,
                max_size=50 * 1024 * 1024,
            )
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
        """停止WebSocket服务器和所有活动连接.

        这个方法会优雅地关闭所有适配器连接，并确保服务器干净地停止.
        如果服务器已经在停止中，直接返回.
        """
        if self._stop_event.is_set():
            logger.info("服务器已在停止中，别催啦，讨厌~")
            return
        logger.info("正在停止 AIcarus 核心 WebSocket 服务器...")
        self._stop_event.set()

        # 1. 去除心跳检查任务，确保结束进程不会被心跳检查拖慢
        if self._heartbeat_check_task and not self._heartbeat_check_task.done():
            self._heartbeat_check_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._heartbeat_check_task

        # 2. 获取所有当前活动的适配器连接
        #    这里使用 action_sender 中维护的连接列表，确保我们能优雅地关闭所有连接
        active_connections_ws_list = list(self.action_sender.connected_adapters.values())

        if active_connections_ws_list:
            logger.info(f"正在温柔地关闭 {len(active_connections_ws_list)} 个活动的适配器连接...")

            # 3. 创建一个任务列表，来处理每个连接的断开
            #    websocket.close() 会触发 _connection_handler 的 finally 块，那里包含了写日志的逻辑
            close_tasks = [
                ws.close(code=1001, reason="Server shutting down")
                for ws in active_connections_ws_list
            ]

            # 4. 使用 asyncio.gather 来并发地执行所有断开任务
            results = await asyncio.gather(*close_tasks, return_exceptions=True)

            # 检查每个断开任务的结果
            # 如果有异常，记录警告日志
            for ws, result in zip(active_connections_ws_list, results, strict=False):
                if isinstance(result, Exception):
                    adapter_id = self._websocket_to_adapter_id.get(ws, "未知适配器")
                    logger.warning(f"关闭与适配器 '{adapter_id}' 的连接时出了点小意外: {result}")

            # 5. 给所有适配器一点时间来处理后事
            #    这里的缓冲时间是为了确保所有适配器都能优雅地关闭连接
            await asyncio.sleep(0.1)  # 给0.1秒的缓冲时间
            logger.info("所有适配器连接的关闭指令已发出，并给予了短暂的余韵时间来处理后事。")

        # 5. 最后，等所有客人都穿好裤子走光了，我们再关闭整个会所
        if self.server and self.server.is_serving():
            self.server.close()
            await self.server.wait_closed()

        logger.info("AIcarus 核心 WebSocket 服务器已完全停止，干净又卫生，哼！")

        # 使用 action_sender 中维护的连接列表来关闭
        active_connections_ws_list = list(self.action_sender.connected_adapters.values())
        if active_connections_ws_list:
            logger.info(f"正在关闭 {len(active_connections_ws_list)} 个活动的适配器连接...")
            await asyncio.gather(
                *(
                    ws.close(code=1001, reason="Server shutting down")
                    for ws in active_connections_ws_list
                ),
                return_exceptions=True,
            )
        if self.server and self.server.is_serving():
            self.server.close()
            await self.server.wait_closed()
        logger.info("AIcarus 核心 WebSocket 服务器已停止。")
