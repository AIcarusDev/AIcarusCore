# src/focus_chat_mode/chat_session_manager.py
import asyncio
import time
from collections import deque
from typing import TYPE_CHECKING, Any, Optional

from src.action.action_handler import ActionHandler
from src.common.custom_logging.logging_config import get_logger
from src.common.utils import parse_focus_path
from src.config.aicarus_configs import FocusChatModeSettings
from src.database.models import ConversationDetails
from src.database.services.event_storage_service import EventStorageService
from src.database.services.summary_storage_service import SummaryStorageService
from src.database.services.thought_storage_service import ThoughtStorageService
from src.llmrequest.llm_processor import Client as LLMProcessorClient
from src.platform_builders.registry import platform_builder_registry

from .chat_session import ChatSession

if TYPE_CHECKING:
    from src.common.intelligent_interrupt_system.intelligent_interrupter import (
        IntelligentInterrupter,
    )
    from src.common.summarization_observation.summarization_service import SummarizationService
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
        self.entity_graph_service = entity_graph_service
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

        logger.info("ChatSessionManager 初始化完成 (嵌套焦点修复版)。")

    @property
    def current_focus_path(self) -> dict[str, Any] | None:
        """属性：返回当前焦点路径（堆栈顶部）."""
        return self.focus_history[-1] if self.focus_history else None

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
                logger.error(f"严重错误：找不到ID为'{conversation_entity_uid}'的会话实体或类型不匹配！")
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
                getattr(conv_entity_doc, "last_read_timestamp", 0.0)
                or time.time() * 1000.0
            )

            self.sessions[conversation_entity_uid] = ChatSession(
                conversation_info=conversation_info_obj,
                conversation_id=conversation_entity_uid,
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
            return self.sessions[conversation_entity_uid]

    async def deactivate_session(
        self,
        conversation_entity_uid: str,
        handover_context: dict | None = None
    ) -> None:
        """处理会话停用，触发最终总结并从管理器中移除会话档案."""
        async with self.lock:
            if session := self.sessions.pop(conversation_entity_uid, None):
                logger.info(f"[SessionManager] 会话实体 '{conversation_entity_uid}' "
                            f"的档案正在被移除。")
                final_timestamp = session.last_processed_timestamp
                await self.entity_graph_service.update_conversation_last_read_timestamp(
                    conversation_entity_uid, final_timestamp
                )
                context = handover_context or {}
                await session.summarization_manager.create_and_save_final_summary(
                    shift_motivation=context.get("motivation"),
                    target_conversation_id=context.get("target_id"),
                )
                logger.info(
                    f"[SessionManager] 会话实体 '{conversation_entity_uid}' 的最终总结已处理，"
                    f"档案已移除。"
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

        level, platform_id, conv_id = parse_focus_path(focus_path)

        if level == "platform":
            return f"平台 '{platform_id}'"

        if level == "cellular" and platform_id and conv_id:
            # 根据路径信息构建完整的会话实体UID
            conv_type, actual_id = conv_id.split('.', 1)
            entity_uid = f"{platform_id}_{conv_type}_{actual_id}"
            # 尝试从会话管理器获取会话实例
            session = self.sessions.get(entity_uid)
            if session and session.conversation_name:
                conv_type_str = "群会话" if session.conversation_type == "group" else "私聊会话"
                return (
                    f"{conv_type_str}'{session.conversation_name}'"
                    f"(ID: {session.conversation_info.conversation_id})"
                )
            # 如果没有会话实例，尝试从实体图服务获取会话实体
            entity_doc = await self.entity_graph_service.get_entity_by_key(entity_uid)
            if entity_doc and isinstance(entity_doc.details, ConversationDetails):
                details = entity_doc.details
                conv_type_str = "群会话" if details.type == "group" else "私聊会话"
                return (
                    f"{conv_type_str}'{details.name or details.conversation_id}'"
                    f"(ID: {details.conversation_id})"
                )

            logger.warning(f"无法获取会话实体 '{entity_uid}' 的详细信息。")
            return f"一个位于平台'{platform_id}'下的未知会话"

        return f"一个未知的地方: {focus_path}"

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

    def _is_platform_id(self, target_id: str) -> bool:
        """[新增] 辅助函数，判断一个ID是否为平台ID."""
        return target_id in platform_builder_registry.get_all_builders()

    def _is_partial_conversation_id(self, target_id: str) -> bool:
        """[新增] 辅助函数，判断一个ID是否为部分会话ID（如 "group.123"）."""
        return (
            '.' in target_id
            and (
                target_id.startswith('group.')
                or target_id.startswith('private.')
            )
        )

    async def _handle_push_focus(self, params: dict, history_entry_base: dict) -> bool:
        """处理 'push_focus'，优先处理 entity_uid，并兼容裸ID和部分路径."""
        if not (target_id := params.get("target_id")):
            logger.error("'push_focus' 指令缺少 'target_id'。")
            return False

        current_path = (
            self.current_focus_path.get("target_path", "core")
            if self.current_focus_path else "core"
        )
        level, platform_id, _ = parse_focus_path(current_path)

        # 场景1: 目标是平台 (e.g., target_id='qq')
        if self._is_platform_id(target_id):
            if level != 'core':
                logger.error(f"只能从 'core' 层级聚焦到平台，当前层级为 '{level}'。")
                return False
            new_path = target_id
            self.focus_history.append({**history_entry_base, "target_path": new_path})
            logger.info(f"[堆栈 PUSH] 焦点下潜至平台: {new_path}")
            return True

        # 统一处理会话ID
        if level != 'platform':
            logger.error(f"只能从 'platform' 层级聚焦到会话，当前层级为 '{level}'。")
            return False

        conversation_entity_uid: str | None = None
        partial_conv_path: str | None = None

        # 场景2 (主要场景): 目标是完整的 entity_uid (e.g., 'qq_group_123')
        if target_id.startswith(f"{platform_id}_"):
            conversation_entity_uid = target_id
            # 从完整的UID中反向构建出部分路径
            try:
                conv_entity = await self.entity_graph_service.get_entity_by_key(
                    conversation_entity_uid
                )
                if conv_entity and isinstance(conv_entity.details, ConversationDetails):
                    details = conv_entity.details
                    partial_conv_path = f"{details.type}.{details.conversation_id}"
                else:
                    logger.error(
                        f"找到了实体 '{conversation_entity_uid}' 但它不是一个有效的会话实体。"
                    )
                    return False
            except ValueError:
                logger.error(f"无法从 entity_uid '{target_id}' 解析出部分路径。")
                return False

        # 场景3 (后备方案): 目标是裸ID (e.g., '123456')
        elif target_id.isdigit():
            logger.info(f"收到裸ID '{target_id}'，正在尝试从数据库推断会话实体...")
            conv_entity = (
                await self.entity_graph_service.find_conversation_entity_by_platform_and_id(
                    platform=platform_id,
                    conversation_id=target_id,
                )
            )
            if conv_entity and isinstance(conv_entity.details, ConversationDetails):
                conversation_entity_uid = conv_entity._key
                details = conv_entity.details
                partial_conv_path = f"{details.type}.{details.conversation_id}"
                logger.success(
                    f"成功推断出裸ID '{target_id}' 对应的实体UID为: "
                    f"'{conversation_entity_uid}'"
                )
            else:
                logger.error(
                    f"无法为裸ID '{target_id}' 在平台 '{platform_id}' "
                    f"下找到对应的会话实体。"
                )
                return False

        # 场景4 (兼容旧格式): 目标是部分路径 (e.g., 'group.123456')
        elif self._is_partial_conversation_id(target_id):
            conv_type, actual_id = target_id.split('.', 1)
            conversation_entity_uid = f"{platform_id}_{conv_type}_{actual_id}"
            partial_conv_path = target_id

        else:
            logger.error(f"未知的 'push_focus' 目标ID格式: '{target_id}'")
            return False

        # --- 后续逻辑统一处理 ---
        if conversation_entity_uid and partial_conv_path:
            if not await self.get_or_create_session(conversation_entity_uid):
                logger.error(
                    f"无法 'push_focus'，创建或获取会话实体 '{conversation_entity_uid}' 失败。"
                )
                return False

            new_path = f"{current_path}.{partial_conv_path}"
            self.focus_history.append({**history_entry_base, "target_path": new_path})
            logger.info(f"[堆栈 PUSH] 焦点下潜至会话: {new_path}")
            return True

        return False

    async def _handle_pop_focus(self, params: dict, history_entry_base: dict) -> bool:
        """处理 'pop_focus' 指令，现在会停用会话."""
        if len(self.focus_history) <= 1:
            logger.warning("在顶层Core-Level尝试执行 'pop_focus'，无效操作，已忽略。")
            return False

        leaving_entry = self.focus_history.pop()
        leaving_path = leaving_entry.get("target_path")
        logger.info(f"[堆栈 POP] 焦点从 '{leaving_path}' 上浮。")

        level, platform_id, conv_id_part = parse_focus_path(leaving_path)

        # 只有当离开的是会话层时，才需要停用 session
        if level == 'cellular' and platform_id and conv_id_part:
            conv_type, actual_id = conv_id_part.split('.', 1)
            conversation_entity_uid = f"{platform_id}_{conv_type}_{actual_id}"
            await self.deactivate_session(
                conversation_entity_uid, {"motivation": history_entry_base["motivation"]}
            )

        return True

    async def _handle_swap_focus(self, params: dict, history_entry_base: dict) -> bool:
        """处理 'swap_focus' 指令，现在支持路径替换."""
        if not (target_id := params.get("target_id")):
            logger.error("'swap_focus' 指令缺少 'target_id'。")
            return False

        if not self._is_partial_conversation_id(target_id):
            logger.error(
                f"'swap_focus' 的目标必须是会话ID (e.g., 'group.123'), 收到: '{target_id}'"
            )
            return False

        current_path = (
            self.current_focus_path.get("target_path", "core")
            if self.current_focus_path else "core"
        )
        level, platform_id, _ = parse_focus_path(current_path)

        if level != 'cellular':
            logger.error(f"'swap_focus' 只能在会话层级使用，当前层级为 '{level}'。")
            return False

        # 停用旧会话
        leaving_entry = self.focus_history.pop()
        leaving_path = leaving_entry.get("target_path")
        _, leaving_platform, leaving_conv_part = parse_focus_path(leaving_path)
        if leaving_platform and leaving_conv_part:
            conv_type, actual_id = leaving_conv_part.split('.', 1)
            leaving_entity_uid = f"{leaving_platform}_{conv_type}_{actual_id}"
            await self.deactivate_session(
                leaving_entity_uid,
                {"motivation": history_entry_base["motivation"], "target_id": target_id},
            )

        # 激活新会话
        new_conv_type, new_actual_id = target_id.split('.', 1)
        new_entity_uid = f"{platform_id}_{new_conv_type}_{new_actual_id}"
        # 如果无法创建或获取新会话，则返回错误
        if not await self.get_or_create_session(new_entity_uid):
            logger.error(f"无法 'swap_focus'，目标实体 '{new_entity_uid}' 无法创建会话。切换中止。")
            # [修复] 切换失败时，应该回到平台层，而不是让堆栈为空
            platform_path = {"target_path": platform_id, "motivation": "切换失败后返回"}
            self.focus_history.append(platform_path)
            return True

        # 构建新路径
        new_path = f"{platform_id}.{target_id}"
        self.focus_history.append({**history_entry_base, "target_path": new_path})
        logger.info(f"[堆栈 SWAP] 焦点切换至: {new_path}")
        return True

    async def _handle_back(self, params: dict, history_entry_base: dict) -> bool:
        """处理 'back' 指令，直接跳转到堆栈顶部的历史焦点."""
        return await self._handle_jump_to_history(
            {"history_index": -1, **params}, history_entry_base
        )

    async def _handle_teleport_focus(self, params: dict, history_entry_base: dict) -> bool:
        """处理 'teleport_focus' 指令，直接跳转到指定路径."""
        if not (target_path := params.get("target_path")):
            return False

        current_entry = self.current_focus_path
        if current_entry and current_entry.get("target_path") != "core":
            level, platform, conv_part = parse_focus_path(current_entry.get("target_path"))
            if level == 'cellular' and platform and conv_part:
                conv_type, actual_id = conv_part.split('.', 1)
                entity_uid = f"{platform}_{conv_type}_{actual_id}"
                await self.deactivate_session(entity_uid, {"motivation": f"传送到 {target_path}"})

        self.focus_history.clear()
        self.focus_history.append({"target_path": "core", "motivation": "传送起点"})

        if target_path != "core":
            self.focus_history.append({**history_entry_base, "target_path": target_path})
            level, platform, conv_part = parse_focus_path(target_path)
            if level == 'cellular' and platform and conv_part:
                conv_type, actual_id = conv_part.split('.', 1)
                entity_uid = f"{platform}_{conv_type}_{actual_id}"
                await self.get_or_create_session(entity_uid)

        return True

    async def _handle_jump_to_history(self, params: dict, history_entry_base: dict) -> bool:
        """处理 'jump_to_history' 指令，跳转到指定的历史焦点."""
        try:
            history_index = int(params.get("history_index", -1))
            history_len = len(self.focus_history)
            if not (1 <= abs(history_index) < history_len):
                return False

            target_deque_index = history_index if history_index < 0 else history_index - history_len
            target_entry = self.focus_history[target_deque_index]
            target_path = target_entry.get("target_path")

            current_entry = self.current_focus_path
            if current_entry and current_entry.get("target_path") != "core":
                level, platform, conv_part = parse_focus_path(current_entry.get("target_path"))
                if level == 'cellular' and platform and conv_part:
                    conv_type, actual_id = conv_part.split('.', 1)
                    entity_uid = f"{platform}_{conv_type}_{actual_id}"
                    await self.deactivate_session(
                        entity_uid,
                        {"motivation": f"跳跃到历史焦点 {target_path}"}
                    )

            while len(self.focus_history) > abs(target_deque_index):
                self.focus_history.pop()

            if target_path and target_path != "core":
                level, platform, conv_part = parse_focus_path(target_path)
                if level == 'cellular' and platform and conv_part:
                    conv_type, actual_id = conv_part.split('.', 1)
                    entity_uid = f"{platform}_{conv_type}_{actual_id}"
                    await self.get_or_create_session(entity_uid)
            return True
        except (IndexError, TypeError, ValueError) as e:
            logger.error(f"处理 'jump_to_history' 时发生错误: {e}")
            return False
