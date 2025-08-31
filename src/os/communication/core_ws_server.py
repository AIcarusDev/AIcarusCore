# src/core_communication/core_ws_server.py
import asyncio
import json
import time
import uuid
from contextlib import suppress
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.services.action.action_handler import ActionHandler

import websockets
from aicarus_protocols import ConversationInfo, SegBuilder
from aicarus_protocols import Event as ProtocolEvent
from aicarus_protocols import UserInfo as ProtocolUserInfo
from src.common.custom_logging.logging_config import get_logger
from src.config import config
from AIcarusCore.src.os.apps.qq.qq_inspection_service import inspect_and_initialize_self_profile
from src.os.apps.registry import platform_builder_registry
from src.services.core_communication.action_sender import ActionSender
from src.services.core_communication.event_receiver import EventReceiver
from src.services.database import EntityGraphService
from src.services.database.services.event_storage_service import EventStorageService
from websockets.exceptions import ConnectionClosed, ConnectionClosedError, ConnectionClosedOK
from websockets.server import WebSocketServerProtocol

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
        action_sender: ActionSender,
        event_storage_service: EventStorageService,
        action_handler_instance: "ActionHandler",
        entity_service: "EntityGraphService",
    ) -> None:
        self.host: str = host
        self.port: int = port
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
        }
        self.action_sender.register_adapter(adapter_id, display_name, websocket)
        logger.info(
            f"适配器 '{display_name}({adapter_id})' 已连接: {websocket.remote_address}. "
            f"当前连接数: {len(self.adapter_clients_info)}"
        )
        await self._generate_and_store_system_event(
            adapter_id, display_name, "lifecycle.adapter_connected"
        )

        # --- 核心逻辑 ---
        builder = platform_builder_registry.get_builder(adapter_id)

        if builder and builder.needs_on_connect_inspection:
            # 路径 A: 需要安检的平台 (e.g., QQ)
            logger.info(f"平台 '{display_name}({adapter_id})' 需要上线安检，启动安检仪式...")

            # 1. 创建安检任务
            inspection_task = asyncio.create_task(
                self._run_inspection_ceremony(adapter_id, display_name)
            )
            self.active_inspection_tasks.add(inspection_task)

            # 2. 定义并绑定回调函数 (只在这里做，只做一次！)
            def _done_callback(t: asyncio.Task) -> None:
                """任务完成后的回调函数，用于清理和记录异常."""
                self.active_inspection_tasks.discard(t)
                if not t.cancelled() and t.exception():
                    logger.error("安检仪式后台任务异常:", exc_info=t.exception())

            inspection_task.add_done_callback(_done_callback)

        else:
            logger.info(f"平台 '{display_name}({adapter_id})' 无需上线安检，执行轻量化身份登记。")
            await self._register_simple_identity(adapter_id, display_name)

    async def _run_inspection_ceremony(self, adapter_id: str, display_name: str) -> None:
        """一个专门用来在后台运行安检的协程."""
        max_retries = 3  # 最多重试3次
        initial_delay = 5  # 初始延迟5秒
        backoff_factor = 2  # 每次重试延迟时间乘以2
        for attempt in range(max_retries + 1):
            try:
                if attempt > 0:
                    delay = initial_delay * (backoff_factor ** (attempt - 1))
                    logger.info(
                        f"适配器 '{adapter_id}' 的安检将在 {delay} 秒后进行"
                        f"第 {attempt}/{max_retries} 次重试..."
                    )
                    await asyncio.sleep(delay)
                logger.info(
                    f"为适配器 '{adapter_id}' 举行欢迎仪式 (执行安检，尝试次数 {attempt + 1})..."
                )
                # 给一点点时间，确保连接完全稳定
                await asyncio.sleep(0.5)

                # [PROBE START] 添加探针，捕获特定解包错误
                try:
                    success, profile_data = await inspect_and_initialize_self_profile(
                        entity_service=self.entity_service,
                        action_handler=self.action_handler_instance,
                        platform_id=adapter_id,
                    )
                except TypeError as e:
                    # 这个探针专门捕获解包错误，提供更具体的上下文
                    logger.critical(
                        f"安检仪式在调用 inspect_and_initialize_self_profile 后"
                        f"发生解包错误 (TypeError)。"
                        f"这通常意味着函数返回值与预期不符。错误: {e}",
                        exc_info=True,
                    )
                    # 将 success 和 profile_data 设置为失败状态，以便重试逻辑可以继续
                    success, profile_data = False, None
                # [PROBE END]

                if success and profile_data:
                    logger.success(
                        f"安检成功 (尝试次数 {attempt + 1})，"
                        f"获取到适配器 '{adapter_id}' 中祂的档案。"
                    )
                    # 将获取到的档案缓存起来
                    if adapter_id in self.adapter_clients_info:
                        self.adapter_clients_info[adapter_id]["bot_profile"] = profile_data

                    # 安检成功后，需要更新 ChatSessionManager 的 ID 地图
                    if self.action_handler_instance.chat_session_manager and (
                        bot_id := profile_data.get("user_id")
                    ):
                        self.action_handler_instance.chat_session_manager.self_bot_ids_map[
                            adapter_id
                        ] = str(bot_id)
                        logger.info(f"ChatSessionManager 的 ID 地图已为平台 '{adapter_id}' 更新。")

                    return  # 成功后直接退出函数

                # 如果执行到这里，说明 success 为 False
                logger.warning(
                    f"安检尝试 {attempt + 1} 失败。返回结果: success={success}, "
                    f"profile_data={str(profile_data)[:200]}"
                )

            except Exception as e:
                logger.error(
                    f"在为适配器 '{adapter_id}' 举行后台安检仪式 (尝试次数 {attempt + 1}) "
                    f"时发生严重错误: {e}",
                    exc_info=True,
                )

        # 如果循环结束都没有成功
        logger.critical(
            f"后台安检仪式在经过 {max_retries + 1} 次尝试后彻底失败！"
            f"适配器 '{adapter_id}' 的相关功能将严重受影响。"
        )

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

                # 预先检查，如果消息明显不是心跳，就直接跳过解析，交给后面的标准处理器
                # 这样可以避免不必要的JSON解析和宽泛的异常捕获
                if ".heartbeat" not in message_str:
                    await self.event_receiver.handle_message(
                        message_str, websocket, adapter_id, display_name
                    )
                    continue

                # 如果消息中包含".heartbeat"，我们再尝试将其作为心跳处理
                try:
                    message_dict = json.loads(message_str)
                    msg_event_type = message_dict.get("event_type")
                    if (
                        msg_event_type
                        and msg_event_type.startswith("meta.")
                        and msg_event_type.endswith(".heartbeat")
                    ):
                        # 确认是心跳，更新时间戳并继续下一次循环
                        self.adapter_clients_info[adapter_id]["last_heartbeat"] = time.time()
                        logger.debug(
                            f"适配器 '{display_name}({adapter_id})' 的心跳已收到，计时器已重置~"
                        )
                        continue
                except (json.JSONDecodeError, KeyError, TypeError):
                    # 解析失败，说明它虽然包含".heartbeat"字符串但不是有效的心跳事件
                    # 这种情况我们依然将它视为普通消息，交给标准处理器
                    logger.debug("消息包含'.heartbeat'但不是有效的心跳事件，交由标准处理器分析。")
                    pass

                # 如果代码执行到这里，说明它不是一个被我们处理掉的心跳事件
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
        # --- [修改结束] ---

        # 下面的内存ID地图更新逻辑保持不变
        if self.action_handler_instance.chat_session_manager:
            self.action_handler_instance.chat_session_manager.self_bot_ids_map[adapter_id] = (
                bot_id_for_platform
            )
            logger.debug(f"ChatSessionManager 的 ID 地图已为平台 '{adapter_id}' 更新 (简单登记)。")

        logger.info(f"平台 '{display_name}({adapter_id})' 已完成轻量化身份登记。")
