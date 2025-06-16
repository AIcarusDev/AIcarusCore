import asyncio
import time
from typing import TYPE_CHECKING, Optional

from aicarus_protocols.event import Event

from src.action.action_handler import ActionHandler

# 从项目中导入必要的模块
from src.common.custom_logging.logger_manager import get_logger
from src.config.aicarus_configs import SubConsciousnessSettings
from src.database.services.event_storage_service import EventStorageService
from src.llmrequest.llm_processor import Client as LLMProcessorClient

from .chat_session import ChatSession  # Updated import

if TYPE_CHECKING:
    from src.core_logic.consciousness_flow import CoreLogic as CoreLogicFlow  # 用于类型提示
    from src.core_logic.summarization_service import SummarizationService  # 用于类型提示

logger = get_logger(__name__)


class ChatSessionManager:  # Renamed class
    """
    管理所有 ChatSession 实例，处理消息分发和会话生命周期。
    """

    def __init__(
        self,
        config: SubConsciousnessSettings,
        llm_client: LLMProcessorClient,
        event_storage: EventStorageService,
        action_handler: ActionHandler,
        bot_id: str,
        # 新增依赖，core_logic 稍后通过 setter 注入
        summarization_service: "SummarizationService",
        core_logic: Optional["CoreLogicFlow"] = None,
    ) -> None:
        self.config = config
        self.llm_client = llm_client
        self.event_storage = event_storage
        self.action_handler = action_handler
        self.bot_id = bot_id
        self.logger = logger

        self.summarization_service = summarization_service  # 新增
        self.core_logic = core_logic  # 新增，可能为 None

        self.sessions: dict[str, ChatSession] = {}
        self.lock = asyncio.Lock()
        self._core_logic_activity_event: Optional[asyncio.Event] = None  # 用于唤醒主循环

        self.logger.info("ChatSessionManager 初始化完成。")

    def set_core_logic(self, core_logic_instance: "CoreLogicFlow") -> None:
        """延迟注入 CoreLogic 实例，解决循环依赖。"""
        self.core_logic = core_logic_instance
        self.logger.info("CoreLogic 实例已成功注入到 ChatSessionManager。")

    def set_core_logic_activity_event(self, event: asyncio.Event) -> None:
        """从 CoreLogic 接收用于控制其主循环的事件。"""
        self._core_logic_activity_event = event
        self.logger.info("已从 CoreLogic 接收到活动事件。")

    def _get_conversation_id(self, event: Event) -> str:
        # 从 Event 中提取唯一的会话ID (例如 group_id 或 user_id)
        # 此处需要根据 aicarus_protocols 的具体定义来实现
        info = event.conversation_info
        return info.conversation_id if info else "default_conv"

    async def get_or_create_session(
        self, conversation_id: str, platform: str | None = None, conversation_type: str | None = None
    ) -> ChatSession:  # Updated return type hint
        async with self.lock:
            if conversation_id not in self.sessions:
                logger.info(f"[SessionManager] 为 '{conversation_id}' 创建新的会话实例。")

                # 对于新会话，platform 和 conversation_type 应该是必需的
                # 调用者 (handle_incoming_message) 应该从事件中提取并提供这些信息
                if not platform or not conversation_type:
                    logger.error(
                        f"[SessionManager] 创建新会话 '{conversation_id}' 时缺少 platform 或 conversation_type。"
                    )
                    # 可以选择抛出错误，或者使用默认值（但不推荐）
                    # raise ValueError(f"Platform and conversation_type are required to create a new session for {conversation_id}")
                    # 使用默认值作为后备，但理想情况下不应发生
                    platform = platform or "unknown_platform"
                    conversation_type = conversation_type or "unknown_type"

                if not self.core_logic:
                    logger.error(
                        f"[SessionManager] CoreLogic尚未注入到ChatSessionManager，无法创建ChatSession '{conversation_id}'。"
                    )
                    raise RuntimeError("CoreLogic未注入，ChatSessionManager无法创建会话。")
                if not self.summarization_service:  # summarization_service 应该在 __init__ 时就提供
                    logger.error(
                        f"[SessionManager] SummarizationService未初始化，无法创建ChatSession '{conversation_id}'。"
                    )
                    raise RuntimeError("SummarizationService未初始化，ChatSessionManager无法创建会话。")

                self.sessions[conversation_id] = ChatSession(
                    conversation_id=conversation_id,
                    llm_client=self.llm_client,
                    event_storage=self.event_storage,
                    action_handler=self.action_handler,
                    bot_id=self.bot_id,
                    platform=platform,
                    conversation_type=conversation_type,
                    core_logic=self.core_logic,  # 注入 CoreLogic
                    chat_session_manager=self,  # 注入自身
                    summarization_service=self.summarization_service,  # 注入 SummarizationService
                )

            # # 确保现有会话实例也有 platform 和 conversation_type (如果之前没有)
            # # 这部分逻辑可能不需要，因为这些属性应该在创建时就设置好
            # elif not hasattr(self.sessions[conversation_id], 'platform') or \
            #      not hasattr(self.sessions[conversation_id], 'conversation_type'):
            #     if platform and conversation_type:
            #         self.sessions[conversation_id].platform = platform
            #         self.sessions[conversation_id].conversation_type = conversation_type
            #         # self.active_session_context[conversation_id] = {"platform": platform, "type": conversation_type}
            #     # else:
            #         # logger.warning(f"[SessionManager] 尝试更新现有会话 '{conversation_id}' 的上下文，但未提供 platform/type。")

            return self.sessions[conversation_id]

    async def deactivate_session(self, conversation_id: str) -> None:
        """
        根据会话ID停用并移除一个会话。
        这通常由会话自身决定结束时调用。
        哼，不想玩了就直说嘛，我帮你收拾烂摊子。
        """
        async with self.lock:
            if conversation_id in self.sessions:
                session = self.sessions.pop(conversation_id)
                session.deactivate()
                self.logger.info(f"[SessionManager] 会话 '{conversation_id}' 已被停用并从管理器中移除。")

                # 检查是否所有会话都已结束
                if not self.is_any_session_active():
                    if self._core_logic_activity_event:
                        self.logger.info("所有专注会话已结束，正在设置事件以唤醒主意识循环。")
                        self._core_logic_activity_event.set()
                    else:
                        self.logger.warning("所有专注会话已结束，但未设置核心逻辑活动事件，无法唤醒主意识。")
            else:
                self.logger.warning(f"[SessionManager] 尝试停用一个不存在或已被移除的会话 '{conversation_id}'。")

    def _is_bot_mentioned(self, event: Event) -> bool:
        # 检查消息中是否 @ 了机器人
        if event.event_type != "message.group.normal":
            return False

        if not event.content:  # 检查 content 是否为 None 或空列表
            self.logger.debug(f"[_is_bot_mentioned] Event content is empty for event {event.event_id}")
            return False

        for seg in event.content:  # 应该直接访问 event.content
            if seg.type == "at":
                at_user_id = seg.data.get("user_id")  # 应该用 'user_id' 而不是 'qq'
                self.logger.debug(
                    f"[_is_bot_mentioned] Found @ segment. at_user_id: {at_user_id} (type: {type(at_user_id)}), self.bot_id: {self.bot_id} (type: {type(self.bot_id)})"
                )
                # 确保比较时双方都是字符串
                if at_user_id is not None and str(at_user_id) == str(self.bot_id):
                    self.logger.info(
                        f"[_is_bot_mentioned] Bot (ID: {self.bot_id}) was mentioned in event {event.event_id}."
                    )
                    return True

        self.logger.debug(f"[_is_bot_mentioned] Bot (ID: {self.bot_id}) was NOT mentioned in event {event.event_id}.")
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

        # 激活逻辑：如果被@，则激活会话
        if self._is_bot_mentioned(event) and not session.is_active:
            # 从 CoreLogic 获取最新的思考
            last_think = self.core_logic.get_latest_thought() if self.core_logic else None
            session.activate(core_last_think=last_think)

        # 将事件交给会话处理 (会话内部会判断 is_active)
        await session.process_event(event)

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
        self, conversation_id: str, core_last_think: str, platform: str, conversation_type: str
    ) -> None:
        """
        根据会话ID激活一个专注会话，并传递主意识的最后想法以及会话的platform和type。
        如果会话不存在，则会使用提供的platform和type创建它。
        哼，这是大老板直接下达的命令，得赶紧办！
        """
        self.logger.info(
            f"[SessionManager] 收到激活会话 '{conversation_id}' 的请求。"
            f" Platform: {platform}, Type: {conversation_type}, 主意识想法: '{core_last_think[:50]}...'"
        )

        # 现在 platform 和 conversation_type 是由调用者（CoreLogic）提供的，
        # CoreLogic 应该从 UnreadInfoService 返回的结构化信息中获取这些。

        try:
            session = await self.get_or_create_session(
                conversation_id=conversation_id, platform=platform, conversation_type=conversation_type
            )

            if session:
                session.activate(core_last_think=core_last_think)  # 调用 ChatSession 的 activate 方法并传递想法
                self.logger.info(f"[SessionManager] 会话 '{conversation_id}' 已成功激活，并传递了主意识的想法。")
            else:
                # get_or_create_session 内部如果因为 core_logic 未注入等原因创建失败会抛异常，理论上不会到这里
                self.logger.error(f"[SessionManager] 激活会话 '{conversation_id}' 失败：未能获取或创建会话实例。")
        except Exception as e:
            self.logger.error(f"[SessionManager] 激活会话 '{conversation_id}' 时发生错误: {e}", exc_info=True)

    def is_any_session_active(self) -> bool:
        """
        检查当前是否有任何会话处于激活状态。
        哼，主意识那个家伙会用这个来看我是不是在忙！
        """
        # async with self.lock: # 读取操作，如果 sessions 的修改都在锁内，这里可能不需要锁，或者用更轻量级的读锁
        # 简单遍历，不加锁以避免潜在的异步问题，假设读取是相对安全的
        return any(session.is_active for session in self.sessions.values())
