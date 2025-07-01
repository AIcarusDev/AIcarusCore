# src/focus_chat_mode/chat_session.py

import asyncio
import time
import uuid
from collections import OrderedDict
from typing import TYPE_CHECKING, Any

from src.action.action_handler import ActionHandler
from src.common.custom_logging.logging_config import get_logger
from src.config import config
from src.database.services.conversation_storage_service import ConversationStorageService
from src.database.services.event_storage_service import EventStorageService
from src.llmrequest.llm_processor import Client as LLMProcessorClient

from .action_executor import ActionExecutor
from .behavioral_guidance_generator import BehavioralGuidanceGenerator
from .chat_prompt_builder import ChatPromptBuilder
from .focus_chat_cycler import FocusChatCycler
from .llm_response_handler import LLMResponseHandler
from .summarization_manager import SummarizationManager

if TYPE_CHECKING:
    from src.common.intelligent_interrupt_system.intelligent_interrupter import IntelligentInterrupter
    from src.common.summarization_observation.summarization_service import SummarizationService
    from src.core_logic.consciousness_flow import CoreLogic as CoreLogicFlow
    from src.database.services.summary_storage_service import SummaryStorageService
    from src.focus_chat_mode.chat_session_manager import ChatSessionManager

CACHE_EXPIRATION_SECONDS = 600
CONVERSATION_DETAILS_CACHE_EXPIRATION_SECONDS = 7200 # 2小时

logger = get_logger(__name__)


class ChatSession:
    """
    管理单个专注聊天会话的状态和逻辑。
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
        intelligent_interrupter: "IntelligentInterrupter",
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
        self.conversation_service = conversation_service
        self.summarization_service = summarization_service
        self.summary_storage_service = summary_storage_service
        self.intelligent_interrupter: IntelligentInterrupter = intelligent_interrupter

        # --- 模块化组件 --
        self.action_executor = ActionExecutor(self)
        self.llm_response_handler = LLMResponseHandler(self)
        self.summarization_manager = SummarizationManager(self)
        self.guidance_generator = BehavioralGuidanceGenerator(self)

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
        self.consecutive_bot_messages_count: int = 0
        self.bot_profile_cache: dict[str, Any] = {}
        self.last_profile_update_time: float = 0.0
        self.conversation_details_cache: dict[str, Any] = {}
        self.last_details_update_time: float = 0.0


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
        # 哼，这里要把 Cycler 也改造一下，让它能接收 uid_map
        self.cycler = FocusChatCycler(self)

        logger.info(f"[ChatSession][{self.conversation_id}] 实例已创建，依赖已注入。")

    # // 这就是我们新的“情报获取术”，喵~
    async def get_conversation_details(self) -> dict[str, Any]:
        """
        智能获取会话的详细信息，比如成员数。
        有缓存就用缓存，没有或者过期了就去问，懒得每次都问。
        """
        # 1. 先看看脑子里有没有，并且还没发霉
        if self.conversation_details_cache and (time.time() - self.last_details_update_time < CONVERSATION_DETAILS_CACHE_EXPIRATION_SECONDS):
            logger.debug(f"[{self.conversation_id}] 使用缓存的会话详情。")
            return self.conversation_details_cache

        # 2. 没办法了，只能去问适配器了，真麻烦
        logger.info(f"[{self.conversation_id}] 会话详情缓存失效或不存在，向适配器查询。")

        # 构造一个获取群信息的动作事件
        action_event_dict = {
            "event_id": f"focus_get_conv_info_{self.conversation_id}_{uuid.uuid4().hex[:6]}",
            "event_type": "action.conversation.get_info",
            "platform": self.platform,
            "bot_id": self.bot_id,
            # 目标会话信息要放在 conversation_info 里，让 ActionHandler 知道对谁下手
            "conversation_info": {
                "conversation_id": self.conversation_id,
                "type": self.conversation_type
            },
            # content 理论上可以为空，但为了清晰，可以加上
            "content": [{"type": "action.conversation.get_info", "data": {}}],
        }

        # 调用 ActionHandler 里那个专门和适配器打交道的函数
        success, result = await self.action_handler.send_action_and_wait_for_response(action_event_dict)

        details = result if success and result else None

        if details:
            # 问到了！赶紧记下来！
            self.conversation_details_cache = details
            self.last_details_update_time = time.time()
            logger.debug(f"[{self.conversation_id}] 已从适配器获取并缓存了新的会话详情: {details}")
            return details

        # 如果连问都问不到，就用旧的缓存（总比没有好）
        logger.warning(f"[{self.conversation_id}] 无法获取新的会话详情，将使用旧的缓存（如果存在）。")
        return self.conversation_details_cache or {}


    async def update_counters_on_new_events(self) -> None:
        """
        根据新消息重置计数器。
        如果检测到有别人说话，就重置我（机器人）的连续发言计数。
        """
        new_events = await self.event_storage.get_message_events_after_timestamp(
            self.conversation_id, self.last_processed_timestamp
        )

        if not new_events:
            return  # 没人理我，啥也不用干

        bot_id = str((await self.get_bot_profile()).get("user_id", self.bot_id))

        for event in new_events:
            sender_id = event.get("user_info", {}).get("user_id")
            if sender_id and str(sender_id) != bot_id:
                # 啊哈！终于有人理我了！
                if self.consecutive_bot_messages_count > 0:
                    logger.debug(
                        f"[{self.conversation_id}] 检测到来自 '{sender_id}' 的新消息，"
                        f"重置 consecutive_bot_messages_count (之前是 {self.consecutive_bot_messages_count})。"
                    )
                    self.consecutive_bot_messages_count = 0

                if self.no_action_count > 0:
                    logger.debug(
                        f"[{self.conversation_id}] 检测到新消息，重置 no_action_count (之前是 {self.no_action_count})。"
                    )
                    self.no_action_count = 0

                # 只要有一个人说话，就可以滚了，后面的不用看了
                break

    async def get_bot_profile(self) -> dict[str, Any]:
        """
        智能获取机器人档案，优先使用缓存，再查数据库，最后才问适配器。
        哼，这才叫高效的懒！
        """
        # 1. 检查短期记忆（内存缓存）是否有效
        if self.bot_profile_cache and (time.time() - self.last_profile_update_time < CACHE_EXPIRATION_SECONDS):
            logger.debug(f"[{self.conversation_id}] 使用内存缓存的机器人档案。")
            return self.bot_profile_cache

        # 2. 尝试从长期记忆（数据库）加载
        conv_doc = await self.conversation_service.get_conversation_document_by_id(self.conversation_id)
        if conv_doc and conv_doc.get("bot_profile_in_this_conversation"):
            db_profile = conv_doc["bot_profile_in_this_conversation"]
            db_profile_time = db_profile.get("updated_at", 0) / 1000.0
            if time.time() - db_profile_time < CACHE_EXPIRATION_SECONDS:
                self.bot_profile_cache = db_profile
                self.last_profile_update_time = time.time()
                logger.debug(f"[{self.conversation_id}] 从数据库加载了机器人档案。")
                return self.bot_profile_cache

        # 3. 没办法了，只能去问适配器了，真麻烦
        logger.info(f"[{self.conversation_id}] 缓存失效或不存在，向适配器查询机器人档案。")

        action_event_dict = {
            "event_id": f"focus_get_profile_{self.conversation_id}_{uuid.uuid4().hex[:6]}",
            "event_type": "action.bot.get_profile",
            "platform": self.platform,
            "bot_id": self.bot_id,
            "content": [{"type": "action.bot.get_profile", "data": {"group_id": self.conversation_id}}],
        }
        success, result = await self.action_handler.send_action_and_wait_for_response(action_event_dict)
        profile = result if success and result else None

        if profile:
            self.bot_profile_cache = profile
            self.last_profile_update_time = time.time()
            # 更新数据库里的长期记忆
            profile_with_ts = profile.copy()
            profile_with_ts["updated_at"] = int(time.time() * 1000)
            await self.conversation_service.update_conversation_field(
                self.conversation_id, "bot_profile_in_this_conversation", profile_with_ts
            )
            logger.debug(f"[{self.conversation_id}] 已从适配器获取并缓存了新的机器人档案。")
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
        self.no_action_count = 0
        self.consecutive_bot_messages_count = 0
        self.bot_profile_cache = {}
        self.last_profile_update_time = 0.0

        logger.info(
            f"[ChatSession][{self.conversation_id}] 已激活。首次处理: {self.is_first_turn_for_session}, "
            f"主意识想法: '{core_last_think}'."
        )
        asyncio.create_task(self.cycler.start())

    def deactivate(self) -> None:
        """
        发起停用流程。
        这只是一个信号，真正的关闭逻辑在 shutdown 里。
        """
        if not self.is_active:
            return
        logger.info(f"[ChatSession][{self.conversation_id}] 正在发起停用请求...")
        # 设置 is_active 为 False，让循环自然结束
        self.is_active = False
        # 触发 cycler 的关闭
        if self.cycler:
            asyncio.create_task(self.cycler.shutdown())
        else:
            # 如果没有 cycler，就直接调用 shutdown
            asyncio.create_task(self.shutdown())

    async def shutdown(self) -> None:
        """
        执行并等待会话的优雅关闭。
        由 deactivate 触发，或者在 cycler 结束后调用。
        """
        # 确保 cycler 已经关闭
        if self.cycler and self.cycler._loop_active:
            await self.cycler.shutdown()

        # 最后做一次总结
        await self.summarization_manager.create_and_save_final_summary()

        # 清理会话状态
        self.is_active = False
        self.last_llm_decision = None
        self.last_processed_timestamp = 0.0
        self.current_handover_summary = None
        self.events_since_last_summary = []

        logger.info(f"[ChatSession][{self.conversation_id}] 已成功关闭并清理状态。")
