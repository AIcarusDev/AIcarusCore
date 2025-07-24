# 文件: src/message_processing/default_message_processor.py (竞速模式适配版 V1.0)
import asyncio
import time
from typing import TYPE_CHECKING, Optional

from aicarus_protocols import Event as ProtocolEvent
from src.common.custom_logging.logging_config import get_logger
from src.common.intelligent_interrupt_system.models import SemanticModel
from src.config import config
from src.database import (
    ActionLogStorageService, # 引入ActionLogStorageService
    ConversationStorageService,
    DBEventDocument,
    EnrichedConversationInfo,
    PersonStorageService,
)
from src.database.services.event_storage_service import EventStorageService
from src.focus_chat_mode.chat_session_manager import ChatSessionManager
from websockets.server import WebSocketServerProtocol

if TYPE_CHECKING:
    from src.core_communication.core_ws_server import CoreWebsocketServer
    from src.core_logic.consciousness_flow import CoreLogic as CoreLogicFlow

logger = get_logger(__name__)


class DefaultMessageProcessor:
    """
    默认消息处理器 (竞速模式适配版)。
    它现在拥有了识别“回声”事件并将其正确路由到 ChatSession 的关键能力。
    """

    def __init__(
        self,
        event_service: EventStorageService,
        conversation_service: ConversationStorageService,
        person_service: PersonStorageService,
        action_log_service: ActionLogStorageService, # 注入 ActionLog 服务
        semantic_model: "SemanticModel",
        core_websocket_server: Optional["CoreWebsocketServer"] = None,
        qq_chat_session_manager: Optional["ChatSessionManager"] = None,
    ) -> None:
        self.event_service: EventStorageService = event_service
        self.conversation_service: ConversationStorageService = conversation_service
        self.person_service: PersonStorageService = person_service
        self.action_log_service: ActionLogStorageService = action_log_service # 保存 ActionLog 服务实例
        self.semantic_model: SemanticModel = semantic_model
        self.core_comm_layer: CoreWebsocketServer | None = core_websocket_server
        self.qq_chat_session_manager = qq_chat_session_manager
        self.core_logic: CoreLogicFlow | None = None
        logger.info("DefaultMessageProcessor 初始化完成 (竞速模式适配版)。")

    async def _is_self_echo_message(self, event: ProtocolEvent) -> tuple[bool, str | None]:
        """
        检查一个消息事件是否是AI自身动作的回声。
        
        Args:
            event: 传入的消息事件。

        Returns:
            一个元组 (is_echo, original_action_id)。
            如果事件是回声，is_echo 为 True，且 original_action_id 是触发该回声的原始动作ID。
        """
        platform_message_id = event.get_message_id()
        if not platform_message_id:
            return False, None

        # 通过 message_id 去 action_logs 里反查，看是不是我们自己刚执行的动作
        action_log = await self.action_log_service.get_action_log_by_platform_message_id(platform_message_id)
        if action_log:
            original_action_id = action_log.get("action_id")
            logger.debug(f"事件 (msg_id: {platform_message_id}) 被识别为动作 '{original_action_id}' 的回声。")
            return True, original_action_id
            
        return False, None

    async def process_event(
        self,
        proto_event: ProtocolEvent,
        websocket: WebSocketServerProtocol,
        needs_persistence: bool = True,
    ) -> None:
        """处理来自适配器的事件。"""
        if not isinstance(proto_event, ProtocolEvent):
            logger.error(f"传入的事件不是 ProtocolEvent 类型，而是 {type(proto_event)}。跳过处理。")
            return

        platform_id = proto_event.get_platform()
        if not platform_id:
            logger.error(f"无法从事件类型 '{proto_event.event_type}' 中解析出平台ID，事件处理中止。")
            return

        logger.debug(
            f"开始处理事件: {proto_event.event_type}, ID: {proto_event.event_id}, "
            f"Platform: {platform_id}, BotID: {proto_event.bot_id}"
        )

        try:
            # --- 核心改造点：优先处理回声事件 ---
            if proto_event.event_type.startswith("message."):
                is_echo, original_action_id = await self._is_self_echo_message(proto_event)
                if is_echo and original_action_id:
                    # 这是一个回声！我们需要通知对应的 ChatSession
                    if self.qq_chat_session_manager and proto_event.conversation_info:
                        conv_id = proto_event.conversation_info.conversation_id
                        session = self.qq_chat_session_manager.sessions.get(conv_id)
                        if session:
                            await session.signal_echo_received(original_action_id)
                        else:
                             logger.warning(f"收到回声但找不到会话 '{conv_id}' 来接收信号。")
                    
                    # 回声事件的任务已经完成，不需要持久化或进一步处理，直接返回
                    return

            # --- 如果不是回声，则按原流程处理 ---
            # ... (关联Person、持久化事件、更新会话档案的逻辑保持不变)
            person_id, account_uid = None, None
            if proto_event.user_info and proto_event.user_info.user_id:
                person_id, account_uid = await self.person_service.find_or_create_person_and_account(
                    proto_event.user_info, platform_id
                )
                if person_id and account_uid and proto_event.conversation_info:
                    await self.person_service.update_membership(
                        account_uid=account_uid,
                        conversation_id=proto_event.conversation_info.conversation_id,
                        user_info=proto_event.user_info,
                        conversation_name=proto_event.conversation_info.name,
                    )
            
            if needs_persistence:
                db_event_document = DBEventDocument.from_protocol(proto_event)
                db_event_document.person_id_associated = person_id
                if proto_event.event_type.startswith("message.") and self.semantic_model and (text_content := proto_event.get_text_content()):
                    embedding_vector = self.semantic_model.encode([text_content])[0]
                    db_event_document.embedding = embedding_vector.tolist()
                
                event_doc_to_save = db_event_document.to_dict()
                await self.event_service.save_event_document(event_doc_to_save)
                logger.debug(f"事件文档 '{proto_event.event_id}' 已保存。")

            if proto_event.conversation_info and proto_event.conversation_info.conversation_id:
                enriched_conv_info = EnrichedConversationInfo.from_protocol_and_event_context(
                    proto_conv_info=proto_event.conversation_info,
                    event_platform=platform_id,
                    event_bot_id=proto_event.bot_id,
                )
                conversation_doc_to_upsert = enriched_conv_info.to_db_document()
                await self.conversation_service.upsert_conversation_document(conversation_doc_to_upsert)

            # --- 分发逻辑 ---
            # 在新架构下，消息事件不再由这里分发给 CoreLogic。
            # CoreLogic 的中断哨兵会自己去数据库里看新消息。
            # 我们只需要处理那些非消息类的、需要主动处理的事件。
            if proto_event.event_type == f"notice.{platform_id}.bot.profile_update":
                await self._handle_bot_profile_update(proto_event)
            else:
                 logger.debug(f"事件类型 '{proto_event.event_type}' 无需在此主动处理，交由核心循环自行发现。")

        except Exception as e:
            logger.error(f"处理事件 (ID: {proto_event.event_id}) 的核心逻辑中发生错误: {e}", exc_info=True)

    async def _handle_bot_profile_update(self, event: ProtocolEvent) -> None:
        # (这个方法的逻辑保持不变)
        try:
            if not event.content: return
            report_data = event.content[0].data
            conversation_id = report_data.get("conversation_id")
            update_type = report_data.get("update_type")
            new_value = report_data.get("new_value")
            if not conversation_id or not update_type: return

            logger.info(f"收到会话 '{conversation_id}' 中祂的档案更新通知: '{update_type}' -> '{new_value}'")
            session = self.qq_chat_session_manager.sessions.get(conversation_id) if self.qq_chat_session_manager else None
            if session:
                logger.info(f"会话 '{conversation_id}' 处于激活状态，正在实时更新其祂的档案缓存。")
                if update_type == "card_change": session.bot_profile_cache["card"] = new_value
                session.last_profile_update_time = time.time()
                profile_to_save = session.bot_profile_cache.copy()
                profile_to_save["updated_at"] = int(time.time() * 1000)
                await self.conversation_service.update_conversation_field(conversation_id, "bot_profile_in_this_conversation", profile_to_save)
            else:
                logger.info(f"会话 '{conversation_id}' 不活跃，仅更新其在数据库中祂的档案。")
                conv_doc = await self.conversation_service.get_conversation_document_by_id(conversation_id)
                profile_to_update = {}
                if conv_doc and conv_doc.get("bot_profile_in_this_conversation") and isinstance(conv_doc["bot_profile_in_this_conversation"], dict):
                    profile_to_update = conv_doc["bot_profile_in_this_conversation"]
                if update_type == "card_change": profile_to_update["card"] = new_value
                profile_to_update["updated_at"] = int(time.time() * 1000)
                await self.conversation_service.update_conversation_field(conversation_id, "bot_profile_in_this_conversation", profile_to_update)
        except Exception as e:
            logger.error(f"处理祂的档案更新通知时出错: {e}", exc_info=True)