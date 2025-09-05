# src/os/communication/core_ws_server.py

import asyncio
import json
import time
import uuid
from contextlib import suppress
from typing import TYPE_CHECKING, Any

import websockets
from aicarus_protocols import ConversationInfo, SegBuilder
from aicarus_protocols import Event as ProtocolEvent
from aicarus_protocols import UserInfo as ProtocolUserInfo
from src.common.custom_logging.logging_config import get_logger
from src.config import config
from src.services.action.components.base_builder import BaseAppBuilder
from src.services.database import EntityGraphService
from src.services.database.services.event_storage_service import EventStorageService
from websockets.exceptions import ConnectionClosed, ConnectionClosedError, ConnectionClosedOK
from websockets.server import WebSocketServerProtocol

from .action_sender import ActionSender
from .event_receiver import EventReceiver

if TYPE_CHECKING:
    from src.bootstrap.container import ServiceContainer
    from src.services.action.action_handler import ActionHandler

logger = get_logger(__name__)


class CoreWebsocketServer:
    """AIcarus 核心 WebSocket 服务器类."""

    HEARTBEAT_CLIENT_INTERVAL_SECONDS = 30
    HEARTBEAT_SERVER_TIMEOUT_SECONDS = 90
    HEARTBEAT_SERVER_CHECK_INTERVAL_SECONDS = 15

    def __init__(
        self,
        host: str,
        port: int,
        event_receiver: EventReceiver,
        container: "ServiceContainer",
        action_sender: ActionSender,
        event_storage_service: EventStorageService,
        action_handler_instance: "ActionHandler",
        entity_service: "EntityGraphService",
    ) -> None:
        self.host: str = host
        self.port: int = port
        self.container = container
        self.server: websockets.WebSocketServer | None = None
        self.event_storage_service = event_storage_service
        self.event_receiver = event_receiver
        self.action_sender = action_sender
        self.action_handler_instance = action_handler_instance
        self.entity_service = entity_service
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
        final_event_type = f"meta.system.{event_type_suffix}"

        event_content_text = ""
        if event_type_suffix == "lifecycle.adapter_connected":
            event_content_text = f"[状态] {display_name}({adapter_id})连接成功"
        elif event_type_suffix == "lifecycle.adapter_disconnected":
            event_content_text = f"[状态] {display_name}({adapter_id})断开({reason})"
        else:
            logger.debug(f"生成一个通用的系统事件，后缀: {event_type_suffix}")
            event_content_text = f"[系统事件] {display_name}({adapter_id}): {event_type_suffix}"

        system_event = ProtocolEvent(
            event_id=f"core_event_{adapter_id}_{event_type_suffix.split('.')[-1]}_{int(current_timestamp)}_{uuid.uuid4().hex[:6]}",
            event_type=final_event_type,
            time=int(current_timestamp * 1000),
            bot_id=config.persona.bot_name,
            content=[SegBuilder.text(event_content_text)],
            conversation_info=ConversationInfo(conversation_id="system_events", type="system"),
            user_info=ProtocolUserInfo(user_id="system", user_nickname="AIcarus Core"),
        )

        if self.event_storage_service:
            try:
                event_dict = system_event.to_dict()
                event_dict["platform"] = adapter_id
                await self.event_storage_service.save_event_document(event_dict)
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
        """注册一个新的适配器，并根据其需求和类型决定处理流程."""
        # 在注册任何适配器之前，首先确保其在我们的认知图谱中拥有一个客观实体。
        # 这是一个幂等操作，如果实体已存在，它会直接返回；
        # 如果不存在，则会原子性地创建实体及其Profile。
        await self.entity_service.get_or_create_platform_entity(adapter_id, display_name)

        current_timestamp = time.time()
        self._websocket_to_adapter_id[websocket] = adapter_id
        self.adapter_clients_info[adapter_id] = {
            "websocket": websocket,
            "last_heartbeat": current_timestamp,
            "display_name": display_name,
            "bot_profile": None,
            "is_ready": False,  # 新增 is_ready 标志
        }
        self.action_sender.register_adapter(adapter_id, display_name, websocket)
        logger.info(
            f"适配器 '{display_name}({adapter_id})' 已连接: {websocket.remote_address}. "
            f"当前连接数: {len(self.adapter_clients_info)}"
        )
        await self._generate_and_store_system_event(
            adapter_id, display_name, "lifecycle.adapter_connected"
        )

        # --- 核心逻辑修改：不再在此处直接触发安检 ---
        logger.info(
            f"适配器 '{display_name}({adapter_id})' 已注册，"
            f"等待其发送 'ready' 信号以启动安检（如果需要）。"
        )

    async def _run_inspection_ceremony(self, builder: BaseAppBuilder) -> None:
        """后台运行安检的协程 (只负责重试和调用)."""
        max_retries = 3
        initial_delay = 5
        backoff_factor = 2
        for attempt in range(max_retries + 1):
            try:
                if attempt > 0:
                    delay = initial_delay * (backoff_factor ** (attempt - 1))
                    logger.info(
                        f"适配器 '{builder.app_name}' 的安检将在 {delay} "
                        f"秒后进行第 {attempt}/{max_retries} 次重试..."
                    )
                    await asyncio.sleep(delay)

                await asyncio.sleep(0.5)
                # BUG:run_on_connect_inspection似乎未定义。
                await builder.run_on_connect_inspection(self.container)
                logger.info(
                    f"适配器 '{builder.app_name}' 的安检仪式 (尝试次数 {attempt + 1}) 已执行。"
                )
                return

            except Exception as e:
                logger.error(
                    f"在为适配器 '{builder.app_name}' 举行后台安检仪式时发生严重错误: {e}",
                    exc_info=True
                )

        logger.critical(f"后台安检仪式在经过 {max_retries + 1} 次尝试后彻底失败！")

    async def _handle_ready_event(self, event: ProtocolEvent) -> None:
        """处理来自适配器的 ready 事件，并触发安检流程."""
        adapter_id = event.get_platform()
        if not adapter_id or adapter_id not in self.adapter_clients_info:
            logger.warning(f"收到来自未知或未注册适配器 '{adapter_id}' 的 ready 事件，已忽略。")
            return

        connection_info = self.adapter_clients_info[adapter_id]
        if connection_info.get("is_ready"):
            logger.info(f"适配器 '{adapter_id}' 已处于就绪状态，重复的 ready 事件已被忽略。")
            return

        display_name = connection_info.get("display_name", adapter_id)
        logger.info(f"适配器 '{display_name}({adapter_id})' 已报告就绪状态。")
        connection_info["is_ready"] = True

        # --- 核心修改：从 ready 事件中提取并缓存 profile_data ---
        try:
            details = event.content[0].data.get("details", {})
            if details and "profile_data" in details:
                profile = details["profile_data"]
                if profile:
                    connection_info["bot_profile"] = profile
                    logger.success(f"已从适配器 '{adapter_id}' 的 ready 信号中接收并缓存了其档案。")
                else:
                    logger.warning(
                        f"适配器 '{adapter_id}' 在 ready 信号中提供了空的 profile_data。"
                    )
        except (IndexError, AttributeError, KeyError) as e:
            logger.warning(f"处理来自 '{adapter_id}' 的 ready 事件时，提取 profile_data 失败: {e}")
        # --- 修改结束 ---

        builder = self.container.application_manager.get_builder_by_name(adapter_id)
        if builder and builder.needs_on_connect_inspection:
            logger.info(f"平台 '{display_name}({adapter_id})' 需要上线安检，启动安检仪式...")
            inspection_task = asyncio.create_task(self._run_inspection_ceremony(builder))
            self.active_inspection_tasks.add(inspection_task)
            inspection_task.add_done_callback(lambda t: self.active_inspection_tasks.discard(t))
        else:
            logger.info(f"平台 '{display_name}({adapter_id})' 已就绪，但无需执行上线安检。")
            # 对于无需安检的平台，在 ready 后执行简单登记
            await self._register_simple_identity(adapter_id, display_name)

    async def wait_for_all_inspections(self) -> None:
        """等待所有正在进行的安检任务完成.

        这个方法提供了一个阻塞点，确保在继续执行依赖安检结果的逻辑前，
        所有平台的身份信息都已获取。
        """
        if not self.active_inspection_tasks:
            logger.info("没有正在进行的安检任务需要等待。")
            return

        logger.info(f"正在等待 {len(self.active_inspection_tasks)} 个平台的安检仪式完成...")
        await asyncio.gather(*self.active_inspection_tasks)
        logger.success("所有待处理的安检仪式均已完成。")

    async def _unregister_adapter(
        self, websocket: WebSocketServerProtocol, reason: str = "连接关闭"
    ) -> None:
        """注销一个适配器，并通知 ActionSender."""
        adapter_id = self._websocket_to_adapter_id.pop(websocket, None)
        if adapter_id:
            info = self.adapter_clients_info.pop(adapter_id, {})
            display_name = info.get("display_name", adapter_id)
            # 通知 ActionSender
            self.action_sender.unregister_adapter(websocket)

            logger.info(
                f"适配器 '{display_name}({adapter_id})' 已断开 ({reason}): "
                f"{websocket.remote_address}. 当前连接数: {len(self.adapter_clients_info)}"
            )
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

            # 尝试从消息中解析出 event_type
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

                # 尝试将消息解析为事件，以处理元事件
                try:
                    message_dict = json.loads(message_str)
                    event = ProtocolEvent.from_dict(message_dict)

                    if event.event_type.endswith(".lifecycle.ready"):
                        await self._handle_ready_event(event)
                        continue  # 处理完毕，继续下一轮循环

                    if event.event_type.endswith(".heartbeat"):
                        if adapter_id in self.adapter_clients_info:
                            self.adapter_clients_info[adapter_id]["last_heartbeat"] = time.time()
                            logger.debug(
                                f"适配器 '{display_name}({adapter_id})' 的心跳已收到，计时器已重置~"
                            )
                        continue # 处理完毕

                except (json.JSONDecodeError, KeyError, TypeError, AttributeError):
                    # 解析失败或不是有效事件对象，说明是普通消息，交由下面处理
                    pass

                # 如果不是我们在这里处理的任何元事件，就交给通用处理器
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
                            "lifecycle.adapter_disconnected",
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

    async def _register_simple_identity(self, adapter_id: str, display_name: str) -> None:
        """对于无需安检的平台，执行一个简单的身份登记流程.

        现在它也会在数据库里创建一个基础的Account档案!
        """
        # bot_id 对于工具平台来说，就是它的 platform_id
        bot_id_for_platform = adapter_id

        logger.info(f"为工具平台 '{adapter_id}' 创建或更新数据库中的基础Account档案...")
        # 1. 构造一个最基础的 UserInfo，只需要 user_id 和 nickname
        bot_user_info = ProtocolUserInfo(user_id=bot_id_for_platform, user_nickname=display_name)
        # 2. 调用 entity_service 的公共方法来创建“人”和“账号”，并把它们关联起来
        #    is_self=True 会确保它关联到唯一的 aic_person_0
        person_id, account_uid = await self.entity_service.create_new_profile_with_account_entity(
            user_info=bot_user_info, platform_id=adapter_id, is_self=True
        )
        if not person_id or not account_uid:
            logger.error(f"为工具平台 '{adapter_id}' 创建基础Account档案失败！")
            # 这里可以考虑是否要断开连接，但暂时先只打日志
        else:
            logger.success(
                f"已成功为工具平台 '{adapter_id}' 在数据库中登记身份 (Account UID: {account_uid})。"
            )

        logger.info(f"平台 '{display_name}({adapter_id})' 已完成轻量化身份登记。")
