# src/focus_chat_mode/chat_session_manager.py (本体论重构适配版)
import asyncio
import time
from collections import deque
from typing import TYPE_CHECKING, Any, Optional

from src.action.action_handler import ActionHandler
from src.common.custom_logging.logging_config import get_logger
from src.config.aicarus_configs import FocusChatModeSettings
from src.database.models import ConversationDetails
from src.database.services.event_storage_service import EventStorageService
from src.database.services.summary_storage_service import SummaryStorageService
from src.database.services.thought_storage_service import ThoughtStorageService
from src.llmrequest.llm_processor import Client as LLMProcessorClient

from .chat_session import ChatSession

if TYPE_CHECKING:
    from src.common.intelligent_interrupt_system.intelligent_interrupter import (
        IntelligentInterrupter,
    )
    from src.common.summarization_observation.summarization_service import SummarizationService
    from src.core_logic.consciousness_flow import CoreLogic as CoreLogicFlow
    from src.core_logic.internal_info_builder import InternalInfoBuilder

    # (+) 导入新的服务
    from src.database.services.entity_graph_service import EntityGraphService

logger = get_logger(__name__)


class ChatSessionManager:
    """管理所有 ChatSession 实例，处理消息分发和会话生命周期."""

    def __init__(
        self,
        config: FocusChatModeSettings,
        llm_client: LLMProcessorClient,
        event_storage: EventStorageService,
        action_handler: ActionHandler,
        self_bot_ids_map: dict[str, str],
        summarization_service: "SummarizationService",
        summary_storage_service: "SummaryStorageService",
        intelligent_interrupter: "IntelligentInterrupter",
        entity_graph_service: "EntityGraphService",
        thought_storage_service: "ThoughtStorageService",
        internal_info_builder: "InternalInfoBuilder",
        core_logic: Optional["CoreLogicFlow"] = None,
    ) -> None:
        """初始化 ChatSessionManager."""
        self.config = config
        self.llm_client = llm_client
        self.event_storage = event_storage
        self.action_handler = action_handler
        self.self_bot_ids_map = self_bot_ids_map
        self.summarization_service = summarization_service
        self.summary_storage_service = summary_storage_service
        self.thought_storage_service = thought_storage_service
        self.internal_info_builder = internal_info_builder
        self.intelligent_interrupter = intelligent_interrupter
        # (--) self.conversation_service = conversation_service
        self.entity_graph_service = entity_graph_service  # <--- (±) 存储新神的服务实例

        self.core_logic = core_logic
        self.sessions: dict[str, ChatSession] = {}
        self.lock = asyncio.Lock()
        self.focus_history = deque([{"target_path": "core", "motivation": "初始化"}], maxlen=10)
        self._last_switch_description: str = "你刚刚从发呆的状态中回过神来"
        self._command_handlers = {
            "push_focus": self._handle_push_focus,
            "pop_focus": self._handle_pop_focus,
            "back": self._handle_back,
            "swap_focus": self._handle_swap_focus,
            "teleport_focus": self._handle_teleport_focus,
            "jump_to_history": self._handle_jump_to_history,
        }

        logger.info("ChatSessionManager 初始化完成。")

    @property
    def current_focus_path(self) -> dict[str, Any] | None:
        """属性：返回当前焦点路径（堆栈顶部）."""
        return self.focus_history[-1] if self.focus_history else None

    async def get_or_create_session(self, conversation_id: str) -> ChatSession | None:
        """[核心改造] 获取或创建一个聊天会话实例.

        现在它通过 EntityGraphService 获取会话实体来创建会话。
        """
        async with self.lock:
            if conversation_id in self.sessions:
                return self.sessions[conversation_id]

            logger.info(f"[SessionManager] 为实体 '{conversation_id}' 创建新的会话实例。")

            # 从 EntityGraphService 获取会话实体
            # 注意：这里的 conversation_id 实际上是 entity_uid
            conv_entity_doc = await self.entity_graph_service.get_entity_by_key(conversation_id)

            if not conv_entity_doc or not isinstance(conv_entity_doc.details, ConversationDetails):
                logger.error(f"严重错误：找不到ID为'{conversation_id}'的会话实体或类型不匹配！")
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
                conv_entity_doc.get("last_read_timestamp")
                or time.time() * 1000.0
            )

            self.sessions[conversation_id] = ChatSession(
                conversation_info=conversation_info_obj,
                conversation_id=conversation_id,  # 这里的 conversation_id 仍然是 entity_uid
                llm_client=self.llm_client,
                event_storage=self.event_storage,
                action_handler=self.action_handler,
                bot_id=bot_id_for_session,
                core_logic=self.core_logic,
                chat_session_manager=self,
                summarization_service=self.summarization_service,
                summary_storage_service=self.summary_storage_service,
                intelligent_interrupter=self.intelligent_interrupter,
                thought_storage_service=self.thought_storage_service,
                internal_info_builder=self.internal_info_builder,
                entity_graph_service=self.entity_graph_service,
                initial_last_processed_timestamp=initial_last_processed_timestamp,
            )
            return self.sessions[conversation_id]

    async def deactivate_session(
        self, conversation_id: str, handover_context: dict | None = None
    ) -> None:
        """处理会话停用，触发最终总结并从管理器中移除会话档案."""
        async with self.lock:
            if session := self.sessions.pop(conversation_id, None):
                logger.info(f"[SessionManager] 会话实体 '{conversation_id}' 的档案正在被移除。")

                # 将会话的最终处理时间戳持久化到数据库
                final_timestamp = session.last_processed_timestamp
                await self.entity_graph_service.update_conversation_last_read_timestamp(
                    conversation_id, final_timestamp
    )

                context = handover_context or {}
                await session.summarization_manager.create_and_save_final_summary(
                    shift_motivation=context.get("motivation"),
                    target_conversation_id=context.get("target_id"),
                )
                logger.info(
                    f"[SessionManager] 会话实体 '{conversation_id}' 的最终总结已处理，档案已移除。"
                )

    async def _get_focus_description(self, focus_path_or_entry: str | dict | None) -> str:
        """根据 focus_path 或历史条目 生成一个详细的、人类可读的位置描述."""
        focus_path: str | None = None
        if isinstance(focus_path_or_entry, dict):
            focus_path = focus_path_or_entry.get("target_path")
        elif isinstance(focus_path_or_entry, str):
            focus_path = focus_path_or_entry

        if not focus_path or focus_path == "core":
            return "正在发呆/自我思考"

        path_parts = focus_path.split(".")
        if len(path_parts) >= 2:
            # entity_uid 就是我们的 focus_path
            entity_uid = focus_path

            session = self.sessions.get(entity_uid)
            if session and session.conversation_name:
                conv_type_str = "群会话" if session.conversation_type == "group" else "私聊会话"
                return f"{conv_type_str}'{session.conversation_name}'(ID: {session.conversation_info.conversation_id})"  # noqa: E501

            entity_doc = await self.entity_graph_service.get_entity_by_key(entity_uid)
            if entity_doc and isinstance(entity_doc.details, ConversationDetails):
                details = entity_doc.details
                conv_type_str = "群会话" if details.type == "group" else "私聊会话"
                return f"{conv_type_str}'{details.name or details.conversation_id}'(ID: {details.conversation_id})"  # noqa: E501

            logger.warning(f"无法获取会话实体 '{entity_uid}' 的详细信息。")
            return f"一个位于平台'{path_parts[0]}'下的未知会话"

        return "一个未知的地方"

    def get_last_switch_description(self) -> str:
        """获取上次焦点切换的格式化描述."""
        return self._last_switch_description

    async def handle_consciousness_control(self, control_json: dict) -> None:
        """处理来自LLM决策的意识控制指令的总调度中心."""
        if not (command := next(iter(control_json), None)) or not (
            params := control_json.get(command)
        ):
            logger.warning(f"收到的意识控制指令格式不正确或为空: {control_json}")
            return

        logger.info(f"焦点管理器收到指令: {command}, 参数: {params}")
        handler = self._command_handlers.get(command)
        if not handler:
            logger.error(f"收到未知的意识控制指令: '{command}'，无法处理。")
            return

        previous_path_for_desc = self.current_focus_path
        history_entry_base = {
            "timestamp": int(time.time() * 1000),
            "command": command,
            "motivation": params.get("motivation", "没有明确动机"),
        }

        focus_switched = await handler(params, history_entry_base)

        if focus_switched:
            from_desc = await self._get_focus_description(previous_path_for_desc)
            to_desc = await self._get_focus_description(self.current_focus_path)
            self._last_switch_description = f"你刚刚从“{from_desc}”来到了“{to_desc}”"
            logger.info(
                f"AI 决定 [{command}]，{self._last_switch_description} "
                f"(动机: {history_entry_base['motivation']})"
            )
            if self.core_logic:
                self.core_logic.trigger_immediate_thought_cycle()

    async def _handle_push_focus(self, params: dict, history_entry_base: dict) -> bool:
        """处理 'push_focus' 指令."""
        # 在新架构中，target_id 就是完整的 entity_uid
        if not (target_entity_uid := params.get("target_id")):
            logger.error("'push_focus' 指令缺少 'target_id'。")
            return False

        if not await self.get_or_create_session(conversation_id=target_entity_uid):
            logger.error(f"无法 'push_focus'，创建或获取会话实体 '{target_entity_uid}' 失败。")
            return False

        entry_to_push = {**history_entry_base, "target_path": target_entity_uid}
        self.focus_history.append(entry_to_push)
        logger.info(f"[堆栈 PUSH] 焦点下潜至: {target_entity_uid}")
        return True

    async def _handle_pop_focus(self, params: dict, history_entry_base: dict) -> bool:
        """处理 'pop_focus' 指令，从堆栈顶部移除当前焦点."""
        if len(self.focus_history) <= 1:
            logger.warning("在顶层Core-Level尝试执行 'pop_focus'，无效操作，已忽略。")
            return False

        leaving_entry = self.focus_history.pop()
        leaving_path = leaving_entry.get("target_path")
        logger.info(f"[堆栈 POP] 焦点从 '{leaving_path}' 上浮。")

        if leaving_path:
            await self.deactivate_session(
                leaving_path, {"motivation": history_entry_base["motivation"]}
            )

        # 检查上浮后的新焦点是否需要激活
        new_focus_path = (
            self.current_focus_path.get("target_path") if self.current_focus_path else None
        )
        if new_focus_path and new_focus_path != "core":
            await self.get_or_create_session(conversation_id=new_focus_path)
        return True

    async def _handle_swap_focus(self, params: dict, history_entry_base: dict) -> bool:
        """处理 'swap_focus' 指令，在同层级切换焦点."""
        if not (target_entity_uid := params.get("target_id")):
            logger.error("'swap_focus' 指令缺少 'target_id'。")
            return False

        if len(self.focus_history) <= 1:
            logger.error("'swap_focus' 只能在非核心层级使用。")
            return False

        # 先停用当前会话
        leaving_entry = self.focus_history.pop()
        leaving_path = leaving_entry.get("target_path")
        if leaving_path:
            await self.deactivate_session(
                leaving_path,
                {"motivation": history_entry_base["motivation"], "target_id": target_entity_uid},
            )

        # 再激活新会话
        if await self.get_or_create_session(conversation_id=target_entity_uid):
            entry_to_push = {**history_entry_base, "target_path": target_entity_uid}
            self.focus_history.append(entry_to_push)
            logger.info(f"[堆栈 SWAP] 焦点切换至: {target_entity_uid}")
        else:
            logger.error(
                f"无法 'swap_focus'，目标实体 '{target_entity_uid}' "
                f"无法创建会话。切换中止，停留在上层。"
            )

        return True

    # back, teleport_focus, jump_to_history 逻辑相对独立，
    # 暂时保持不变，但其内部调用的 de/activate 已经适配
    async def _handle_back(self, params: dict, history_entry_base: dict) -> bool:
        return await self._handle_jump_to_history(
            {"history_index": -1, **params}, history_entry_base
        )

    async def _handle_teleport_focus(self, params: dict, history_entry_base: dict) -> bool:
        if not (target_path := params.get("target_path")):
            return False
        current_path = (
            self.current_focus_path.get("target_path") if self.current_focus_path else None
        )
        if current_path and current_path != "core":
            await self.deactivate_session(current_path, {"motivation": f"传送到 {target_path}"})
        self.focus_history.clear()
        self.focus_history.append({"target_path": "core", "motivation": "传送起点"})
        self.focus_history.append({**history_entry_base, "target_path": target_path})
        if target_path != "core":
            await self.get_or_create_session(conversation_id=target_path)
        return True

    async def _handle_jump_to_history(self, params: dict, history_entry_base: dict) -> bool:
        try:
            history_index = int(params.get("history_index", -1))
            history_len = len(self.focus_history)
            if not (1 <= abs(history_index) < history_len):
                return False

            target_deque_index = history_index if history_index < 0 else history_index - history_len
            target_entry = self.focus_history[target_deque_index]
            target_path = target_entry.get("target_path")

            current_path = (
                self.current_focus_path.get("target_path") if self.current_focus_path else None
            )
            if current_path and current_path != "core":
                await self.deactivate_session(
                    current_path, {"motivation": f"跳跃到历史焦点 {target_path}"}
                )

            # 弹出直到目标成为栈顶
            while len(self.focus_history) > abs(target_deque_index):
                self.focus_history.pop()

            if target_path and target_path != "core":
                await self.get_or_create_session(conversation_id=target_path)
            return True
        except (IndexError, TypeError, ValueError) as e:
            logger.error(f"处理 'jump_to_history' 时发生错误: {e}")
            return False
