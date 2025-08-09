# src/focus_chat_mode/chat_session.py
import time
from typing import TYPE_CHECKING, Any

from src.action.action_handler import ActionHandler
from src.common.custom_logging.logging_config import get_logger
from src.config import config
from src.database import EnrichedConversationInfo
from src.database.services.event_storage_service import EventStorageService
from src.database.services.thought_storage_service import ThoughtStorageService
from src.llmrequest.llm_processor import Client as LLMProcessorClient

from .behavioral_guidance_generator import BehavioralGuidanceGenerator
from .summarization_manager import SummarizationManager

if TYPE_CHECKING:
    from src.common.intelligent_interrupt_system.intelligent_interrupter import (
        IntelligentInterrupter,
    )
    from src.common.summarization_observation.summarization_service import SummarizationService
    from src.core_logic.consciousness_flow import CoreLogic as CoreLogicFlow
    from src.core_logic.internal_info_builder import InternalInfoBuilder
    from src.database.services.entity_graph_service import EntityGraphService
    from src.database.services.summary_storage_service import SummaryStorageService
    from src.focus_chat_mode.chat_session_manager import ChatSessionManager

CACHE_EXPIRATION_SECONDS = 600
CONVERSATION_DETAILS_CACHE_EXPIRATION_SECONDS = 7200

logger = get_logger(__name__)


class ChatSession:
    """管理单个专注聊天会话的状态和逻辑."""

    def __init__(
        self,
        conversation_info: EnrichedConversationInfo,
        conversation_id: str,
        llm_client: LLMProcessorClient,
        event_storage: EventStorageService,
        action_handler: ActionHandler,
        bot_id: str,
        core_logic: "CoreLogicFlow",
        chat_session_manager: "ChatSessionManager",
        summarization_service: "SummarizationService",
        summary_storage_service: "SummaryStorageService",
        internal_info_builder: "InternalInfoBuilder",
        intelligent_interrupter: "IntelligentInterrupter",
        thought_storage_service: "ThoughtStorageService",
        entity_graph_service: "EntityGraphService",
        initial_last_processed_timestamp: float | None = None,
    ) -> None:
        # --- 模块化组件 ---
        self.conversation_info = conversation_info
        self.conversation_id: str = conversation_id  # 注意：这里的 ID 是 entity_uid
        self.llm_client: LLMProcessorClient = llm_client
        self.event_storage: EventStorageService = event_storage
        self.action_handler: ActionHandler = action_handler
        self.bot_id: str = bot_id
        self.platform: str = conversation_info.platform
        self.conversation_type: str | None = conversation_info.type
        self.conversation_name: str | None = conversation_info.name
        self.core_logic = core_logic
        self.chat_session_manager = chat_session_manager
        self.summarization_service = summarization_service
        self.summary_storage_service = summary_storage_service
        self.internal_info_builder = internal_info_builder
        self.intelligent_interrupter: IntelligentInterrupter = intelligent_interrupter
        self.thought_storage_service: ThoughtStorageService = thought_storage_service
        self.entity_graph_service = entity_graph_service  # 存储服务实例

        # --- 功能组件初始化 ---
        self.summarization_manager = SummarizationManager(self)
        self.guidance_generator = BehavioralGuidanceGenerator(self)

        # --- 会话运行时状态 ---
        self.last_processed_timestamp: float = (
            initial_last_processed_timestamp
            if initial_last_processed_timestamp is not None
            else time.time() * 1000.0
        )
        self.pending_handover_result: dict | None = None

        # --- 行为计数器 ---
        self.no_action_count: int = 0
        self.consecutive_bot_messages_count: int = 0

        # --- 中断相关 ---
        self.interruption_context: dict | None = None
        self.sent_action_ids_this_turn: list[str] = []
        self.current_handover_summary: str | None = None
        self.last_command_feedback: str | None = None

        # --- 缓存 ---
        self.bot_profile_cache: dict[str, Any] = {}
        self.last_profile_update_time: float = 0.0
        self.working_memory: dict[str, Any] = {}  # <-- 新增：工作记忆容器

        logger.info(f"[ChatSession][{self.conversation_id}] 实例已创建。")

    async def get_bot_profile(self) -> dict[str, Any]:
        """智能获取祂的档案，如果缓存有效则直接返回，否则从数据库加载最新的客观数据."""
        # --- 步骤 1: 检查缓存（The Fast Path） ---
        if self.bot_profile_cache and (
            time.time() - self.last_profile_update_time < CACHE_EXPIRATION_SECONDS
        ):
            return self.bot_profile_cache

        # --- 步骤 2: 缓存未命中，直接、精确地从数据库获取当前平台实体 ---
        # 移除了原有的 "获取全部再查找" 的低效逻辑
        entity_doc = await self.entity_graph_service.get_self_entity_by_platform(
            self.platform
        )

        if not (entity_doc and isinstance(entity_doc, dict)):
            logger.warning(f"[{self.conversation_id}] 未找到祂有效的全局档案。将使用临时基础档案。")
            return {"user_id": self.bot_id, "nickname": config.persona.bot_name}

        details = entity_doc.get("details") or {}

        # 2.1 构建基础档案
        base_profile = {
            "user_id": details.get("platform_id"),
            "nickname": details.get("nickname"),
            "platform": self.platform,
        }

        # 3. 获取特定于本会话的身份信息 (card, role)
        if self.conversation_type == "group":
            presence_info = await self.entity_graph_service.get_self_presence_in_conversation(
                platform=self.platform,
                conversation_entity_uid=self.conversation_id,
            )
            if presence_info and isinstance(presence_info, dict):
                base_profile["card"] = presence_info.get("cardname")
                base_profile["role"] = presence_info.get("permission_level")
                logger.debug(f"[{self.conversation_id}] 成功获取到祂在本会话的群名片和权限。")

        # --- 步骤 4: 更新缓存 ---
        self.bot_profile_cache = base_profile
        self.last_profile_update_time = time.time()
        logger.debug(f"[{self.conversation_id}] 已加载并缓存祂的完整档案。")

        return self.bot_profile_cache

    def reset_consecutive_bot_message_count(self) -> None:
        """一个专门重置连续发言计数器的方法."""
        if self.consecutive_bot_messages_count > 0:
            logger.debug(f"[{self.conversation_id}] 检测到他人发言，重置连续发言计数器。")
            self.consecutive_bot_messages_count = 0

    async def shutdown(self) -> None:
        """关闭会话前的清理工作."""
        logger.info(f"[{self.conversation_id}] 开始执行关闭清理...")
        # 在这里可以添加其他需要清理的逻辑，比如保存最终状态等
        # 目前主要逻辑在 deactivate_session 中，这里作为一个预留接口
        logger.info(f"[{self.conversation_id}] 关闭清理完成。")
