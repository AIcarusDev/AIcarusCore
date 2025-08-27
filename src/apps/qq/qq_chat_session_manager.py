# src/focus_chat_mode/chat_session_manager.py
import asyncio
import time
from typing import TYPE_CHECKING, Optional

from src.common.custom_logging.logging_config import get_logger
from src.config.aicarus_configs import FocusChatModeSettings
from src.services.action.action_handler import ActionHandler
from src.services.database.models import ConversationDetails
from src.services.database.services.event_storage_service import EventStorageService
from src.services.database.services.thought_storage_service import ThoughtStorageService
from src.services.llmrequest.llm_processor import Client as LLMProcessorClient

from .qq_chat_session import ChatSession

if TYPE_CHECKING:
    from src.common.intelligent_interrupt_system.intelligent_interrupter import (
        IntelligentInterrupter,
    )
    from src.mind.consciousness_flow import CoreLogic as CoreLogicFlow
    from src.mind.internal_info_builder import InternalInfoBuilder
    from src.services.database import EnrichedConversationInfo
    from src.services.database.services.entity_graph_service import EntityGraphService

logger = get_logger(__name__)


class ChatSessionManager:
    """管理所有 ChatSession 实例，作为会话对象的工厂和缓存池.

    负责提供会话的业务逻辑和数据上下文。
    """

    def __init__(
        self,
        config: FocusChatModeSettings,
        llm_client: LLMProcessorClient,
        # deliberation_llm_client 依赖已移除
        event_storage: EventStorageService,
        action_handler: ActionHandler,
        self_bot_ids_map: dict[str, str],
        intelligent_interrupter: "IntelligentInterrupter",
        entity_graph_service: "EntityGraphService",
        thought_storage_service: "ThoughtStorageService",
        internal_info_builder: "InternalInfoBuilder",
        core_logic: Optional["CoreLogicFlow"] = None,
    ) -> None:
        """初始化 ChatSessionManager."""
        self.config = config
        self.llm_client = llm_client
        self.event_storage = event_storage
        self.action_handler = action_handler
        self.self_bot_ids_map = self_bot_ids_map
        self.thought_storage_service = thought_storage_service
        self.internal_info_builder = internal_info_builder
        self.intelligent_interrupter = intelligent_interrupter
        self.entity_graph_service = entity_graph_service
        self.core_logic = core_logic

        self.sessions: dict[str, ChatSession] = {}
        self.lock = asyncio.Lock()

        # DeliberationService 现在由 DecisionDispatcher 在需要时直接使用，或通过服务容器获取
        # self.deliberation_service = ...

        logger.info("ChatSessionManager 初始化完成。")

    async def get_or_create_session(self, conversation_entity_uid: str) -> ChatSession | None:
        """根据会话实体的UID获取或创建ChatSession.

        这是该模块在新架构下的主要入口点。
        """
        async with self.lock:
            if conversation_entity_uid in self.sessions:
                return self.sessions[conversation_entity_uid]

            logger.info(f"[SessionManager] 为实体 '{conversation_entity_uid}' 创建新的会话实例。")
            conv_entity_doc = await self.entity_graph_service.get_entity_by_key(
                conversation_entity_uid
            )

            if not conv_entity_doc or not isinstance(conv_entity_doc.details, ConversationDetails):
                logger.error(
                    f"严重错误：找不到ID为'{conversation_entity_uid}'的会话实体或类型不匹配！"
                )
                return None

            conv_details = conv_entity_doc.details
            bot_id_for_session = self.self_bot_ids_map.get(conv_details.platform)
            if not bot_id_for_session:
                logger.error(
                    f"无法为平台 '{conv_details.platform}' 创建会话，ID地图中找不到对应ID。"
                )
                return None

            conversation_info_obj = EnrichedConversationInfo(
                conversation_id=conv_details.conversation_id,
                platform=conv_details.platform,
                bot_id=bot_id_for_session,
                type=conv_details.type,
                name=conv_details.name,
                parent_id=conv_details.parent_id,
                avatar=conv_details.avatar,
                extra=conv_details.extra,
            )

            if not self.core_logic:
                raise RuntimeError("CoreLogic未注入，ChatSessionManager无法创建会话。")

            initial_last_processed_timestamp = (
                getattr(conv_entity_doc, "last_read_timestamp", 0.0) or time.time() * 1000.0
            )

            new_session = ChatSession(
                conversation_info=conversation_info_obj,
                conversation_id=conversation_entity_uid,
                llm_client=self.llm_client,
                event_storage=self.event_storage,
                action_handler=self.action_handler,
                bot_id=bot_id_for_session,
                core_logic=self.core_logic,
                chat_session_manager=self,
                intelligent_interrupter=self.intelligent_interrupter,
                thought_storage_service=self.thought_storage_service,
                internal_info_builder=self.internal_info_builder,
                entity_graph_service=self.entity_graph_service,
                initial_last_processed_timestamp=initial_last_processed_timestamp,
            )
            self.sessions[conversation_entity_uid] = new_session

            return new_session

    async def deactivate_session(self, conversation_entity_uid: str) -> None:
        """当聊天窗口关闭时，执行清理工作，例如将会话的最后已读时间戳持久化."""
        async with self.lock:
            if session := self.sessions.pop(conversation_entity_uid, None):
                logger.info(
                    f"[SessionManager] 会话 '{conversation_entity_uid}' "
                    f"对应的窗口已关闭，停用会话实例。"
                )
                final_timestamp = session.last_processed_timestamp
                await self.entity_graph_service.update_conversation_last_read_timestamp(
                    conversation_entity_uid, final_timestamp
                )
                logger.info(
                    f"[SessionManager] 会话 '{conversation_entity_uid}' 的最后已读时间戳已更新。"
                )

    def shutdown(self) -> None:
        """在应用程序关闭时执行清理操作."""
        logger.info("ChatSessionManager 正在关闭...")
        # 未来可以在这里添加需要保存到数据库的批量操作
        logger.info("ChatSessionManager 关闭完成。")
