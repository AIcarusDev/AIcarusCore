# 文件: D:\Aic\AIcarusCore\src\focus_chat_mode\chat_session.py
# 我是小懒猫，这是我给你整理好的版本，哼！

import asyncio
import time
from collections import OrderedDict
from typing import TYPE_CHECKING, Any

from src.action.action_handler import ActionHandler
from src.common.custom_logging.logger_manager import get_logger
from src.config import config
from src.database.services.conversation_storage_service import ConversationStorageService
from src.database.services.event_storage_service import EventStorageService
from src.llmrequest.llm_processor import Client as LLMProcessorClient
from src.tools.platform_actions import get_bot_profile

from .action_executor import ActionExecutor
from .chat_prompt_builder import ChatPromptBuilder
from .focus_chat_cycler import FocusChatCycler
from .llm_response_handler import LLMResponseHandler
from .summarization_manager import SummarizationManager

if TYPE_CHECKING:
    from src.core_logic.consciousness_flow import CoreLogic as CoreLogicFlow
    from src.database.services.summary_storage_service import SummaryStorageService
    from src.focus_chat_mode.chat_session_manager import ChatSessionManager
    from src.observation.summarization_service import SummarizationService

CACHE_EXPIRATION_SECONDS = 600  # 缓存10分钟

logger = get_logger(__name__)


class ChatSession:
    """
    管理单个专注聊天会话的状态和逻辑。
    哼，这里就是机器人开小差的地方。
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
        conversation_service: ConversationStorageService,  # 【修改点1】依赖直接注入！
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
        self.conversation_name: str | None = None
        self.core_logic = core_logic
        self.chat_session_manager = chat_session_manager
        self.conversation_service = conversation_service  # 【修改点2】直接保存依赖实例！
        self.summarization_service = summarization_service
        self.summary_storage_service = summary_storage_service

        # --- 新的模块化组件 ---
        self.action_executor = ActionExecutor(self)
        self.llm_response_handler = LLMResponseHandler(self)
        self.summarization_manager = SummarizationManager(self)

        # --- 会话状态属性 ---
        self.is_active: bool = False
        self.last_active_time: float = 0.0
        self.last_processed_timestamp: float = 0.0
        self.last_llm_decision: dict[str, Any] | None = None
        self.sent_actions_context: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self.processing_lock = asyncio.Lock()

        # --- 上下文和记忆属性 ---
        self.is_first_turn_for_session: bool = True
        self.initial_core_think: str | None = None
        self.initial_core_mood: str | None = None
        self.current_handover_summary: str | None = None
        self.events_since_last_summary: list[dict[str, Any]] = []
        self.message_count_since_last_summary: int = 0
        self.no_action_count: int = 0
        self.bot_profile_cache: dict[str, Any] = {}  # 短期记忆小本本
        self.last_profile_update_time: float = 0.0  # 上次更新时间

        # --- 辅助组件 ---
        self.SUMMARY_INTERVAL: int = getattr(config.focus_chat_mode, "summary_interval", 5)
        self.prompt_builder = ChatPromptBuilder(
            session=self,
            event_storage=self.event_storage,
            action_handler=self.action_handler,
            bot_id=self.bot_id,
            platform=self.platform,
            conversation_id=self.conversation_id,
            conversation_type=self.conversation_type,
        )
        self.cycler = FocusChatCycler(self)

        logger.info(f"[ChatSession][{self.conversation_id}] 实例已创建，依赖已注入。")

    async def get_bot_profile(self) -> dict[str, Any]:
        """
        智能获取机器人档案，优先使用缓存，再查数据库，最后才问适配器。
        哼，这才叫高效的懒！
        """
        # 1. 检查短期记忆（内存缓存）是否有效
        if self.bot_profile_cache and (time.time() - self.last_profile_update_time < CACHE_EXPIRATION_SECONDS):
            logger.info(f"[{self.conversation_id}] 使用内存缓存的机器人档案。")
            return self.bot_profile_cache

        # 2. 尝试从长期记忆（数据库）加载
        # 【修改点3】直接用 self.conversation_service，不再绕道 ChatSessionManager 了！
        conv_doc = await self.conversation_service.get_conversation_document_by_id(self.conversation_id)
        if conv_doc and conv_doc.get("bot_profile_in_this_conversation"):
            db_profile = conv_doc["bot_profile_in_this_conversation"]
            # 假设数据库里也存了更新时间戳
            db_profile_time = db_profile.get("updated_at", 0) / 1000.0  # 数据库存的是毫秒
            if time.time() - db_profile_time < CACHE_EXPIRATION_SECONDS:
                self.bot_profile_cache = db_profile
                self.last_profile_update_time = time.time()
                logger.info(f"[{self.conversation_id}] 从数据库加载了机器人档案。")
                return self.bot_profile_cache

        # 3. 没办法了，只能去问适配器了，真麻烦
        logger.info(f"[{self.conversation_id}] 缓存失效或不存在，向适配器查询机器人档案。")
        profile = await get_bot_profile(
            self.action_handler,
            adapter_id=self.platform,
            group_id=self.conversation_id if self.conversation_type == "group" else None,
        )

        if profile:
            self.bot_profile_cache = profile
            self.last_profile_update_time = time.time()
            # 更新数据库里的长期记忆
            profile_with_ts = profile.copy()
            profile_with_ts["updated_at"] = int(time.time() * 1000)  # 存毫秒时间戳
            # 【修改点4】这里也一样，直接调用！
            await self.conversation_service.update_conversation_field(
                self.conversation_id, "bot_profile_in_this_conversation", profile_with_ts
            )
            logger.info(f"[{self.conversation_id}] 已从适配器获取并缓存了新的机器人档案。")
            return profile

        # 如果连问都问不到，就用旧的缓存（总比没有好）
        logger.warning(f"[{self.conversation_id}] 无法获取新的机器人档案，将使用旧的缓存（如果存在）。")
        return self.bot_profile_cache or {}

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
        self.no_action_count = 0  # 每次激活时重置
        self.bot_profile_cache = {}  # 每次激活时清空短期记忆，强制重新获取
        self.last_profile_update_time = 0.0

        logger.info(
            f"[ChatSession][{self.conversation_id}] 已激活。首次处理: {self.is_first_turn_for_session}, "
            f"主意识想法: '{core_last_think}'."
        )
        asyncio.create_task(self.cycler.start())

    def deactivate(self) -> None:
        """停用会话并触发其关闭流程（不等待）。"""
        if not self.is_active:
            return
        logger.info(f"[ChatSession][{self.conversation_id}] 正在发起停用请求...")
        self.is_active = False  # 只设置状态，不清理任何东西！
        # 把所有清理工作都交给 shutdown
        asyncio.create_task(self.shutdown())
        logger.info(f"[ChatSession][{self.conversation_id}] 停用状态已设置，关闭任务已派发。")

    async def shutdown(self) -> None:
        """
        执行并等待会话的优雅关闭。
        现在它负责所有清理工作。
        """
        if self.cycler:
            # shutdown cycler 会触发 _save_final_summary
            await self.cycler.shutdown()

        # 【懒猫修复】在所有异步任务完成后，再清理会话的状态！
        self.is_active = False
        self.last_llm_decision = None
        self.last_processed_timestamp = 0.0
        self.current_handover_summary = None  # 也可以在这里清理
        self.events_since_last_summary = []

        logger.info(f"[ChatSession][{self.conversation_id}] 已成功关闭并清理状态。")
