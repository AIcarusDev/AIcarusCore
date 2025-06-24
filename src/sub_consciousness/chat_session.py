import asyncio
import time
from collections import OrderedDict
from typing import TYPE_CHECKING, Any

from src.action.action_handler import ActionHandler
from src.common.custom_logging.logger_manager import get_logger
from src.config import config
from src.database.services.event_storage_service import EventStorageService
from src.llmrequest.llm_processor import Client as LLMProcessorClient

from .chat_prompt_builder import ChatPromptBuilder
from .focus_chat_cycler import FocusChatCycler

if TYPE_CHECKING:
    from src.core_logic.consciousness_flow import CoreLogic as CoreLogicFlow
    from src.database.services.summary_storage_service import SummaryStorageService
    from src.observation.summarization_service import SummarizationService
    from src.sub_consciousness.chat_session_manager import ChatSessionManager

logger = get_logger(__name__)


class ChatSession:
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
        summarization_service: "SummarizationService",
        summary_storage_service: "SummaryStorageService",
    ) -> None:
        self.conversation_id: str = conversation_id
        self.llm_client: LLMProcessorClient = llm_client
        self.event_storage: EventStorageService = event_storage
        self.action_handler: ActionHandler = action_handler
        self.bot_id: str = bot_id
        self.platform: str = platform
        self.conversation_type: str = conversation_type
        self.core_logic = core_logic
        self.chat_session_manager = chat_session_manager
        self.summarization_service = summarization_service
        self.summary_storage_service = summary_storage_service
        self.is_active: bool = False
        self.last_active_time: float = 0.0
        self.last_processed_timestamp: float = 0.0
        self.last_llm_decision: dict[str, Any] | None = None
        self.sent_actions_context: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self.processing_lock = asyncio.Lock()
        self.is_first_turn_for_session: bool = True
        self.initial_core_think: str | None = None
        self.initial_core_mood: str | None = None
        self.current_handover_summary: str | None = None
        self.events_since_last_summary: list[dict[str, Any]] = []
        self.message_count_since_last_summary: int = 0
        self.SUMMARY_INTERVAL: int = getattr(config.sub_consciousness, "summary_interval", 5)
        self.no_action_count: int = 0
        self.prompt_builder = ChatPromptBuilder(
            event_storage=self.event_storage,
            action_handler=self.action_handler,
            bot_id=self.bot_id,
            platform=self.platform,
            conversation_id=self.conversation_id,
            conversation_type=self.conversation_type,
        )
        self.cycler = FocusChatCycler(self)
        logger.info(f"[ChatSession][{self.conversation_id}] 实例已创建。")

    def activate(self, core_last_think: str | None = None, core_last_mood: str | None = None) -> None:
        """激活会话并启动其主动循环。"""
        if self.is_active:
            self.initial_core_think = core_last_think
            self.initial_core_mood = core_last_mood
            self.is_first_turn_for_session = True
            logger.info(f"[ChatSession][{self.conversation_id}] 会话已激活，重置思考和心情上下文。")
            return
        self.is_active = True
        self.is_first_turn_for_session = True
        self.initial_core_think = core_last_think
        self.initial_core_mood = core_last_mood
        self.last_active_time = time.time()
        self.current_handover_summary = None
        self.events_since_last_summary = []
        self.message_count_since_last_summary = 0
        logger.info(
            f"[ChatSession][{self.conversation_id}] 已激活。首次处理: {self.is_first_turn_for_session}, "
            f"主意识想法: '{core_last_think}'."
        )
        asyncio.create_task(self.cycler.start())

    def deactivate(self) -> None:
        """停用会话并触发其关闭流程（不等待）。"""
        if not self.is_active:
            return
        logger.info(f"[ChatSession][{self.conversation_id}] 正在停用...")
        self.is_active = False
        # 触发关闭，但不阻塞当前同步的 deactivation 流程
        asyncio.create_task(self.shutdown())
        self.last_llm_decision = None
        self.last_processed_timestamp = 0.0
        logger.info(f"[ChatSession][{self.conversation_id}] 停用状态已设置，关闭任务已派发。")

    async def shutdown(self) -> None:
        """
        执行并等待会话的优雅关闭。
        这是被 ChatSessionManager 调用的主要关闭入口。
        """
        if self.cycler:
            await self.cycler.shutdown()
        self.is_active = False  # 再次确认状态
        logger.info(f"[ChatSession][{self.conversation_id}] 已成功关闭。")
