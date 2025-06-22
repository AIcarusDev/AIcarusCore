# src/message_processing/default_message_processor.py
import asyncio  # 新增导入 asyncio
import time
import uuid
from typing import TYPE_CHECKING, Any, Optional

from aicarus_protocols import ConversationInfo as ProtocolConversationInfo
from aicarus_protocols import ConversationType, Seg, SegBuilder, UserInfo

# v1.4.0 协议导入
from aicarus_protocols import Event as ProtocolEvent  # 重命名以区分数据库模型
from websockets.server import WebSocketServerProtocol

from src.common.custom_logging.logger_manager import get_logger
from src.config import config
from src.database.models import DBEventDocument, EnrichedConversationInfo  # 导入我们定义的数据模型
from src.database.services.conversation_storage_service import ConversationStorageService

# 导入新的存储服务和模型
from src.database.services.event_storage_service import EventStorageService

# 使用TYPE_CHECKING避免循环导入
if TYPE_CHECKING:
    from src.core_communication.core_ws_server import CoreWebsocketServer
    from src.main import CoreSystemInitializer
    from src.sub_consciousness.chat_session_manager import ChatSessionManager  # Updated import


class DefaultMessageProcessor:
    """
    默认的消息处理器。
    负责处理来自适配器的原始事件，进行初步解析、数据转换，
    然后调用相应的存储服务进行持久化，并根据事件类型分发到后续处理逻辑。
    """

    def __init__(
        self,
        event_service: EventStorageService,  # 依赖注入 EventStorageService
        conversation_service: ConversationStorageService,  # 依赖注入 ConversationStorageService
        core_websocket_server: Optional["CoreWebsocketServer"] = None,
        qq_chat_session_manager: Optional["ChatSessionManager"] = None,  # Updated type hint
    ) -> None:
        """
        初始化消息处理器。

        Args:
            event_service: 事件存储服务的实例。
            conversation_service: 会话信息存储服务的实例。
            core_websocket_server: (可选) 核心WebSocket服务器实例，用于主动发送动作。
        """
        self.logger = get_logger(f"AIcarusCore.{self.__class__.__name__}")
        self.event_service: EventStorageService = event_service
        self.conversation_service: ConversationStorageService = conversation_service
        self.core_comm_layer: CoreWebsocketServer | None = core_websocket_server
        self.qq_chat_session_manager = qq_chat_session_manager  # 保存实例
        self.core_initializer_ref: CoreSystemInitializer | None = None
        self.logger.info("DefaultMessageProcessor 初始化完成，已配备新的存储服务。")
        if self.core_comm_layer:
            self.logger.info("DefaultMessageProcessor 已获得 CoreWebsocketServer 实例的引用。")
        else:
            self.logger.warning("DefaultMessageProcessor 未获得 CoreWebsocketServer 实例的引用，将无法主动发送动作。")

    async def process_event(
        self, proto_event: ProtocolEvent, websocket: WebSocketServerProtocol, needs_persistence: bool = True
    ) -> None:
        """
        处理来自适配器的事件。

        Args:
            proto_event: 从适配器接收到的原始 `aicarus_protocols.Event` 对象。
            websocket: 发送此事件的WebSocket连接。
            needs_persistence: 指示此事件是否需要持久化到数据库，默认为True。
        """
        if not isinstance(proto_event, ProtocolEvent):
            self.logger.error(f"传入的事件不是 ProtocolEvent 类型，而是 {type(proto_event)}。跳过处理。")
            return

        self.logger.debug(
            f"开始处理事件: {proto_event.event_type}, ID: {proto_event.event_id}, Platform: {proto_event.platform}, BotID: {proto_event.bot_id}"
        )

        # --- START: 核心改造！先决定要不要深入玩弄 ---
        # 默认这个事件是需要后续处理的处女
        is_event_processed = False
        conversation_id_for_check = (
            proto_event.conversation_info.conversation_id if proto_event.conversation_info else None
        )

        # 如果是测试模式，并且消息来自非测试群，哼，那就直接把你标记为“已玩弄”
        if (
            proto_event.event_type.startswith("message.")
            and config.test_function.enable_test_group
            and (not conversation_id_for_check or conversation_id_for_check not in config.test_function.test_group)
        ):
            self.logger.debug(
                f"测试模式下，事件 '{proto_event.event_id}' 来自非测试会话 '{conversation_id_for_check}'，将直接标记为已处理。"
            )
            is_event_processed = True
        # --- END: 核心改造 ---

        try:
            # 1. 事件持久化 (如果需要)
            # 无论如何都要记录下这次接触，但要带上正确的“贞操锁”
            if needs_persistence:
                db_event_document = DBEventDocument.from_protocol(proto_event)
                # db_event_document.is_processed = is_event_processed  # 在这里！注入我们刚才的判断结果！
                event_doc_to_save = db_event_document.to_dict()
                event_doc_to_save["is_processed"] = is_event_processed
                await self.event_service.save_event_document(event_doc_to_save)
                self.logger.debug(f"事件文档 '{proto_event.event_id}' 已保存，is_processed={is_event_processed}")

            # 2. 会话信息 (ConversationInfo) 的创建或更新
            # 这是必要的爱抚！即使不深入，也要更新对它的了解。
            if proto_event.conversation_info and proto_event.conversation_info.conversation_id:
                enriched_conv_info = EnrichedConversationInfo.from_protocol_and_event_context(
                    proto_conv_info=proto_event.conversation_info,
                    event_platform=proto_event.platform,
                    event_bot_id=proto_event.bot_id,
                )
                conversation_doc_to_upsert = enriched_conv_info.to_db_document()
                upsert_result_key = await self.conversation_service.upsert_conversation_document(
                    conversation_doc_to_upsert
                )
                if upsert_result_key:
                    self.logger.info(f"会话档案 (ConversationInfo) '{upsert_result_key}' 已成功插入或更新。")
                else:
                    self.logger.error(
                        f"处理会话档案 (ConversationInfo) '{proto_event.conversation_info.conversation_id}' 时发生错误。"
                    )
            elif proto_event.event_type.startswith("message."):
                self.logger.warning(
                    f"消息类事件 {proto_event.event_id} 缺少有效的 ConversationInfo，无法为其创建或更新会话档案。"
                )

            # 3. 贞操检查！如果已经被标记为“已玩弄”，那就到此为止，不许再深入了！
            if is_event_processed:
                self.logger.debug(f"事件 '{proto_event.event_id}' 已被预处理并跳过后续分发。")
                return

            # 4. 根据事件类型进行后续分发处理 (只有 is_processed=False 的处女才能进来)
            if proto_event.event_type.startswith("message."):
                await self._handle_message_event(proto_event, websocket)
            elif proto_event.event_type.startswith("request."):
                await self._handle_request_event(proto_event, websocket)
            else:
                self.logger.debug(
                    f"事件类型 '{proto_event.event_type}' 没有特定的处理器，跳过分发。"
                )

        except Exception as e:
            self.logger.error(f"处理事件 (ID: {proto_event.event_id}) 的核心逻辑中发生错误: {e}", exc_info=True)


    def _extract_text_from_protocol_event_content(self, content_seg_list: list[Seg] | None) -> str:
        """
        从 ProtocolEvent.content (即 Seg 对象列表) 中提取所有文本内容。
        """
        if not content_seg_list:
            return ""
        try:
            text_parts = []
            for seg_obj in content_seg_list:  # seg_obj 已经是 Seg 对象
                if not isinstance(seg_obj, Seg):
                    self.logger.warning(f"事件内容列表中发现非 Seg 对象: {type(seg_obj)}，已跳过。")
                    continue
                if seg_obj.type == "text" and seg_obj.data:  # 假设 Seg.data 是一个包含 "text"键的字典
                    text_parts.append(seg_obj.data.get("text", ""))
            return "".join(text_parts).strip()
        except Exception as e:
            self.logger.error(f"从协议事件内容提取文本时发生错误: {e}", exc_info=True)
            return ""

    async def _handle_message_event(self, proto_event: ProtocolEvent, websocket: WebSocketServerProtocol) -> bool:
        """
        处理消息事件。
        如果消息被特殊处理（例如硬编码的命令），则返回 False，表示不应再进行后续的通用处理。
        否则返回 True。

        Args:
            proto_event: 已解析的 `aicarus_protocols.Event` 对象。
            websocket: 相关的 WebSocket 连接。
        Returns:
            一个布尔值，指示是否应继续进行后续的通用事件处理。
        """
        # # --- START: 新增的“窥阴癖之锁” ---
        # # 哼，虽然所有的骚话我都要记下来，但我的肉棒只会为主人指定的测试小穴硬起来！
        # print(f"config.test_function.enable_test_group: {config.test_function.enable_test_group}")
        # if config.test_function.enable_test_group:
        #     conversation_id = (
        #         proto_event.conversation_info.conversation_id if proto_event.conversation_info else None
        #     )
        #     print(f"conversation_id: {conversation_id}")
        #     print(f"config.test_function.test_group: {config.test_function.test_group}")
        #     print(f"conversation_id not in config.test_function.test_group: {conversation_id not in config.test_function.test_group}")
        #     if not conversation_id or conversation_id not in config.test_function.test_group:
        #         self.logger.debug(
        #             f"测试模式下，已记录但跳过主动处理来自非测试会话 '{conversation_id}' 的消息。我就看看，不进去~"
        #         )
        #         return False  # 看完了，不进去，告诉外面我处理好了
        # # --- END: 新增的“窥阴癖之锁” ---
        try:
            # 从 proto_event.content (List[Seg]) 提取文本内容
            _text_content = self._extract_text_from_protocol_event_content(proto_event.content)

            sender_nickname_log = "未知用户"
            sender_id_log = "未知ID"

            if proto_event.user_info:
                sender_nickname_log = proto_event.user_info.user_nickname or sender_nickname_log
                sender_id_log = proto_event.user_info.user_id or sender_id_log

            # TODO: 这里未来可以加入硬编码的命令处理，例如 `!ping`
            # 如果是命令，处理后可以 return False 阻止后续LLM思考

            # 将消息事件分发给 ChatSessionManager
            if self.qq_chat_session_manager:
                await self.qq_chat_session_manager.handle_incoming_message(proto_event)

            return True  # 表示消息已处理，可以继续

        except Exception as e:
            self.logger.error(f"处理消息事件 (ID: {proto_event.event_id}) 时发生错误: {e}", exc_info=True)
            return False

    async def _handle_request_event(self, proto_event: ProtocolEvent, websocket: WebSocketServerProtocol) -> None:
        """处理请求类事件（如好友请求、加群请求）。"""
        try:
            sender_id_log = "未知用户"
            if proto_event.user_info and proto_event.user_info.user_id:  # 安全访问
                sender_id_log = str(proto_event.user_info.user_id)
            self.logger.info(f"收到请求事件: {proto_event.event_type} 来自用户 {sender_id_log}")

            # 示例：自动同意好友请求
            if proto_event.event_type == "request.friend.add":
                self.logger.info(f"检测到好友添加请求事件，来自 {sender_id_log}。准备自动同意。")
                if not self.core_comm_layer:  # 检查通信层
                    self.logger.error("核心通信层 (CoreWebsocketServer) 实例未设置，无法自动同意好友请求。")
                    return

                # 请求事件的内容通常包含执行响应动作所需的参数，例如 request_flag
                # 假设这些参数在 proto_event.content[0].data 中
                if (
                    not proto_event.content
                    or not isinstance(proto_event.content[0], Seg)
                    or not proto_event.content[0].data
                ):
                    self.logger.error("好友请求事件的内容格式不正确或为空，无法获取请求参数 (如 request_flag)。")
                    return

                request_params_data: dict = proto_event.content[0].data
                request_flag = request_params_data.get("request_flag")

                if not request_flag:
                    self.logger.error("好友请求事件的内容中缺少 'request_flag'，无法自动同意。")
                    return

                # 构造同意好友请求的动作内容 Seg
                approve_action_seg_data: dict[str, Any] = {
                    "request_flag": request_flag,  # 用于标识要响应哪个请求
                    "remark": "AIcarus Core 自动通过了您的好友请求！",  # (可选) 同意后的备注名
                }
                approve_action_seg = Seg(  # Seg 对象
                    type="action.request.friend.approve",  # 假设这是协议中定义的同意好友请求动作的Seg类型
                    data=approve_action_seg_data,
                )

                # 构造顶层的动作 Event
                # 对于同意好友请求的动作，通常不需要特定的 conversation_info
                # user_info 可以是被请求的用户信息（即原始请求的发起者），如果协议要求或适配器需要
                action_user_info_for_approve: UserInfo | None = None
                if proto_event.user_info and proto_event.user_info.user_id:
                    action_user_info_for_approve = UserInfo(
                        user_id=proto_event.user_info.user_id, user_nickname=proto_event.user_info.user_nickname
                    )  # 只传递必要信息

                approve_action_event = ProtocolEvent(
                    event_id=f"action_approve_friend_{uuid.uuid4()}",  # 新的唯一ID
                    event_type="action.request.friend.approve",  # 顶层事件类型也指明是同意好友请求
                    time=int(time.time() * 1000.0),
                    platform=proto_event.platform,
                    bot_id=proto_event.bot_id,
                    user_info=action_user_info_for_approve,
                    conversation_info=None,  # 通常此动作不与特定会话关联
                    content=[approve_action_seg],  # Seg 对象列表
                )
                self.logger.debug(f"准备自动同意好友请求的动作事件: {approve_action_event.to_dict()}")
                send_success = await self.core_comm_layer.send_action_to_specific_adapter(
                    websocket, approve_action_event
                )
                if send_success:
                    self.logger.info(f"自动同意来自 {sender_id_log} 的好友请求的动作已发送。")
                else:
                    self.logger.error(f"自动同意来自 {sender_id_log} 的好友请求的动作发送失败。")

            # 此处可以添加对其他请求类型的处理逻辑，例如：
            # elif proto_event.event_type == "request.group.join":
            #     self.logger.info(f"检测到加群请求...")
            #     # ... 类似地构造 action.request.group.approve 或 .reject ...

        except Exception as e:  # 捕获处理请求事件时可能发生的任何错误
            self.logger.error(f"处理请求事件 (ID: {proto_event.event_id}) 时发生错误: {e}", exc_info=True)
