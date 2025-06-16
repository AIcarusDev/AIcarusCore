import asyncio
import json
import random
import re
import time
import uuid
from collections import OrderedDict
from typing import TYPE_CHECKING, Any, Optional

from aicarus_protocols.conversation_info import ConversationInfo
from aicarus_protocols.event import Event
from aicarus_protocols.seg import SegBuilder
from aicarus_protocols.user_info import UserInfo

from src.action.action_handler import ActionHandler
from src.common.custom_logging.logger_manager import get_logger
from src.common.text_splitter import process_llm_response
from src.config import config
from src.database.services.event_storage_service import EventStorageService
from src.llmrequest.llm_processor import Client as LLMProcessorClient

from .chat_prompt_builder import ChatPromptBuilder
from .focus_chat_cycler import FocusChatCycler

if TYPE_CHECKING:
    from src.core_logic.consciousness_flow import CoreLogic as CoreLogicFlow
    from src.core_logic.summarization_service import SummarizationService
    from src.sub_consciousness.chat_session_manager import ChatSessionManager

logger = get_logger(__name__)

# Templates are now in ChatPromptBuilder


class ChatSession:  # Renamed class
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
    ) -> None:
        self.conversation_id: str = conversation_id
        self.llm_client: LLMProcessorClient = llm_client
        self.event_storage: EventStorageService = event_storage
        self.action_handler: ActionHandler = action_handler
        self.bot_id: str = bot_id
        self.platform: str = platform
        self.conversation_type: str = conversation_type

        # 新增的依赖实例
        self.core_logic = core_logic
        self.chat_session_manager = chat_session_manager
        self.summarization_service = summarization_service

        self.is_active: bool = False
        self.last_active_time: float = 0.0
        self.last_processed_timestamp: float = 0.0  # 记录本会话处理到的最新消息时间戳
        self.last_llm_decision: dict[str, Any] | None = None
        self.sent_actions_context: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self.processing_lock = asyncio.Lock()

        # 新增状态，用于首次构建 prompt
        self.is_first_turn_for_session: bool = True
        self.initial_core_think: str | None = None

        # 新增状态，用于渐进式总结
        self.current_handover_summary: str | None = None
        self.events_since_last_summary: list[dict[str, Any]] = []
        self.message_count_since_last_summary: int = 0
        self.SUMMARY_INTERVAL: int = getattr(config.sub_consciousness, "summary_interval", 5)  # 从配置读取或默认5条消息

        # 新增：用于引导LLM在无互动时主动退出的计数器
        self.no_action_count: int = 0

        # Instantiate the prompt builder
        # Passing self.bot_id to the builder
        self.prompt_builder = ChatPromptBuilder(
            event_storage=self.event_storage, bot_id=self.bot_id, conversation_id=self.conversation_id
        )
        
        # 新增：集成 Cycler
        self.cycler = FocusChatCycler(self)
        
        logger.info(f"[ChatSession][{self.conversation_id}] 实例已创建。")

    def activate(self, core_last_think: str | None = None) -> None:
        """激活会话并启动其主动循环。"""
        if self.is_active:
            # 如果已经激活，可能只是更新思考上下文
            self.initial_core_think = core_last_think
            self.is_first_turn_for_session = True # 重新进入时，视为新周期的第一轮
            logger.info(f"[ChatSession][{self.conversation_id}] 会话已激活，重置思考上下文。")
            return

        self.is_active = True
        self.is_first_turn_for_session = True
        self.initial_core_think = core_last_think
        self.last_active_time = time.time()

        # 重置状态
        self.current_handover_summary = None
        self.events_since_last_summary = []
        self.message_count_since_last_summary = 0
        
        logger.info(
            f"[ChatSession][{self.conversation_id}] 已激活。首次处理: {self.is_first_turn_for_session}, "
            f"主意识想法: '{core_last_think}'."
        )
        
        # 启动循环
        asyncio.create_task(self.cycler.start())

    def deactivate(self) -> None:
        """停用会话并关闭其主动循环。"""
        if not self.is_active:
            return
            
        self.is_active = False
        logger.info(f"[ChatSession][{self.conversation_id}] 正在停用...")
        
        # 关闭循环
        asyncio.create_task(self.cycler.shutdown())
        
        self.last_llm_decision = None
        self.last_processed_timestamp = 0.0
        logger.info(f"[ChatSession][{self.conversation_id}] 已停用。")
