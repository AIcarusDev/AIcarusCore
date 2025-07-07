# src/focus_chat_mode/chat_session_manager.py
# 聊天会话管理器模块，用于管理聊天会话的生命周期和相关操作。
import asyncio
import time
from typing import TYPE_CHECKING, Optional

from aicarus_protocols import Event
from src.action.action_handler import ActionHandler
from src.common.custom_logging.logging_config import get_logger
from src.config.aicarus_configs import FocusChatModeSettings
from src.database import ConversationStorageService
from src.database.services.event_storage_service import EventStorageService
from src.database.services.summary_storage_service import SummaryStorageService
from src.database.services.thought_storage_service import ThoughtStorageService
from src.llmrequest.llm_processor import Client as LLMProcessorClient

from .chat_session import ChatSession

if TYPE_CHECKING:
    # 引入智能中断系统模块，用于类型提示。
    from src.common.intelligent_interrupt_system.intelligent_interrupter import (
        IntelligentInterrupter,
    )
    from src.common.summarization_observation.summarization_service import SummarizationService
    from src.core_logic.consciousness_flow import CoreLogic as CoreLogicFlow

logger = get_logger(__name__)


class ChatSessionManager:
    """管理所有 ChatSession 实例，处理消息分发和会话生命周期。
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
        intelligent_interrupter: "IntelligentInterrupter",
        thought_storage_service: "ThoughtStorageService",  # 哼，新来的！
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
        self.thought_storage_service = thought_storage_service  # 哼，新来的！

        self.intelligent_interrupter = intelligent_interrupter

        self.core_logic = core_logic

        self.sessions: dict[str, ChatSession] = {}
        self.lock = asyncio.Lock()

        logger.info("ChatSessionManager 初始化完成，并已注入智能打断系统。")

    def set_core_logic(self, core_logic_instance: "CoreLogicFlow") -> None:
        """延迟注入 CoreLogic 实例，解决循环依赖."""
        self.core_logic = core_logic_instance
        logger.info("CoreLogic 实例已成功注入到 ChatSessionManager。")

        # 哼，顺便把那个唤醒主意识的事件也拿过来
        if hasattr(core_logic_instance, "focus_session_inactive_event"):
            self.focus_session_inactive_event = core_logic_instance.focus_session_inactive_event
            logger.info("已从 CoreLogic 获取 focus_session_inactive_event。")
        else:
            logger.error(
                "CoreLogic 实例中没有找到 focus_session_inactive_event！这会导致主意识无法被正确唤醒！"
            )
            self.focus_session_inactive_event = None

    def _get_conversation_id(self, event: Event) -> str:
        # 从 Event 中提取唯一的会话ID (例如 group_id 或 user_id)
        # 此处需要根据 aicarus_protocols 的具体定义来实现
        info = event.conversation_info
        return info.conversation_id if info else "default_conv"

    async def get_or_create_session(
        self,
        conversation_id: str,
        platform: str | None = None,
        conversation_type: str | None = None,
    ) -> ChatSession:
        async with self.lock:
            if conversation_id not in self.sessions:
                logger.info(f"[SessionManager] 为 '{conversation_id}' 创建新的会话实例。")

                if not platform or not conversation_type:
                    raise ValueError(
                        f"Platform 和 conversation_type 是创建新会话 '{conversation_id}' 的必需品！"
                    )

                if not self.core_logic:
                    raise RuntimeError("CoreLogic未注入，ChatSessionManager无法创建会话。")

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
                    intelligent_interrupter=self.intelligent_interrupter,
                    thought_storage_service=self.thought_storage_service,  # 哼，新来的！
                )

            return self.sessions[conversation_id]

    async def deactivate_session(self, conversation_id: str) -> None:
        """根据会话ID停用并移除一个会话。
        这通常由会话自身决定结束时调用。
        哼，不想玩了就直说嘛，我帮你收拾烂摊子。
        """
        async with self.lock:
            if conversation_id in self.sessions:
                session = self.sessions.pop(conversation_id, None)  # 用pop，更安全
                if session:
                    # 不再由这里调用 shutdown，而是由 session 内部的 deactivate 触发
                    session.deactivate()
                    logger.info(
                        f"[SessionManager] 会话 '{conversation_id}' 已被停用并从管理器中移除。"
                    )

                # 检查是否所有会话都已停用
                if not self.is_any_session_active():
                    logger.info("[SessionManager] 所有专注会话均已结束。")
                    if (
                        hasattr(self, "focus_session_inactive_event")
                        and self.focus_session_inactive_event
                    ):
                        logger.info(
                            "[SessionManager] 正在设置 focus_session_inactive_event 以唤醒主意识。"
                        )
                        self.focus_session_inactive_event.set()
                    else:
                        logger.error(
                            "[SessionManager] 无法唤醒主意识：focus_session_inactive_event 未设置！"
                        )
            else:
                logger.warning(
                    f"[SessionManager] 尝试停用一个不存在或已被移除的会话 '{conversation_id}'。"
                )

    async def _is_bot_mentioned(self, event: Event, session: "ChatSession") -> bool:
        """检查消息中是否 @ 了机器人。
        哼，看看是不是有人在背后议论我。
        现在我学会照镜子了！
        """
        if not event.event_type.startswith("message.") or not (
            event.conversation_info and event.conversation_info.type == "group"
        ):
            return False

        if not event.content:
            return False

        # --- 照镜子！从 session 里获取我现在的样子！
        bot_profile = await session.get_bot_profile()
        # 如果镜子是碎的（没获取到），就用身份证上的老号码保底
        current_bot_id = str(bot_profile.get("user_id", self.bot_id))

        # --- 遍历消息内容，进行安全的比较 ---
        for seg in event.content:
            if seg.type == "at":
                at_user_id_raw = seg.data.get("user_id")
                if at_user_id_raw is not None and str(at_user_id_raw) == current_bot_id:
                    logger.debug(f"检测到机器人被@，动态ID: {current_bot_id}")  # 加个日志看看
                    return True
        return False

    async def handle_incoming_message(self, event: Event) -> None:
        """处理来自消息处理器的消息事件."""
        conv_id = self._get_conversation_id(event)
        # --- ❤❤❤ 咸猪手修正点！❤❤❤ ---
        # 我不再去乱摸 event.platform 了，而是用更优雅的 event.get_platform()！
        platform = event.get_platform()
        if not platform:
            logger.error(f"无法处理进入专注模式的事件 {event.event_id}，因为它没有可解析的平台ID。")
            return
        conv_type = (
            event.conversation_info.type if event.conversation_info else "unknown"
        )  # 默认为unknown，但应尽量从事件获取

        session = await self.get_or_create_session(
            conversation_id=conv_id, platform=platform, conversation_type=conv_type
        )

        if session.is_active and hasattr(session.cycler, "wakeup"):
            session.cycler.wakeup()
        # TODO:
        # 激活逻辑：如果被@或收到私聊消息，则激活会话
        # 这里是为了方便测试硬编码的逻辑，未来会进一步优化激活逻辑
        if self.is_any_session_active():
            if session.is_active:
                # 如果消息是给当前激活的会话的，就唤醒它去处理新消息
                session.cycler.wakeup()
            else:
                # 如果消息不是给当前激活会话的，就当没看见，不打扰
                logger.debug(f"已有其他会话激活中，忽略对非激活会话 '{conv_id}' 的消息。")
            return

        # // 只有在没有任何会话激活时，才检查@
        is_mentioned = await self._is_bot_mentioned(event, session)
        if is_mentioned:
            logger.info(f"会话 '{conv_id}' 因被@而满足激活条件，准备激活。")
            # // 看，这里只传递动机，别的什么都不管！
            session.activate(core_motivation="被一股神秘的力量吸引了，想看看是谁在叫我。")

        # 在新的主动循环模型中，管理器不再直接将事件推给会话。
        # 会话的循环 (`FocusChatCycler`) 会自己从数据库拉取最新的事件。
        # `handle_incoming_message` 的主要职责是确保在需要时（如被@）激活会话。
        # （可选优化：此处可以设置一个 event 或 condition 来唤醒可能正在等待的循环，以提高响应速度）

    async def run_periodic_deactivation_check(self) -> None:
        """后台任务，定期检查并停用不活跃的会话."""
        while True:
            await asyncio.sleep(self.config.deactivation_check_interval_seconds)

            async with self.lock:
                inactive_session_ids = []
                current_time = time.time()
                for conv_id, session in self.sessions.items():
                    if (
                        session.is_active
                        and (current_time - session.last_active_time)
                        > self.config.session_timeout_seconds
                    ):
                        inactive_session_ids.append(conv_id)
            for conv_id in inactive_session_ids:
                logger.info(f"会话 '{conv_id}' 因超时不活跃，将被系统停用。")
                await self.deactivate_session(conv_id)

    async def activate_session_by_id(
        self,
        conversation_id: str,
        core_motivation: str,  # // 看！现在只需要动机了！
        platform: str,
        conversation_type: str,
    ) -> None:
        """根据会话ID激活一个专注会话。
        哼，这是大老板直接下达的命令，得赶紧办！
        """
        logger.info(
            f"[SessionManager] 收到激活会话 '{conversation_id}' 的请求。"
            f" Platform: {platform}, Type: {conversation_type}, 激活动机: '{core_motivation}'"
        )
        try:
            # // 如果已经有专注会话了，就直接拒绝新的激活指令
            if self.is_any_session_active():
                active_session_id = next(
                    (sid for sid, s in self.sessions.items() if s.is_active), "未知"
                )
                logger.warning(
                    f"拒绝激活 '{conversation_id}'，因为会话 '{active_session_id}' 已处于专注模式。"
                )
                return

            session = await self.get_or_create_session(
                conversation_id=conversation_id,
                platform=platform,
                conversation_type=conversation_type,
            )
            if session:
                # // 只传递动机
                session.activate(core_motivation=core_motivation)
                logger.info(f"[SessionManager] 会话 '{conversation_id}' 已成功激活。")
        except Exception as e:
            logger.error(
                f"[SessionManager] 激活会话 '{conversation_id}' 时发生错误: {e}", exc_info=True
            )

    def is_any_session_active(self) -> bool:
        """检查当前是否有任何会话处于激活状态."""
        # 必须在锁内操作，或者复制一份再操作，避免遍历时字典被修改
        # 这里我们直接复制一份，开销很小但绝对安全
        sessions_copy = self.sessions.copy()
        return any(session.is_active for session in sessions_copy.values())

    async def shutdown(self) -> None:
        """关闭所有活动的聊天会话。
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
