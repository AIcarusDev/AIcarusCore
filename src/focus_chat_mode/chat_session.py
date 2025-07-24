# 文件: src/focus_chat_mode/chat_session.py (竞速模式适配版 V1.0)
import asyncio
import contextlib
import time
from typing import TYPE_CHECKING, Any

from src.action.action_handler import ActionHandler
from src.common.custom_logging.logging_config import get_logger
from src.config import config
from src.database import ConversationStorageService
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
    from src.database.services.summary_storage_service import SummaryStorageService
    from src.focus_chat_mode.chat_session_manager import ChatSessionManager

CACHE_EXPIRATION_SECONDS = 600
CONVERSATION_DETAILS_CACHE_EXPIRATION_SECONDS = 7200  # 2小时

logger = get_logger(__name__)


class ChatSession:
    """
    管理单个专注聊天会话的状态和逻辑 (竞速模式适配版)。
    这个类现在拥有一个更健壮的回声等待机制来处理异步消息确认。
    """

    def __init__(
        self,
        conversation_id: str,
        llm_client: LLMProcessorClient,
        event_storage: EventStorageService,
        action_handler: ActionHandler,
        bot_id: str,
        platform: str,
        conversation_type: str,
        core_logic: "CoreLogicFlow",
        chat_session_manager: "ChatSessionManager",
        conversation_service: ConversationStorageService,
        summarization_service: "SummarizationService",
        summary_storage_service: "SummaryStorageService",
        internal_info_builder: "InternalInfoBuilder",
        intelligent_interrupter: "IntelligentInterrupter",
        thought_storage_service: "ThoughtStorageService",
    ) -> None:
        # --- 模块化组件 ---
        self.conversation_id: str = conversation_id
        self.llm_client: LLMProcessorClient = llm_client
        self.event_storage: EventStorageService = event_storage
        self.action_handler: ActionHandler = action_handler
        self.bot_id: str = bot_id
        self.platform: str = platform
        self.conversation_type: str = conversation_type
        self.conversation_name: str | None = None
        self.core_logic = core_logic
        self.chat_session_manager = chat_session_manager
        self.conversation_service = conversation_service
        self.summarization_service = summarization_service
        self.summary_storage_service = summary_storage_service
        self.internal_info_builder = internal_info_builder
        self.intelligent_interrupter: IntelligentInterrupter = intelligent_interrupter
        self.thought_storage_service: ThoughtStorageService = thought_storage_service

        # --- 功能组件初始化 ---
        self.summarization_manager = SummarizationManager(self)
        self.guidance_generator = BehavioralGuidanceGenerator(self)

        # --- 会话运行时状态 ---
        self.last_processed_timestamp: float = 0.0
        self._echo_wait_events: dict[str, asyncio.Event] = {}
        self._received_echo_ids: set[str] = set()
        self._echo_lock = asyncio.Lock()

        # --- 行为计数器 ---
        self.no_action_count: int = 0
        self.consecutive_bot_messages_count: int = 0

        # --- 中断相关 ---
        self.interruption_context: dict | None = None
        self.sent_action_ids_this_turn: list[str] = []

        # --- 上下文与记忆属性 ---
        self.pending_handover_result: dict | None = None

        # --- 缓存 ---
        self.bot_profile_cache: dict[str, Any] = {}
        self.last_profile_update_time: float = 0.0
        self.conversation_details_cache: dict[str, Any] = {}
        self.last_details_update_time: float = 0.0

        logger.info(f"[ChatSession][{self.conversation_id}] 实例已创建。")

    async def wait_for_echo(self, action_id: str, timeout: float = 20.0) -> bool:
        """
        智能等待方法！它现在拥有一个“暂存器”来处理信号提前到达的竞态问题。
        """
        async with self._echo_lock:
            # 检查信号是否已经提前到达并暂存在 _received_echo_ids 中
            if action_id in self._received_echo_ids:
                self._received_echo_ids.remove(action_id)
                logger.success(f"[{self.conversation_id}] 回声等待: 动作 '{action_id}' 在暂存器中命中！立即确认成功。")
                return True

            # 如果信号还没到，就创建一个事件并准备等待
            wake_up_event = asyncio.Event()
            self._echo_wait_events[action_id] = wake_up_event
            logger.info(f"[{self.conversation_id}] 回声等待: 动作 '{action_id}' 未命中暂存器，开始正式等待...")

        try:
            # 在锁外等待，避免阻塞其他信号的处理
            await asyncio.wait_for(wake_up_event.wait(), timeout=timeout)
            logger.success(f"[{self.conversation_id}] 回声等待: 动作 '{action_id}' 在等待过程中被成功唤醒！")
            return True
        except asyncio.TimeoutError:
            logger.warning(f"[{self.conversation_id}] 回声等待: 动作 '{action_id}' 等待超时！")
            return False
        finally:
            # 无论成功与否，都要清理掉等待事件
            async with self._echo_lock:
                self._echo_wait_events.pop(action_id, None)

    async def signal_echo_received(self, action_id: str) -> None:
        """
        智能信号处理方法！它会先尝试唤醒正在等待的任务，如果没人等，就把信号暂存起来。
        """
        async with self._echo_lock:
            # 检查是否有人正在等待这个信号
            if event_to_wake := self._echo_wait_events.get(action_id):
                event_to_wake.set()
                logger.info(f"[{self.conversation_id}] 回声信号: 信号命中等待者，已唤醒动作 '{action_id}'。")
            else:
                # 如果没人等，说明信号比等待请求先到，暂存起来
                self._received_echo_ids.add(action_id)
                logger.warning(f"[{self.conversation_id}] 回声信号: 信号提前到达！已将 '{action_id}' 存入暂存器。")


    async def get_bot_profile(self) -> dict[str, Any]:
        """智能获取祂的档案，优先使用缓存，再查数据库."""
        if self.bot_profile_cache and (time.time() - self.last_profile_update_time < CACHE_EXPIRATION_SECONDS):
            return self.bot_profile_cache

        conv_doc = await self.conversation_service.get_conversation_document_by_id(self.conversation_id)
        if conv_doc and (db_profile := conv_doc.get("bot_profile_in_this_conversation")):
            self.bot_profile_cache = db_profile
            self.last_profile_update_time = time.time()
            logger.debug(f"[{self.conversation_id}] 从数据库加载了祂的档案并放入缓存。")
            return self.bot_profile_cache

        logger.warning(
            f"[{self.conversation_id}] 缓存和数据库中均未找到祂有效的档案。"
            f"将使用初始化时提供的 ID '{self.bot_id}' 构建一个临时的基础档案。"
        )
        return {
            "user_id": self.bot_id,
            "nickname": config.persona.bot_name,
            "card": config.persona.bot_name,
        }

    def reset_consecutive_bot_message_count(self) -> None:
        """一个专门重置连续发言计数器的方法."""
        if self.consecutive_bot_messages_count > 0:
            logger.debug(
                f"[{self.conversation_id}] 检测到他人发言，"
                f"重置 consecutive_bot_messages_count (之前是 {self.consecutive_bot_messages_count})。"
            )
            self.consecutive_bot_messages_count = 0