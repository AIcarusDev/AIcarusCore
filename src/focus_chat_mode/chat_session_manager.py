# src/focus_chat_mode/chat_session_manager.py
import asyncio
import time
from typing import TYPE_CHECKING, Any, Optional

from src.action.action_handler import ActionHandler
from src.common.custom_logging.logging_config import get_logger
from src.config.aicarus_configs import FocusChatModeSettings
from src.database.models import ConversationDetails
from src.database.services.event_storage_service import EventStorageService
from src.database.services.thought_storage_service import ThoughtStorageService
from src.llmrequest.llm_processor import Client as LLMProcessorClient

from .chat_session import ChatSession
from .deliberation_service import DeliberationService
from .focus_manager import FocusManager

if TYPE_CHECKING:
    from src.common.intelligent_interrupt_system.intelligent_interrupter import (
        IntelligentInterrupter,
    )
    from src.core_logic.consciousness_flow import CoreLogic as CoreLogicFlow
    from src.core_logic.internal_info_builder import InternalInfoBuilder
    from src.database.services.entity_graph_service import EntityGraphService

logger = get_logger(__name__)


class ChatSessionManager:
    """管理所有 ChatSession 实例，处理消息分发和会话生命周期."""

    def __init__(
        self,
        config: FocusChatModeSettings,
        llm_client: LLMProcessorClient,
        deliberation_llm_client: LLMProcessorClient | None,
        event_storage: EventStorageService,
        action_handler: ActionHandler,
        self_bot_ids_map: dict[str, str],
        intelligent_interrupter: "IntelligentInterrupter",
        entity_graph_service: "EntityGraphService",
        thought_storage_service: "ThoughtStorageService",
        internal_info_builder: "InternalInfoBuilder",
        core_logic: Optional["CoreLogicFlow"] = None,
    ) -> None:
        """初始化 ChatSessionManager."""
        self.config = config
        self.llm_client = llm_client
        self.deliberation_llm_client = deliberation_llm_client
        self.event_storage = event_storage
        self.action_handler = action_handler
        self.self_bot_ids_map = self_bot_ids_map
        self.thought_storage_service = thought_storage_service
        self.internal_info_builder = internal_info_builder
        self.intelligent_interrupter = intelligent_interrupter
        self.entity_graph_service = entity_graph_service
        self.core_logic = core_logic
        self.sessions: dict[str, ChatSession] = {}
        self.lock = asyncio.Lock()
        self.platform_view_states: dict[str, dict[str, Any]] = {}
        self.global_command_feedback: str | None = None

        # 初始化新的服务
        self.focus_manager = FocusManager(
            chat_session_manager=self,
            entity_graph_service=self.entity_graph_service,
            trigger_thought_cycle_callback=self.core_logic.trigger_immediate_thought_cycle
            if self.core_logic
            else lambda: None,
        )
        self.deliberation_service = (
            DeliberationService(self.deliberation_llm_client)
            if self.deliberation_llm_client
            else None
        )

        logger.info("ChatSessionManager 初始化完成。")

    def initialize_platform_view_state(self, platform_id: str) -> None:
        """为特定平台初始化或重置视图状态."""
        self.platform_view_states[platform_id] = {"scroll_offset": 0}
        logger.info(f"已为平台 '{platform_id}' 初始化视图状态。")

    def clear_platform_view_state(self, platform_id: str) -> None:
        """当焦点离开平台时，清除其视图状态."""
        if platform_id in self.platform_view_states:
            del self.platform_view_states[platform_id]
            logger.info(f"已清除平台 '{platform_id}' 的视图状态。")

    @property
    def current_focus_path(self) -> dict[str, Any] | None:
        """属性：返回当前的注意力焦点状态字典."""
        return self.focus_manager.current_focus_path

    async def get_or_create_session(self, conversation_entity_uid: str) -> ChatSession | None:
        """根据会话实体的UID获取或创建ChatSession."""
        async with self.lock:
            if conversation_entity_uid in self.sessions:
                return self.sessions[conversation_entity_uid]

            logger.info(f"[SessionManager] 为实体 '{conversation_entity_uid}' 创建新的会话实例。")
            conv_entity_doc = await self.entity_graph_service.get_entity_by_key(
                conversation_entity_uid
            )

            if not conv_entity_doc or not isinstance(conv_entity_doc.details, ConversationDetails):
                logger.error(
                    f"严重错误：找不到ID为'{conversation_entity_uid}'的会话实体或类型不匹配！"
                )
                return None

            # 从实体文档中提取信息来创建 EnrichedConversationInfo (DTO)
            from src.database import EnrichedConversationInfo

            conv_details = conv_entity_doc.details
            bot_id_for_session = self.self_bot_ids_map.get(conv_details.platform)
            if not bot_id_for_session:
                logger.error(
                    f"无法为平台 '{conv_details.platform}' 创建会话，ID地图中找不到对应ID。"
                )
                return None
                
            # ========================= [FIX START] =========================
            # 删除了以下三行错误代码：
            # if "extra" not in conv_details:
            #     conv_details.extra = {}
            # conv_details.extra["membership_status"] = conv_details.membership_status
            # 理由：
            # 1. `conv_details` 是 ConversationDetails 对象，不是字典，`in` 操作会引发 TypeError。
            # 2. dataclass 定义已确保 `extra` 始终存在且为字典。
            # 3. `membership_status` 不是 `ConversationDetails` 的属性，访问它会引发 AttributeError。
            #    该属性应在创建 EnrichedConversationInfo 时从 extra 字典中读取。
            # ========================== [FIX END] ==========================

            conversation_info_obj = EnrichedConversationInfo(
                conversation_id=conv_details.conversation_id,
                platform=conv_details.platform,
                bot_id=bot_id_for_session,
                type=conv_details.type,
                name=conv_details.name,
                parent_id=conv_details.parent_id,
                avatar=conv_details.avatar,
                extra=conv_details.extra,
            )

            if not self.core_logic:
                raise RuntimeError("CoreLogic未注入，ChatSessionManager无法创建会话。")

            # 从会话实体文档中读取上次处理的时间戳，如果没有则使用当前时间
            initial_last_processed_timestamp = (
                getattr(conv_entity_doc, "last_read_timestamp", 0.0) or time.time() * 1000.0
            )

            new_session = ChatSession(
                conversation_info=conversation_info_obj,
                conversation_id=conversation_entity_uid,
                llm_client=self.llm_client,
                event_storage=self.event_storage,
                action_handler=self.action_handler,
                bot_id=bot_id_for_session,
                core_logic=self.core_logic,
                chat_session_manager=self,
                intelligent_interrupter=self.intelligent_interrupter,
                thought_storage_service=self.thought_storage_service,
                internal_info_builder=self.internal_info_builder,
                entity_graph_service=self.entity_graph_service,
                initial_last_processed_timestamp=initial_last_processed_timestamp,
            )
            self.sessions[conversation_entity_uid] = new_session

            return new_session

    async def deactivate_session(
        self, conversation_entity_uid: str, handover_context: dict | None = None
    ) -> None:
        """处理会话停用，触发最终总结并从管理器中移除会话档案."""
        async with self.lock:
            if session := self.sessions.pop(conversation_entity_uid, None):
                logger.info(
                    f"[SessionManager] 会话实体 '{conversation_entity_uid}' 的档案正在被移除。"
                )
                final_timestamp = session.last_processed_timestamp
                await self.entity_graph_service.update_conversation_last_read_timestamp(
                    conversation_entity_uid, final_timestamp
                )
                logger.info(
                    f"[SessionManager] 会话实体 '{conversation_entity_uid}' 的最终总结已处理，"
                    f"档案已移除。"
                )

    def get_last_switch_description(self) -> str:
        """获取上次注意力切换的格式化描述."""
        return self.focus_manager.get_last_switch_description()

    async def handle_consciousness_control(
        self, control_json: dict, current_internal_state: dict
    ) -> dict | None:
        """处理来自LLM决策的意识控制指令的总调度中心.

        此方法现在可以处理“慢思考”指令，并返回一个新的思考状态。
        """
        # 在处理新指令前，获取当前会话并清除旧的反馈
        session = self.core_logic._get_current_session() if self.core_logic else None
        if session:
            session.last_command_feedback = None

        # 清除全局反馈
        self.global_command_feedback = None

        # 验证：检查是否违反了“最多一个指令”的规则
        if len(control_json) > 1:
            error_message = "错误：同时发出了多个意识控制指令，每轮只能执行一个。"
            logger.error(
                f"LLM违反了 'maxProperties: 1' 约束，"
                f"在 'consciousness_control' 中提供了多个指令: {control_json}。将忽略所有指令。"
            )
            if session:
                session.last_command_feedback = error_message
            else:
                self.global_command_feedback = error_message
            return None

        if not (command := next(iter(control_json), None)) or not (
            params := control_json.get(command)
        ):
            logger.warning(f"收到的意识控制指令格式不正确或为空: {control_json}")
            return None

        # 检查指令是否合法
        known_commands = {
            "deep_think",
            "focus",
            "return",
            "back",
            "shift_focus",
            "teleport_focus",
            "jump_to_history",
        }
        if command not in known_commands:
            error_message = f"未知的意识控制指令: '{command}'。"
            logger.error(f"收到未知的意识控制指令: '{command}'，无法处理。")
            if session:
                session.last_command_feedback = error_message
            else:
                self.global_command_feedback = error_message
            return None

        # 如果是“慢思考”指令，则进入深度思考流程
        if command == "deep_think":
            logger.info(f"检测到 [慢思考]，参数: {params}，正在进入深度思考...")
            return await self.deliberation_service.execute(params, current_internal_state, session)

        # 其他所有已知指令都属于注意力转移
        logger.info(f"检测到 [注意力转移]，指令: {command}, 参数: {params}, 正在处理...")
        await self.focus_manager.handle_focus_control(command, params)
        return None

    def shutdown(self) -> None:
        """在应用程序关闭时执行清理操作.

        主要负责清除平台视图状态，以防止跨会话的状态泄漏。
        """
        logger.info("ChatSessionManager 正在关闭，清理平台视图状态...")
        self.platform_view_states.clear()
        logger.info("平台视图状态已清除。")