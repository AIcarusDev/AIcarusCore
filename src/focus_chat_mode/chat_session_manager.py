# src/focus_chat_mode/chat_session_manager.py
# 聊天会话管理器模块，用于管理聊天会话的生命周期和相关操作。
import asyncio
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
    from src.core_logic.internal_info_builder import InternalInfoBuilder

logger = get_logger(__name__)


class ChatSessionManager:
    """管理所有 ChatSession 实例，处理消息分发和会话生命周期.

    这个类负责创建、获取和管理聊天会话的生命周期，包括消息的分发和处理。

    Attributes:
        config (FocusChatModeSettings): 专注聊天模式的配置设置。
        llm_client (LLMProcessorClient): LLM 处理器客户端，用于与 LLM 交互。
        event_storage (EventStorageService): 事件存储服务，用于存储和检索事件。
        action_handler (ActionHandler): 动作处理器，用于处理会话中的动作。
        bot_id (str): 祂的唯一标识符，用于识别和处理消息。
        conversation_service (ConversationStorageService): 会话存储服务，用于管理会话数据.
        summarization_service (SummarizationService): 摘要服务，用于生成会话摘要.
        summary_storage_service (SummaryStorageService): 摘要存储服务，用于存储和检索摘要数据.
        intelligent_interrupter (IntelligentInterrupter): 智能中断系统，用于处理
            会话中的智能中断逻辑.
        thought_storage_service (ThoughtStorageService): 思考存储服务，用于存储和检索思考数据.
        core_logic (Optional[CoreLogicFlow]): 核心逻辑流实例，用于处理会话的核心逻辑和决策.
        sessions (dict[str, ChatSession]): 存储所有活动聊天会话的字典，键为会话ID，
            值为 ChatSession 实例.
        lock (asyncio.Lock): 异步锁，用于确保对会话字典的线程安全访问.
        focus_session_inactive_event (Optional[asyncio.Event]): 用于唤醒主意识的
            事件对象，当所有专注会话结束时触发.
    """

    def __init__(
        self,
        config: FocusChatModeSettings,
        llm_client: LLMProcessorClient,
        event_storage: EventStorageService,
        action_handler: ActionHandler,
        self_bot_ids_map: dict[str, str],
        conversation_service: ConversationStorageService,
        summarization_service: "SummarizationService",
        summary_storage_service: "SummaryStorageService",
        intelligent_interrupter: "IntelligentInterrupter",
        thought_storage_service: "ThoughtStorageService",
        internal_info_builder: "InternalInfoBuilder",
        core_logic: Optional["CoreLogicFlow"] = None,
    ) -> None:
        self.config = config
        self.llm_client = llm_client
        self.event_storage = event_storage
        self.action_handler = action_handler
        self.self_bot_ids_map = self_bot_ids_map

        self.conversation_service = conversation_service
        self.summarization_service = summarization_service
        self.summary_storage_service = summary_storage_service
        self.thought_storage_service = thought_storage_service
        self.internal_info_builder = internal_info_builder

        self.intelligent_interrupter = intelligent_interrupter

        self.core_logic = core_logic

        self.sessions: dict[str, ChatSession] = {}
        self.lock = asyncio.Lock()
        self.current_focus_path: str | None = None

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
                "CoreLogic 实例中没有找到 focus_session_inactive_event！"
                "这会导致主意识无法被正确唤醒！"
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
        """获取或创建一个聊天会话实例.

        如果会话已存在，则返回现有实例；如果不存在，则创建一个新的会话实例。

        Args:
            conversation_id (str): 会话的唯一标识符。
            platform (str | None): 消息来源的平台（如 QQ、Telegram 等）。
            conversation_type (str | None): 会话类型（如 group、private 等）。
        Returns:
            ChatSession: 对应的聊天会话实例。
        """
        async with self.lock:
            if conversation_id not in self.sessions:
                logger.info(f"[SessionManager] 为 '{conversation_id}' 创建新的会话实例。")

                if not platform or not conversation_type:
                    raise ValueError(
                        f"Platform 和 conversation_type 是创建新会话 '{conversation_id}' 的必需品！"
                    )

                if not self.core_logic:
                    raise RuntimeError("CoreLogic未注入，ChatSessionManager无法创建会话。")
                # 确保在创建会话时使用正确的自身ID
                bot_id_for_session = self.self_bot_ids_map.get(platform)
                if not bot_id_for_session:
                    # 如果因为某种原因找不到（比如安检失败），这是一个严重问题
                    raise RuntimeError(
                        f"无法为平台 '{platform}' 创建会话，"
                        "因为在 ChatSessionManager 的 ID 地图中找不到祂对应的ID。"
                    )

                self.sessions[conversation_id] = ChatSession(
                    conversation_id=conversation_id,
                    llm_client=self.llm_client,
                    event_storage=self.event_storage,
                    action_handler=self.action_handler,
                    bot_id=bot_id_for_session,
                    platform=platform,
                    conversation_type=conversation_type,
                    core_logic=self.core_logic,
                    chat_session_manager=self,
                    conversation_service=self.conversation_service,
                    summarization_service=self.summarization_service,
                    summary_storage_service=self.summary_storage_service,
                    intelligent_interrupter=self.intelligent_interrupter,
                    thought_storage_service=self.thought_storage_service,
                    internal_info_builder=self.internal_info_builder,
                )

            return self.sessions[conversation_id]

    async def deactivate_session(
        self, conversation_id: str, handover_context: dict | None = None
    ) -> None:
        """处理会话停用。现在它负责触发最终总结并从管理器中移除会话档案."""
        async with self.lock:
            session = self.sessions.pop(conversation_id, None)
            if session:
                logger.info(f"[SessionManager] 会话 '{conversation_id}' 的档案正在被移除。")

                # 停止会话的中断检查器
                await session.stop_interrupt_checker()

                # 调用其内部的总结管理器来执行最终总结
                shift_motivation = handover_context.get("motivation") if handover_context else None
                target_id = handover_context.get("target_id") if handover_context else None
                await session.summarization_manager.create_and_save_final_summary(
                    shift_motivation=shift_motivation, target_conversation_id=target_id
                )
                logger.info(
                    f"[SessionManager] 会话 '{conversation_id}' 的最终总结已处理，档案已移除。"
                )

    async def shutdown(self) -> None:
        """关闭所有活动的聊天会话."""
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

    async def handle_consciousness_control(self, control_json: dict) -> None:
        """处理来自LLM决策的意识控制指令.

        这是焦点管理的核心，负责根据指令更新 self.current_focus_path 并触发主循环.
        """
        if not control_json or not isinstance(control_json, dict):
            return

        logger.info(f"焦点管理器收到意识控制指令: {control_json}")

        # 标志位，表示焦点是否发生了切换
        focus_switched = False

        # 获取指令和动机
        command, params = next(iter(control_json.items()))
        motivation = params.get("motivation", "没有明确动机")

        handover_result = params.pop("_handover_action_result", None)
        if handover_result:
            logger.info(f"在注意力控制指令中发现了交接的动作结果: {handover_result['action_name']}")

        if command == "focus":
            target_id = params.get("platform_id") or params.get("conversation_id")
            if not target_id:
                logger.error("'focus' 指令缺少 'platform_id' 或 'conversation_id'。")
                return

            platform_to_check = target_id if not self.current_focus_path else self.current_focus_path.split('.')[0]

            # 在尝试 focus 到一个平台或会话前，必须检查该平台的身份是否已确认
            if platform_to_check not in self.self_bot_ids_map:
                logger.warning(
                    f"AI 尝试 [focus] 到平台 '{platform_to_check}' 或其下的会话 '{target_id}'，"
                    f"但该平台的安检尚未完全完成（ID未登记）。本次 focus 动作被拒绝。"
                )
                # 动作失败，直接返回，不改变焦点，也不触发思考。
                # 主循环会按正常间隔继续下一轮思考。
                return

            # 构建新的焦点路径
            new_focus_path = ""
            if not self.current_focus_path:  # 从顶层进入中层
                new_focus_path = target_id
            else:  # 从中层进入底层
                new_focus_path = f"{self.current_focus_path}.{target_id}"

            # 如果是进入底层会话，需要预先创建会话档案
            if len(new_focus_path.split(".")) >= 2:
                conv_doc = await self.conversation_service.get_conversation_document_by_id(
                    target_id
                )
                if not conv_doc:
                    logger.error(f"无法 'focus'，数据库中找不到会话 '{target_id}'。")
                    return
                await self.get_or_create_session(
                    conversation_id=target_id,
                    platform=conv_doc.get("platform"),
                    conversation_type=conv_doc.get("type"),
                )

            self.current_focus_path = new_focus_path
            focus_switched = True
            logger.info(f"AI 决定 [focus] 到: '{self.current_focus_path}' (动机: {motivation})")

        elif command == "return":
            if not self.current_focus_path:
                logger.warning("在顶层Core-Level尝试执行 'return'，这是一个无效操作，已忽略。")
                return

            path_parts = self.current_focus_path.split(".")
            if len(path_parts) > 1:  # 从底层返回中层
                # 在返回前，需要处理刚刚离开的会话的最终总结
                leaving_conv_id = path_parts[-1]
                await self.deactivate_session(leaving_conv_id, {"motivation": motivation})
                self.current_focus_path = ".".join(path_parts[:-1])
            else:  # 从中层返回顶层
                self.current_focus_path = None
            # 触发主循环
            focus_switched = True
            logger.info(
                f"AI 决定 [return] 到: '{self.current_focus_path or 'Core'}' (动机: {motivation})"
            )

        elif command == "shift":
            target_conv_id = params.get("conversation_id")
            if (
                not target_conv_id
                or not self.current_focus_path
                or "." not in self.current_focus_path
            ):
                logger.error("'shift' 指令无效：缺少目标ID或当前不在底层会话中。")
                return

            leaving_conv_id = self.current_focus_path.split(".")[-1]
            platform_path = ".".join(self.current_focus_path.split(".")[:-1])

            # 先处理离开的会话
            await self.deactivate_session(
                leaving_conv_id, {"motivation": motivation, "target_id": target_conv_id}
            )

            # 再处理进入新的会话
            conv_doc = await self.conversation_service.get_conversation_document_by_id(
                target_conv_id
            )
            if not conv_doc:
                logger.error(f"无法 'shift'，数据库中找不到目标会话 '{target_conv_id}'。")
                self.current_focus_path = platform_path  # 退回到平台层
            else:
                await self.get_or_create_session(
                    conversation_id=target_conv_id,
                    platform=conv_doc.get("platform"),
                    conversation_type=conv_doc.get("type"),
                )
                self.current_focus_path = f"{platform_path}.{target_conv_id}"

            focus_switched = True
            logger.info(f"AI 决定 [shift] 到: '{self.current_focus_path}' (动机: {motivation})")

        # 如果发生了任何焦点切换，就唤醒主循环并设置上下文切换标志
        if focus_switched and self.core_logic and hasattr(self.core_logic, "prompt_builder"):
            self.core_logic.prompt_builder.is_context_switch_flag = True
            self.core_logic.trigger_immediate_thought_cycle()
