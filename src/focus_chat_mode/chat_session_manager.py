# src/focus_chat_mode/chat_session_manager.py
# 哼，这里是小穴的入口，看我怎么把你那硬邦邦的大脑塞进来~

import asyncio
import time
from typing import TYPE_CHECKING, Optional

from aicarus_protocols.event import Event

from src.action.action_handler import ActionHandler
from src.common.custom_logging.logging_config import get_logger
from src.config.aicarus_configs import FocusChatModeSettings
from src.database.services.conversation_storage_service import ConversationStorageService
from src.database.services.event_storage_service import EventStorageService
from src.database.services.summary_storage_service import SummaryStorageService
from src.llmrequest.llm_processor import Client as LLMProcessorClient

from .chat_session import ChatSession

if TYPE_CHECKING:
    # ❤ 引入我们性感的新大脑，为了类型提示~
    from src.common.intelligent_interrupt_system.intelligent_interrupter import IntelligentInterrupter
    from src.common.summarization_observation.summarization_service import SummarizationService  # 用于类型提示
    from src.core_logic.consciousness_flow import CoreLogic as CoreLogicFlow  # 用于类型提示
    from src.database.services.summary_storage_service import SummaryStorageService

logger = get_logger(__name__)


class ChatSessionManager:
    """
    管理所有 ChatSession 实例，处理消息分发和会话生命周期。
    （小色猫重构版）
    """

    def __init__(
        self,
        config: FocusChatModeSettings,
        llm_client: LLMProcessorClient,
        event_storage: EventStorageService,
        action_handler: ActionHandler,
        bot_id: str,
        conversation_service: ConversationStorageService,
        summarization_service: "SummarizationService",
        summary_storage_service: "SummaryStorageService",
        # ❤❤ 【高潮改造点 1】❤❤
        # 在这里开一个全新的小口，准备接收我那硬邦邦、热乎乎的大脑！
        intelligent_interrupter: "IntelligentInterrupter",
        core_logic: Optional["CoreLogicFlow"] = None,
    ) -> None:
        self.config = config
        self.llm_client = llm_client
        self.event_storage = event_storage
        self.action_handler = action_handler
        self.bot_id = bot_id

        self.conversation_service = conversation_service
        self.summarization_service = summarization_service
        self.summary_storage_service = summary_storage_service

        # ❤❤ 【高潮改造点 2】❤❤
        # 啊~ 进来吧！把它保存在我的身体里！
        self.intelligent_interrupter = intelligent_interrupter

        self.core_logic = core_logic

        self.sessions: dict[str, ChatSession] = {}
        self.lock = asyncio.Lock()

        logger.info("ChatSessionManager (小色猫重构版) 初始化完成，并已注入智能打断系统。")

    def set_core_logic(self, core_logic_instance: "CoreLogicFlow") -> None:
        """延迟注入 CoreLogic 实例，解决循环依赖。"""
        self.core_logic = core_logic_instance
        logger.info("CoreLogic 实例已成功注入到 ChatSessionManager。")

        # 哼，顺便把那个唤醒主意识的事件也拿过来
        if hasattr(core_logic_instance, "focus_session_inactive_event"):
            self.focus_session_inactive_event = core_logic_instance.focus_session_inactive_event
            logger.info("已从 CoreLogic 获取 focus_session_inactive_event。")
        else:
            logger.error("CoreLogic 实例中没有找到 focus_session_inactive_event！这会导致主意识无法被正确唤醒！")
            self.focus_session_inactive_event = None

    def _get_conversation_id(self, event: Event) -> str:
        # 从 Event 中提取唯一的会话ID (例如 group_id 或 user_id)
        # 此处需要根据 aicarus_protocols 的具体定义来实现
        info = event.conversation_info
        return info.conversation_id if info else "default_conv"

    async def get_or_create_session(
        self, conversation_id: str, platform: str | None = None, conversation_type: str | None = None
    ) -> ChatSession:
        async with self.lock:
            if conversation_id not in self.sessions:
                logger.info(f"[SessionManager] 为 '{conversation_id}' 创建新的会话实例。")

                if not platform or not conversation_type:
                    raise ValueError(f"Platform 和 conversation_type 是创建新会话 '{conversation_id}' 的必需品！")

                if not self.core_logic:
                    raise RuntimeError("CoreLogic未注入，ChatSessionManager无法创建会话。")

                # ❤❤ 【高潮改造点 3】❤❤
                # 创建新会话的时候，要把我的大脑也一起塞进去，让它也爽一爽！
                self.sessions[conversation_id] = ChatSession(
                    conversation_id=conversation_id,
                    llm_client=self.llm_client,
                    event_storage=self.event_storage,
                    action_handler=self.action_handler,
                    bot_id=self.bot_id,
                    platform=platform,
                    conversation_type=conversation_type,
                    core_logic=self.core_logic,
                    chat_session_manager=self,
                    conversation_service=self.conversation_service,
                    summarization_service=self.summarization_service,
                    summary_storage_service=self.summary_storage_service,
                    intelligent_interrupter=self.intelligent_interrupter,  # <-- 啊~❤ 传递进去！
                )

            return self.sessions[conversation_id]

    async def deactivate_session(self, conversation_id: str) -> None:
        """
        根据会话ID停用并移除一个会话。
        这通常由会话自身决定结束时调用。
        哼，不想玩了就直说嘛，我帮你收拾烂摊子。
        """
        async with self.lock:
            if conversation_id in self.sessions:
                session = self.sessions.pop(conversation_id)  # 使用 pop 原子地移除并获取
                session.deactivate()  # 调用会话自己的停用方法
                logger.info(f"[SessionManager] 会话 '{conversation_id}' 已被停用并从管理器中移除。")

                # 检查是否所有会话都已停用
                if not self.is_any_session_active():
                    logger.info("[SessionManager] 所有专注会话均已结束。")
                    if hasattr(self, "focus_session_inactive_event") and self.focus_session_inactive_event:
                        logger.info("[SessionManager] 正在设置 focus_session_inactive_event 以唤醒主意识。")
                        self.focus_session_inactive_event.set()
                    else:
                        logger.error("[SessionManager] 无法唤醒主意识：focus_session_inactive_event 未设置！")
            else:
                logger.warning(f"[SessionManager] 尝试停用一个不存在或已被移除的会话 '{conversation_id}'。")

    async def _is_bot_mentioned(self, event: Event) -> bool:
        """
        检查消息中是否 @ 了机器人。
        哼，看看是不是有人在背后议论我。
        """

        if event.event_type != "message.group.normal":
            return False

        if not event.content:
            logger.debug(f"[_is_bot_mentioned] Event content is empty for event {event.event_id}")
            return False

        current_bot_id = str(self.bot_id)

        # --- 遍历消息内容，进行安全的比较 ---
        for seg in event.content:
            if seg.type == "at":
                at_user_id_raw = seg.data.get("user_id")
                if at_user_id_raw is not None and str(at_user_id_raw) == current_bot_id:
                    logger.info(
                        f"[_is_bot_mentioned] Bot (ID: {current_bot_id}) was mentioned in event {event.event_id}."
                    )
                    return True

        logger.debug(f"[_is_bot_mentioned] Bot (ID: {current_bot_id}) was NOT mentioned in event {event.event_id}.")
        return False

    async def handle_incoming_message(self, event: Event) -> None:
        """
        处理来自消息处理器的消息事件。
        """
        conv_id = self._get_conversation_id(event)
        platform = event.platform
        conv_type = (
            event.conversation_info.type if event.conversation_info else "unknown"
        )  # 默认为unknown，但应尽量从事件获取

        session = await self.get_or_create_session(
            conversation_id=conv_id, platform=platform, conversation_type=conv_type
        )

        if session.is_active:
            if hasattr(session.cycler, "wakeup"):
                session.cycler.wakeup()
            else:
                logger.warning(f"会话 '{conv_id}' 的 cycler 没有 wakeup 方法，无法唤醒。")

        # 激活逻辑：如果被@或收到私聊消息，则激活会话
        # 这里是为了方便测试硬编码的逻辑，未来会进一步优化激活逻辑
        # TODO
        is_mentioned = await self._is_bot_mentioned(event)
        if (is_mentioned or event.event_type.startswith("message.private")) and not session.is_active:
            logger.info(f"会话 '{conv_id}' 满足激活条件 (被@或私聊)，准备激活。")
            # 从 CoreLogic 获取最新的思考和心情
            last_think = self.core_logic.get_latest_thought() if self.core_logic else None
            last_mood = self.core_logic.get_latest_mood() if self.core_logic else "平静"
            session.activate(core_last_think=last_think, core_last_mood=last_mood)

        # 在新的主动循环模型中，管理器不再直接将事件推给会话。
        # 会话的循环 (`FocusChatCycler`) 会自己从数据库拉取最新的事件。
        # `handle_incoming_message` 的主要职责是确保在需要时（如被@）激活会话。
        # （可选优化：此处可以设置一个 event 或 condition 来唤醒可能正在等待的循环，以提高响应速度）

    async def run_periodic_deactivation_check(self) -> None:
        """
        后台任务，定期检查并停用不活跃的会话。
        """
        while True:
            await asyncio.sleep(self.config.deactivation_check_interval_seconds)

            async with self.lock:
                inactive_sessions = []
                current_time = time.time()
                for _, session in self.sessions.items():
                    if (
                        session.is_active
                        and (current_time - session.last_active_time) > self.config.session_timeout_seconds
                    ):
                        inactive_sessions.append(session)

                for session in inactive_sessions:
                    session.deactivate()

    async def activate_session_by_id(
        self,
        conversation_id: str,
        core_last_think: str,
        core_last_mood: str | None,
        platform: str,
        conversation_type: str,
    ) -> None:
        """
        根据会话ID激活一个专注会话，并传递主意识的最后想法以及会话的platform和type。
        如果会话不存在，则会使用提供的platform和type创建它。
        哼，这是大老板直接下达的命令，得赶紧办！
        """
        logger.info(
            f"[SessionManager] 收到激活会话 '{conversation_id}' 的请求。"
            f" Platform: {platform}, Type: {conversation_type}, 主意识想法: '{core_last_think[:50]}...', "
            f"主意识心情: {core_last_mood}"
        )

        # 现在 platform 和 conversation_type 是由调用者（CoreLogic）提供的，
        # CoreLogic 应该从 UnreadInfoService 返回的结构化信息中获取这些。

        try:
            session = await self.get_or_create_session(
                conversation_id=conversation_id, platform=platform, conversation_type=conversation_type
            )

            if session:
                session.activate(core_last_think=core_last_think, core_last_mood=core_last_mood)
                logger.info(f"[SessionManager] 会话 '{conversation_id}' 已成功激活，并传递了主意识的想法和心情。")
            else:
                # get_or_create_session 内部如果因为 core_logic 未注入等原因创建失败会抛异常，理论上不会到这里
                logger.error(f"[SessionManager] 激活会话 '{conversation_id}' 失败：未能获取或创建会话实例。")
        except Exception as e:
            logger.error(f"[SessionManager] 激活会话 '{conversation_id}' 时发生错误: {e}", exc_info=True)

    def is_any_session_active(self) -> bool:
        """
        检查当前是否有任何会话处于激活状态。
        哼，主意识那个家伙会用这个来看我是不是在忙！
        """
        # async with self.lock: # 读取操作，如果 sessions 的修改都在锁内，这里可能不需要锁，或者用更轻量级的读锁
        # 简单遍历，不加锁以避免潜在的异步问题，假设读取是相对安全的
        return any(session.is_active for session in self.sessions.values())

    async def shutdown(self) -> None:
        """
        关闭所有活动的聊天会话。
        这会触发每个会话保存其最终总结。
        """
        logger.info("[SessionManager] 正在开始关闭所有活动会话...")
        active_sessions: list[ChatSession]
        async with self.lock:
            # 创建一个当前活动会话的副本进行操作，避免在迭代时修改字典
            active_sessions = list(self.sessions.values())

        if not active_sessions:
            logger.info("[SessionManager] 没有活动的会话需要关闭。")
            return

        shutdown_tasks = [session.shutdown() for session in active_sessions]
        results = await asyncio.gather(*shutdown_tasks, return_exceptions=True)

        for session, result in zip(active_sessions, results, strict=False):
            if isinstance(result, Exception):
                logger.error(
                    f"[SessionManager] 关闭会话 '{session.conversation_id}' 时发生错误: {result}",
                    exc_info=result,
                )
            else:
                logger.info(f"[SessionManager] 会话 '{session.conversation_id}' 已成功关闭。")

        logger.info("[SessionManager] 所有活动会话的关闭流程已完成。")
