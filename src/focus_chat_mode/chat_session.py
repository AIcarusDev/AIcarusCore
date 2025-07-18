# src/focus_chat_mode/chat_session.py
# 聊天会话模块，负责处理单个会话的逻辑，包括消息存储、行为指导等。
import asyncio
import time
from typing import TYPE_CHECKING, Any

from src.config import config
from src.action.action_handler import ActionHandler
from src.common.custom_logging.logging_config import get_logger
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
    """管理单个专注聊天会话的状态和逻辑.

    这个类负责处理会话的生命周期、消息存储、行为指导生成等功能。

    Attributes:
        conversation_id (str): 会话的唯一标识符.
        llm_client (LLMProcessorClient): LLM处理器客户端，用于与LLM交互.
        event_storage (EventStorageService): 事件存储服务，用于存储和检索事件数据.
        action_handler (ActionHandler): 动作处理器，用于执行各种动作和获取数据.
        bot_id (str): 祂的唯一标识符.
        platform (str): 平台标识符.
        conversation_type (str): 会话类型（如群聊或私聊）.
        conversation_name (str | None): 会话名称（如果适用）.
        core_logic (CoreLogicFlow): 核心逻辑处理器，用于处理会话的核心逻辑.
        chat_session_manager (ChatSessionManager): 聊天会话管理器，用于管理多个会话实例.
        conversation_service (ConversationStorageService): 会话存储服务，用于持久化会话数据.
        summarization_service (SummarizationService): 摘要服务，用于生成会话摘要.
        summary_storage_service (SummaryStorageService): 摘要存储服务，用于持久化会话摘要.
        internal_info_builder (InternalInfoBuilder): 内部信息构建器，用于生成内部状态信息块.
        intelligent_interrupter (IntelligentInterrupter): 智能中断系统，
            用于处理会话中的智能中断逻辑.
        thought_storage_service (ThoughtStorageService): 思考存储服务，用于存储和检索思考数据.
        is_active (bool): 会话是否处于活动状态.
        last_active_time (float): 上次活动的时间戳.
        last_processed_timestamp (float): 上次处理的时间戳，用于跟踪新事件.
        last_llm_decision (dict[str, Any] | None): 上次LLM决策的结果.
        interrupting_event_doc (dict | None): 用于存储中断事件的文档.
        sent_actions_context (OrderedDict[str, dict[str, Any]]): 已发送动作的上下文信息.
        messages_planned_this_turn (int): 本轮计划发送的消息数量.
        messages_sent_this_turn (int): 本轮实际发送的消息数量.
        background_tasks (set[asyncio.Task]): 背景任务集合，用于管理异步任务.
        is_first_turn_for_session (bool): 是否为会话的第一轮思考.
        initial_core_think (str | None): 初始核心思考内容.
        initial_core_mood (str | None): 初始核心心境内容.
        initial_core_motivation (str | None): 初始核心动机内容.
        current_handover_summary (str | None): 当前交接摘要内容.
        events_since_last_summary (list[dict[str, Any]]): 自上次摘要以来的事件列表.
        message_count_since_last_summary (int): 自上次摘要以来的消息计数.
        no_action_count (int): 连续未采取行动的计数.
        consecutive_bot_messages_count (int): 连续收到祂的消息计数.
        bot_profile_cache (dict[str, Any]): 祂的档案缓存，用于快速访问。
        last_profile_update_time (float): 上次更新祂档案的时间戳.
        conversation_details_cache (dict[str, Any]): 会话详情缓存，用于快速访问。
        last_details_update_time (float): 上次更新会话详情的时间戳.
        SUMMARY_INTERVAL (int): 摘要生成的时间间隔，单位为分钟.
    """

    # TODO: 优化依赖注入
    # 这个__init__ 方法已经成为依赖地狱
    # 理想情况下，应该用一个“服务容器”来管理这些依赖，而不是全塞进来。
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
        """初始化聊天会话.

        Args:
            conversation_id (str): 会话的唯一标识符.
            llm_client (LLMProcessorClient): LLM处理器客户端，用于与LLM交互.
            event_storage (EventStorageService): 事件存储服务，用于存储和检索事件数据.
            action_handler (ActionHandler): 动作处理器，用于执行各种动作和获取数据.
            bot_id (str): 祂的唯一标识符.
            platform (str): 平台标识符.
            conversation_type (str): 会话类型.
            core_logic (CoreLogicFlow): 核心逻辑处理器.
            chat_session_manager (ChatSessionManager): 聊天会话管理器.
            conversation_service (ConversationStorageService): 会话存储服务.
            summarization_service (SummarizationService): 摘要服务.
            summary_storage_service (SummaryStorageService): 摘要存储服务.
            internal_info_builder (InternalInfoBuilder): 内部信息构建器，用于生成内部状态信息块.
            intelligent_interrupter (IntelligentInterrupter): 智能中断系统，
                用于处理会话中的智能中断逻辑.
            thought_storage_service (ThoughtStorageService): 思考存储服务，用于存储和检索思考数据.
        """
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
        self.interrupt_signal = asyncio.Event()
        """一个专属于此会话的中断信号旗。当被设置时，表示一个高优先级中断已发生。"""
        self._interrupt_checker_task: asyncio.Task | None = None
        """存储常驻的后台中断检查任务。"""
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
        self.processing_lock = asyncio.Lock()
        self.background_tasks: set[asyncio.Task] = set()
        self._echo_events: dict[str, asyncio.Event] = {}
        """存储正在等待回声的动作ID及其对应的唤醒事件。"""
        self._echo_lock = asyncio.Lock()
        """用于保护对 _echo_events 字典的并发访问。"""

        # --- 行为计数器 ---
        self.no_action_count: int = 0
        self.consecutive_bot_messages_count: int = 0

        # --- 中断相关 ---
        self.interruption_context: dict | None = None
        """当中断发生时，用于存储中断现场的详细信息。"""

        self.messages_planned_this_turn: int = 0
        """LLM在本轮思考中，计划要发送的消息总数。"""

        self.messages_sent_this_turn: int = 0
        """在本轮中，已经成功发送了的消息数量。"""

        # --- 上下文与记忆属性 ---
        self.pending_handover_result: dict | None = None
        self.is_first_turn_for_session: bool = True
        self.initial_core_think: str | None = None
        self.initial_core_mood: str | None = None
        self.initial_core_motivation: str | None = None
        self.current_handover_summary: str | None = None

        # --- 缓存 ---
        self.bot_profile_cache: dict[str, Any] = {}
        self.last_profile_update_time: float = 0.0
        self.conversation_details_cache: dict[str, Any] = {}
        self.last_details_update_time: float = 0.0

        logger.info(f"[ChatSession][{self.conversation_id}] 实例已创建，依赖已注入。")

    def record_action_result(self, decision_json: dict) -> None:
        """根据LLM的决策结果，记录并更新本会话的状态计数器."""
        if not decision_json:
            return
        action_planned = "action" in decision_json and decision_json["action"]
        if action_planned:
            if self.no_action_count > 0:
                logger.debug(
                    f"[{self.conversation_id}] AI计划行动，no_action_count "
                    f"从 {self.no_action_count} 重置为 0。"
                )
                self.no_action_count = 0
        else:
            self.no_action_count += 1
            logger.debug(
                f"[{self.conversation_id}] AI决定不行动，"
                f"no_action_count 增加为: {self.no_action_count}"
            )


    async def update_counters_on_new_events(self) -> None:
        """根据新消息重置计数器."""
        new_events = await self.event_storage.get_message_events_after_timestamp(
            self.conversation_id, self.last_processed_timestamp
        )
        if not new_events:
            return

        bot_profile = await self.get_bot_profile()
        bot_id = str(bot_profile.get("user_id", self.bot_id))

        # 使用 any() 表达式，更简洁、高效
        other_user_spoke = any(
            event.get("user_info", {}).get("user_id")
            and str(event.get("user_info", {}).get("user_id")) != bot_id
            for event in new_events
        )

        if other_user_spoke:
            if self.consecutive_bot_messages_count > 0:
                logger.debug(
                    f"[{self.conversation_id}] 检测到他人新消息，"
                    f"重置 consecutive_bot_messages_count "
                    f"(之前是 {self.consecutive_bot_messages_count})。"
                )
                self.consecutive_bot_messages_count = 0
            if self.no_action_count > 0:
                logger.debug(
                    f"[{self.conversation_id}] 检测到新消息，"
                    f"重置 no_action_count (之前是 {self.no_action_count})。"
                )
                self.no_action_count = 0

    async def wait_for_echo(self, action_id: str, timeout: float = 20.0) -> bool:
        """为指定的 action_id 等待一个回声。"""
        wake_up_event = asyncio.Event()
        async with self._echo_lock:
            self._echo_events[action_id] = wake_up_event

        logger.info(f"[{self.conversation_id}] 动作 '{action_id}' 已进入回声等待室，等待适配器回音...")

        try:
            await asyncio.wait_for(wake_up_event.wait(), timeout=timeout)
            logger.success(f"[{self.conversation_id}] 动作 '{action_id}' 已收到回声！")
            return True
        except asyncio.TimeoutError:
            logger.warning(f"[{self.conversation_id}] 等待动作 '{action_id}' 的回声超时！")
            return False
        finally:
            async with self._echo_lock:
                self._echo_events.pop(action_id, None)

    async def signal_echo_received(self, action_id: str) -> None:
        """由 MessageProcessor 调用，通知一个回声已经到达。"""
        async with self._echo_lock:
            if event_to_wake := self._echo_events.get(action_id):
                event_to_wake.set()
                logger.debug(f"[{self.conversation_id}] 已为动作 '{action_id}' 发出唤醒信号。")

    async def get_bot_profile(self) -> dict[str, Any]:
        """智能获取祂的档案，优先使用缓存，再查数据库."""
        if self.bot_profile_cache and (
            time.time() - self.last_profile_update_time < CACHE_EXPIRATION_SECONDS
        ):
            return self.bot_profile_cache

        conv_doc = await self.conversation_service.get_conversation_document_by_id(
            self.conversation_id
        )
        if conv_doc and (db_profile := conv_doc.get("bot_profile_in_this_conversation")):
            self.bot_profile_cache = db_profile
            self.last_profile_update_time = time.time()
            logger.debug(f"[{self.conversation_id}] 从数据库加载了祂的档案并放入缓存。")
            return self.bot_profile_cache

        logger.warning(
            f"[{self.conversation_id}] 缓存和数据库中均未找到祂有效的档案。"
            f"将使用初始化时提供的 ID '{self.bot_id}' 构建一个临时的基础档案。"
        )
        return {"user_id": self.bot_id, "nickname": config.persona.bot_name, "card": config.persona.bot_name}

    async def get_conversation_details(self) -> dict[str, Any]:
        """智能获取会话的详细信息，比如成员数（带缓存）."""
        if self.conversation_details_cache and (
            time.time() - self.last_details_update_time
            < CONVERSATION_DETAILS_CACHE_EXPIRATION_SECONDS
        ):
            return self.conversation_details_cache

        logger.info(f"[{self.conversation_id}] 会话详情缓存失效或不存在，向适配器查询。")
        success, result_payload = await self.action_handler.execute_simple_action(
            platform_id=self.platform,
            action_name="get_group_info",
            params={"group_id": self.conversation_id},
            description="专注模式：获取群聊详情",
        )

        details = None
        if success and isinstance(result_payload, dict) and not result_payload.get("error"):
            details = result_payload
        else:
            logger.error(f"[{self.conversation_id}] 获取群聊详情失败: {result_payload}")

        if details:
            self.conversation_details_cache = details
            self.last_details_update_time = time.time()
            return details

        return self.conversation_details_cache or {}

    def start_interrupt_checker(self) -> None:
        """启动常驻的后台中断检查任务。"""
        if self._interrupt_checker_task and not self._interrupt_checker_task.done():
            logger.warning(f"[{self.conversation_id}] 尝试启动中断检查器，但它已在运行。")
            return

        logger.info(f"[{self.conversation_id}] 启动常驻后台中断检查哨兵...")
        # 我们将 core_logic 中的 _check_for_interruptions 逻辑绑定到这个会话实例上
        # 注意：这里我们直接把 self (ChatSession 实例) 传给了检查器
        self._interrupt_checker_task = asyncio.create_task(
            self.core_logic._check_for_interruptions_task(self)
        )
        # 添加一个回调，以便在任务意外结束时记录日志
        self._interrupt_checker_task.add_done_callback(self._on_interrupt_checker_done)

    async def stop_interrupt_checker(self) -> None:
        """停止常驻的后台中断检查任务。"""
        if self._interrupt_checker_task and not self._interrupt_checker_task.done():
            logger.info(f"[{self.conversation_id}] 停止常驻后台中断检查哨兵...")
            self._interrupt_checker_task.cancel()
            try:
                await self._interrupt_checker_task
            except asyncio.CancelledError:
                pass # 正常取消
            logger.info(f"[{self.conversation_id}] 中断检查哨兵已停止。")
        self._interrupt_checker_task = None

    def _on_interrupt_checker_done(self, task: asyncio.Task) -> None:
        """中断检查任务结束时的回调。"""
        try:
            task.result() # 检查是否有异常
        except asyncio.CancelledError:
            pass # 正常取消，无需记录
        except Exception as e:
            logger.error(
                f"[{self.conversation_id}] 后台中断检查任务意外终止: {e}",
                exc_info=e
            )
