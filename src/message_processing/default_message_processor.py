# src/message_processing/default_message_processor.py
import time
import uuid
from typing import TYPE_CHECKING, Optional

# 导入我们全新的、不带platform字段的协议对象！
from aicarus_protocols import Event as ProtocolEvent
from aicarus_protocols import Seg
from websockets.server import WebSocketServerProtocol

from src.common.custom_logging.logging_config import get_logger
from src.config import config
from src.database import ConversationStorageService, DBEventDocument, EnrichedConversationInfo
from src.database.services.event_storage_service import EventStorageService
from src.focus_chat_mode.chat_session_manager import ChatSessionManager

if TYPE_CHECKING:
    from src.core_communication.core_ws_server import CoreWebsocketServer
    from src.main import CoreSystemInitializer

logger = get_logger(__name__)


class DefaultMessageProcessor:
    """
    默认的消息处理器 (V6.0 命名空间统治版)
    """

    def __init__(
        self,
        event_service: EventStorageService,
        conversation_service: ConversationStorageService,
        core_websocket_server: Optional["CoreWebsocketServer"] = None,
        qq_chat_session_manager: Optional["ChatSessionManager"] = None,
    ) -> None:
        self.event_service: EventStorageService = event_service
        self.conversation_service: ConversationStorageService = conversation_service
        self.core_comm_layer: CoreWebsocketServer | None = core_websocket_server
        self.qq_chat_session_manager = qq_chat_session_manager
        self.core_initializer_ref: CoreSystemInitializer | None = None
        logger.info("DefaultMessageProcessor 初始化完成，已配备新的存储服务。")
        if self.core_comm_layer:
            logger.info("DefaultMessageProcessor 已获得 CoreWebsocketServer 实例的引用。")
        else:
            logger.warning("DefaultMessageProcessor 未获得 CoreWebsocketServer 实例的引用，将无法主动发送动作。")

    async def process_event(
        self, proto_event: ProtocolEvent, websocket: WebSocketServerProtocol, needs_persistence: bool = True
    ) -> None:
        """
        处理来自适配器的事件。
        """
        if not isinstance(proto_event, ProtocolEvent):
            logger.error(f"传入的事件不是 ProtocolEvent 类型，而是 {type(proto_event)}。跳过处理。")
            return

        # --- ❤❤❤ 高潮点 #1: 从 event_type 中解析平台信息！❤❤❤ ---
        platform_id = proto_event.get_platform()
        if not platform_id:
            logger.error(f"无法从事件类型 '{proto_event.event_type}' 中解析出平台ID，事件处理中止。")
            return

        logger.debug(
            f"开始处理事件: {proto_event.event_type}, ID: {proto_event.event_id}, Platform: {platform_id}, BotID: {proto_event.bot_id}"
        )

        event_status = "unread"
        conversation_id_for_check = (
            proto_event.conversation_info.conversation_id if proto_event.conversation_info else None
        )

        if (
            proto_event.event_type.startswith(f"message.{platform_id}")
            and config.test_function.enable_test_group
            and (not conversation_id_for_check or conversation_id_for_check not in config.test_function.test_group)
        ):
            logger.debug(
                f"测试模式下，事件 '{proto_event.event_id}' 来自非测试会话 '{conversation_id_for_check}'，状态将设置为 'ignored'。"
            )
            event_status = "ignored"

        try:
            if needs_persistence:
                # DBEventDocument 的 from_protocol 方法需要被改造，以适应新的 Event 结构
                db_event_document = DBEventDocument.from_protocol(proto_event)
                db_event_document.status = event_status
                event_doc_to_save = db_event_document.to_dict()
                await self.event_service.save_event_document(event_doc_to_save)
                logger.debug(f"事件文档 '{proto_event.event_id}' 已保存，status='{event_status}'")

            if proto_event.conversation_info and proto_event.conversation_info.conversation_id:
                # EnrichedConversationInfo 的 from_protocol_and_event_context 也需要改造
                enriched_conv_info = EnrichedConversationInfo.from_protocol_and_event_context(
                    proto_conv_info=proto_event.conversation_info,
                    event_platform=platform_id,  # 使用我们解析出来的 platform_id
                    event_bot_id=proto_event.bot_id,
                )
                conversation_doc_to_upsert = enriched_conv_info.to_db_document()
                upsert_result_key = await self.conversation_service.upsert_conversation_document(
                    conversation_doc_to_upsert
                )
                if upsert_result_key:
                    logger.info(f"会话档案 (ConversationInfo) '{upsert_result_key}' 已成功插入或更新。")
                else:
                    logger.error(
                        f"处理会话档案 (ConversationInfo) '{proto_event.conversation_info.conversation_id}' 时发生错误。"
                    )
            elif proto_event.event_type.startswith(f"message.{platform_id}"):
                logger.warning(
                    f"消息类事件 {proto_event.event_id} 缺少有效的 ConversationInfo，无法为其创建或更新会话档案。"
                )

            if event_status != "unread":
                logger.debug(f"事件 '{proto_event.event_id}' 的状态为 '{event_status}'，将跳过后续分发。")
                return

            # --- ❤❤❤ 高潮点 #2: 分发时，事件类型也带着完整的命名空间！❤❤❤ ---
            if proto_event.event_type.startswith(f"message.{platform_id}"):
                await self._handle_message_event(proto_event, websocket)
            elif proto_event.event_type.startswith(f"request.{platform_id}"):
                await self._handle_request_event(proto_event, websocket)
            elif proto_event.event_type == f"notice.{platform_id}.bot.profile_update":
                await self._handle_bot_profile_update(proto_event)
            else:
                logger.debug(f"事件类型 '{proto_event.event_type}' 没有特定的处理器，跳过分发。")

        except Exception as e:
            logger.error(f"处理事件 (ID: {proto_event.event_id}) 的核心逻辑中发生错误: {e}", exc_info=True)

    async def _handle_bot_profile_update(self, event: ProtocolEvent) -> None:
        """
        处理机器人自身档案更新的通知，并更新相关会话的缓存和数据库。
        哼，小报告来了，我得赶紧记下来。
        """
        try:
            if not event.content:
                logger.warning("收到的机器人档案更新通知没有内容。")
                return

            # 小报告的核心内容在第一个 seg 的 data 里
            report_data = event.content[0].data
            conversation_id = report_data.get("conversation_id")
            update_type = report_data.get("update_type")
            new_value = report_data.get("new_value")

            if not conversation_id or not update_type:
                logger.warning(f"机器人档案更新通知格式不正确，缺少关键信息: {report_data}")
                return

            logger.info(f"收到会话 '{conversation_id}' 的机器人档案更新通知: '{update_type}' -> '{new_value}'")

            # 检查这个会话当前是否在专注聊天模式下是活跃的
            session = (
                self.qq_chat_session_manager.sessions.get(conversation_id) if self.qq_chat_session_manager else None
            )

            if session and session.is_active:
                # 如果会话活跃，直接更新它的短期记忆（内存缓存）
                logger.info(f"会话 '{conversation_id}' 处于激活状态，正在实时更新其机器人档案缓存。")
                if update_type == "card_change":
                    session.bot_profile_cache["card"] = new_value
                # 可以在这里添加对其他更新类型的处理，比如头衔 'title'

                session.last_profile_update_time = time.time()  # 别忘了更新时间戳！

                # 同时，把更新后的完整档案存回数据库（长期记忆）
                profile_to_save = session.bot_profile_cache.copy()
                profile_to_save["updated_at"] = int(time.time() * 1000)
                await self.conversation_service.update_conversation_field(
                    conversation_id, "bot_profile_in_this_conversation", profile_to_save
                )
            else:
                # 如果会话不活跃，我们只更新数据库里的长期记忆
                # 这样下次会话被激活时，它就能从数据库读到最新的信息
                logger.info(f"会话 '{conversation_id}' 不活跃，仅更新其在数据库中的机器人档案。")

                # 先从数据库读出旧的档案，但我们只关心它的 card
                conv_doc = await self.conversation_service.get_conversation_document_by_id(conversation_id)

                # 淫乱的开始：创建一个全新的、干净的档案，而不是在旧的上面乱搞
                profile_to_update = {}

                # 如果旧档案里有卡片信息，就先继承过来，像是继承了前戏的余韵
                if (
                    conv_doc
                    and conv_doc.get("bot_profile_in_this_conversation")
                    and isinstance(conv_doc["bot_profile_in_this_conversation"], dict)
                ):
                    # 只取我们想要的 card，别的乱七八糟的都不要！
                    old_card = conv_doc["bot_profile_in_this_conversation"].get("card")
                    if old_card:
                        profile_to_update["card"] = old_card

                # 在干净的档案基础上更新，这才是正确的体位！
                if update_type == "card_change":
                    profile_to_update["card"] = new_value
                # 可以在这里添加对其他更新类型的处理，比如头衔 'title'
                # elif update_type == "title_change":
                #     profile_to_update["title"] = new_value

                profile_to_update["updated_at"] = int(time.time() * 1000)

                # 写回数据库，现在里面只有纯洁的爱，没有乱七八糟的“群P”记录了
                await self.conversation_service.update_conversation_field(
                    conversation_id, "bot_profile_in_this_conversation", profile_to_update
                )

        except Exception as e:
            logger.error(f"处理机器人档案更新通知时出错: {e}", exc_info=True)

    async def _handle_message_event(self, proto_event: ProtocolEvent, websocket: WebSocketServerProtocol) -> bool:
        try:
            if self.qq_chat_session_manager:
                # --- ❤❤❤ 高潮点 #3: 将带有新 event_type 的事件喂给下一层！❤❤❤ ---
                await self.qq_chat_session_manager.handle_incoming_message(proto_event)
            return True
        except Exception as e:
            logger.error(f"处理消息事件 (ID: {proto_event.event_id}) 时发生错误: {e}", exc_info=True)
            return False

    async def _handle_request_event(self, proto_event: ProtocolEvent, websocket: WebSocketServerProtocol) -> None:
        """处理请求类事件（如好友请求、加群请求）。"""
        try:
            # --- ❤❤❤ 新的、更优雅的体位！❤❤❤ ---
            # 我们不再调用自己那个私有的、丑陋的方法了
            # 而是直接享受 proto_event 对象自带的、性感的 get_text_content() 方法！
            _text_content = proto_event.get_text_content()

            sender_id_log = "未知用户"
            if proto_event.user_info and proto_event.user_info.user_id:  # 安全访问
                sender_id_log = str(proto_event.user_info.user_id)
            logger.info(f"收到请求事件: {proto_event.event_type} 来自用户 {sender_id_log}")

            # 示例：自动同意好友请求
            if proto_event.event_type.endswith("friend.add"):  # 使用 endswith 更健壮
                logger.info(f"检测到好友添加请求事件，来自 {sender_id_log}。准备自动同意。")
                if not self.core_comm_layer:
                    logger.error("核心通信层 (CoreWebsocketServer) 实例未设置，无法自动同意好友请求。")
                    return

                if (
                    not proto_event.content
                    or not isinstance(proto_event.content[0], Seg)
                    or not proto_event.content[0].data
                ):
                    logger.error("好友请求事件的内容格式不正确或为空，无法获取请求参数 (如 request_flag)。")
                    return

                request_params_data: dict = proto_event.content[0].data
                request_flag = request_params_data.get("request_flag")

                if not request_flag:
                    logger.error("好友请求事件的内容中缺少 'request_flag'，无法自动同意。")
                    return

                # 构造动作事件时，也需要使用新的命名空间
                platform_id = proto_event.get_platform()
                approve_action_event_type = f"action.{platform_id}.request.friend.approve"

                approve_action_seg = Seg(
                    type=approve_action_event_type,
                    data={
                        "request_flag": request_flag,
                        "remark": "AIcarus Core 自动通过了您的好友请求！",
                    },
                )

                approve_action_event = ProtocolEvent(
                    event_id=f"action_approve_friend_{uuid.uuid4()}",
                    event_type=approve_action_event_type,
                    time=int(time.time() * 1000.0),
                    bot_id=proto_event.bot_id,
                    content=[approve_action_seg],
                )
                logger.debug(f"准备自动同意好友请求的动作事件: {approve_action_event.to_dict()}")

                # ActionSender 现在会从 event_type 解析平台ID
                send_success = await self.core_comm_layer.action_sender.send_action_to_adapter_by_id(
                    platform_id, approve_action_event.to_dict()
                )
                if send_success:
                    logger.info(f"自动同意来自 {sender_id_log} 的好友请求的动作已发送。")
                else:
                    logger.error(f"自动同意来自 {sender_id_log} 的好友请求的动作发送失败。")
        except Exception as e:
            logger.error(f"处理请求事件 (ID: {proto_event.event_id}) 时发生错误: {e}", exc_info=True)
