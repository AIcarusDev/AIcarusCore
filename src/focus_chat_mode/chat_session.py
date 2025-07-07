# src/focus_chat_mode/chat_session.py
# 聊天会话模块，负责处理单个会话的逻辑，包括消息存储、行为指导等。
import asyncio
import time
from collections import OrderedDict
from typing import TYPE_CHECKING, Any

from src.action.action_handler import ActionHandler
from src.common.custom_logging.logging_config import get_logger
from src.config import config
from src.database import ConversationStorageService
from src.database.services.event_storage_service import EventStorageService
from src.database.services.thought_storage_service import ThoughtStorageService  # 哼，新来的！
from src.llmrequest.llm_processor import Client as LLMProcessorClient

from .action_executor import ActionExecutor
from .behavioral_guidance_generator import BehavioralGuidanceGenerator
from .chat_prompt_builder import ChatPromptBuilder
from .focus_chat_cycler import FocusChatCycler
from .llm_response_handler import LLMResponseHandler
from .summarization_manager import SummarizationManager

if TYPE_CHECKING:
    from src.common.intelligent_interrupt_system.intelligent_interrupter import (
        IntelligentInterrupter,
    )
    from src.common.summarization_observation.summarization_service import SummarizationService
    from src.core_logic.consciousness_flow import CoreLogic as CoreLogicFlow
    from src.database.services.summary_storage_service import SummaryStorageService
    from src.focus_chat_mode.chat_session_manager import ChatSessionManager

CACHE_EXPIRATION_SECONDS = 600
CONVERSATION_DETAILS_CACHE_EXPIRATION_SECONDS = 7200  # 2小时

logger = get_logger(__name__)


class ChatSession:
    """管理单个专注聊天会话的状态和逻辑."""

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
        thought_storage_service: ThoughtStorageService,  # 哼，新来的！
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
        self.thought_storage_service: ThoughtStorageService = (
            thought_storage_service  # 哼，新来的！
        )

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
        self.messages_planned_this_turn: int = 0  # 计划发几条
        self.messages_sent_this_turn: int = 0  # 实际发了机条

        # --- 上下文和记忆属性 ---
        self.is_first_turn_for_session: bool = True
        self.initial_core_think: str | None = None
        self.initial_core_mood: str | None = None
        self.initial_core_motivation: str | None = None
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

    # // 这就是我们新的“情报获取术”，喵~ 【小色猫·直捣黄龙·最终版】
    async def get_conversation_details(self) -> dict[str, Any]:
        """智能获取会话的详细信息，比如成员数。
        有缓存就用缓存，没有或者过期了就去问，懒得每次都问。
        哼，这次我直接告诉 ActionHandler 我要用哪个姿势，一步到胃！
        """
        # 1. 先看看脑子里有没有，并且还没发霉 (这部分逻辑不变，缓存是好文明！)
        if self.conversation_details_cache and (
            time.time() - self.last_details_update_time
            < CONVERSATION_DETAILS_CACHE_EXPIRATION_SECONDS
        ):
            logger.debug(f"[{self.conversation_id}] 使用缓存的会话详情。")
            return self.conversation_details_cache

        # 2. 没办法了，只能去问适配器了，真麻烦
        logger.info(f"[{self.conversation_id}] 会话详情缓存失效或不存在，向适配器查询。")

        # --- ❤❤❤ 欲望喷射点：这里是手术的核心！❤❤❤ ---
        # 我不再幻想那个不存在的动作了！
        # 我要直接调用 ActionHandler 里那个更简单、更直接的通道！
        # 这个通道允许我直接指定平台、动作名和参数，就像点菜一样！
        success, result_payload = await self.action_handler.execute_simple_action(
            platform_id=self.platform,  # 明确告诉它，我要玩哪个平台的！(e.g., 'napcat_qq')
            action_name="get_group_info",  # 明确告诉它，我要用哪个姿势！(e.g., 'get_group_info')
            params={"group_id": self.conversation_id},  # 把需要的“玩具”（参数）递过去！
            description="专注模式：获取群聊详情",  # 给这次“爱爱”起个名字，方便查日志
        )
        # --- ❤❤❤ 手术结束，完美！❤❤❤ ---

        details = None
        if success:
            # execute_simple_action 成功后，它的 payload 就是我们想要的数据
            # 但要注意，它返回的 payload 可能包含 error 键，也可能直接就是数据
            if isinstance(result_payload, dict) and not result_payload.get("error"):
                details = result_payload
            elif isinstance(result_payload, str):
                logger.warning(
                    f"[{self.conversation_id}] execute_simple_action 成功，但返回的是字符串消息: '{result_payload}'，而不是详情字典。"
                )
        else:
            # 如果不成功，result_payload 就是错误信息字符串
            logger.error(
                f"[{self.conversation_id}] 通过 execute_simple_action 获取群聊详情失败: {result_payload}"
            )

        if details:
            # 问到了！赶紧记下来！
            self.conversation_details_cache = details
            self.last_details_update_time = time.time()
            logger.debug(f"[{self.conversation_id}] 已从适配器获取并缓存了新的会话详情: {details}")
            return details

        # 如果连问都问不到，就用旧的缓存（总比没有好）
        logger.warning(
            f"[{self.conversation_id}] 无法获取新的会话详情，将使用旧的缓存（如果存在）。"
        )
        return self.conversation_details_cache or {}

    async def update_counters_on_new_events(self) -> None:
        """根据新消息重置计数器。
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
        """智能获取机器人档案，优先使用缓存，再查数据库。
        哼，这才叫高效的懒！【小色猫最终治愈版】
        """
        # 1. 检查短期记忆（内存缓存）是否有效
        if self.bot_profile_cache and (
            time.time() - self.last_profile_update_time < CACHE_EXPIRATION_SECONDS
        ):
            logger.debug(f"[{self.conversation_id}] 使用内存缓存的机器人档案。")
            return self.bot_profile_cache

        # 2. 尝试从长期记忆（数据库）加载
        #    这是我们最可靠的信息来源，由“上线安检”和“档案更新通知”来维护
        # TODO: 优化缓存机制，全部改为直接从数据库中读取
        conv_doc = await self.conversation_service.get_conversation_document_by_id(
            self.conversation_id
        )
        if conv_doc and conv_doc.get("bot_profile_in_this_conversation"):
            db_profile = conv_doc["bot_profile_in_this_conversation"]
            if db_profile:
                self.bot_profile_cache = db_profile
                self.last_profile_update_time = time.time()
                logger.debug(f"[{self.conversation_id}] 从数据库加载了机器人档案并放入缓存。")
                return self.bot_profile_cache

        # --- ❤❤❤ 终极切除手术 ❤❤❤ ---
        # 3. 删掉那个多余又危险的“主动询问适配器”的逻辑！
        #    我们不再发起那个该死的 action.bot.get_profile 请求了！
        #    如果缓存和数据库都没有，我们就优雅地承认失败，而不是傻等30秒！
        #    这样，我们的主循环就再也不会被这种破事阻塞了！
        logger.warning(
            f"[{self.conversation_id}] 缓存和数据库中均未找到有效的机器人档案。"
            "将使用一个空的档案作为后备，等待适配器通过 'notice.bot.profile_update' 事件来更新。"
        )
        # 返回一个空的字典，让调用方能安全地 .get()，而不会崩溃
        return {}

    def activate(
        self,
        core_motivation: str | None = None,  # // 只接收动机
    ) -> None:
        """激活会话并启动其主动循环."""
        if self.is_active:
            self.is_first_turn_for_session = True
            self.initial_core_motivation = core_motivation
            logger.info(
                f"[ChatSession][{self.conversation_id}] 会话已激活，但收到新的激活指令，重置为第一轮思考。"
            )
            self.cycler.wakeup()  # 唤醒循环，让它立刻开始
            return

        self.is_active = True
        self.is_first_turn_for_session = True
        self.initial_core_think = None
        self.initial_core_mood = None
        self.initial_core_motivation = core_motivation
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
            f"激活动机: '{core_motivation}'."
        )
        asyncio.create_task(self.cycler.start())

    def deactivate(self) -> None:
        """发起停用流程。
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
        """执行并等待会话的优雅关闭。
        由 deactivate 触发，或者在 cycler 结束后调用。
        """
        if not self.is_active and not self.cycler._loop_active:
            logger.debug(f"[{self.conversation_id}] 会话已处于非活动状态，shutdown 操作被跳过。")
            return
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
