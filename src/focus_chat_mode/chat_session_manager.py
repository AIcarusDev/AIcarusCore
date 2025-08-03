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
        self.platform_view_states: dict[str, dict[str, Any]] = {}

        # 1. focus_history 现在是纯粹的历史日志
        self.focus_history = deque(maxlen=10)

        # 2. current_focus 是一个字典，包含当前的注意力焦点状态
        self.current_focus: dict[str, Any] = {
            "target_path": "core",
            "motivation": "初始化",
            "timestamp": int(time.time() * 1000),
        }
        self.focus_history.append(self.current_focus)  # 初始化时，日志和状态一致

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
        """属性：返回当前的注意力焦点状态字典."""
        return self.current_focus

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

            new_session = ChatSession(  # 保存到局部变量，以便后续探针
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
            conv_type, actual_id = conv_id.split(".", 1)
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
        """获取上次注意力切换的格式化描述."""
        return self._last_switch_description

    async def handle_consciousness_control(self, control_json: dict) -> None:
        """处理来自LLM决策的意识控制指令的总调度中心."""
        if not (command := next(iter(control_json), None)) or not (
            params := control_json.get(command)
        ):
            logger.warning(f"收到的意识控制指令格式不正确或为空: {control_json}")
            return

        logger.info(f"注意力管理器收到指令: {command}, 参数: {params},正在试图处理...")
        handler = self._command_handlers.get(command)
        if not handler:
            logger.error(f"收到未知的注意力管理指令: '{command}'，无法处理。")
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

    async def _switch_focus(self, new_path: str, history_entry_base: dict) -> bool:
        """核心切换逻辑：停用旧会话，激活新会话，更新状态和日志.

        Args:
            new_path (str): 新的注意力焦点路径，必须是一个有效的绝对路径.
            history_entry_base (dict): 用于记录堆栈历史的基础条目.

        Returns:
            bool: 是否成功切换焦点，True 表示成功，False 表示失败或回退.
        """
        old_focus_entry = self.current_focus
        old_path = old_focus_entry.get("target_path")

        if old_path == new_path:
            logger.info(f"目标焦点 '{new_path}' 与当前焦点相同，无需切换。")
            return False  # 返回 False 表示没有发生实际的切换

        # 步骤 1: 激活新会话（如果需要）
        new_level, new_platform, new_conv_part = parse_focus_path(new_path)

        if new_level == "cellular" and new_platform and new_conv_part:
            try:
                # 确保 conv_part 是 "type.id" 形式
                if "." not in new_conv_part:
                    raise ValueError(
                        "Cellular level path must contain conversation type, e.g., 'group.123456'"
                    )

                new_conv_type, new_actual_id = new_conv_part.split(".", 1)
                new_entity_uid = f"{new_platform}_{new_conv_type}_{new_actual_id}"

                if not await self.get_or_create_session(new_entity_uid):
                    logger.error(
                        f"激活新会话 '{new_entity_uid}' 失败！将回退到上一焦点 '{old_path}'。"
                    )
                    # 激活失败，回退路径，但不更新历史日志，让AI知道它的指令失败了
                    # 返回 True 是因为状态最终还是变了（虽然是变回去）
                    # 我们这里不修改 self.current_focus，等于状态没变
                    # TODO:这里逻辑可能需要进一步细化，但是当前暂时不做复杂处理
                    return False
            except (ValueError, IndexError) as e:  # 捕获可能因为解析错误导致的异常
                logger.error(f"无法从新路径 '{new_path}' 中解析并激活会话: {e}。将回退。")
                return False

        # 步骤 2: 停用旧会话（如果需要）
        old_level, old_platform, old_conv_part = parse_focus_path(old_path)

        if old_level == "cellular" and old_platform and old_conv_part:
            try:
                # 确保 conv_part 是 "type.id" 形式
                if "." not in old_conv_part:
                    # 如果旧路径格式不对，不能安全地停用，记录警告并跳过停用
                    logger.warning(f"旧路径 '{old_path}' 格式不正确，无法安全停用会话。")
                else:
                    old_conv_type, old_actual_id = old_conv_part.split(".", 1)
                    old_entity_uid = f"{old_platform}_{old_conv_type}_{old_actual_id}"

                    await self.deactivate_session(old_entity_uid, history_entry_base)
            except (ValueError, IndexError) as e:
                logger.warning(f"无法从旧路径 '{old_path}' 中解析并停用会话: {e}。")

        # 步骤 3: 成功切换，更新状态指针和历史日志
        new_focus_entry = {**history_entry_base, "target_path": new_path}
        self.current_focus = new_focus_entry
        self.focus_history.append(new_focus_entry)

        return True  # 返回 True 表示发生了切换

    def _is_platform_id(self, target_id: str) -> bool:
        """辅助函数，判断一个ID是否为平台ID."""
        return target_id in platform_builder_registry.get_all_builders()

    def _is_partial_conversation_id(self, target_id: str) -> bool:
        """辅助函数，判断一个ID是否为部分会话ID（如 "group.123"）."""
        return "." in target_id and (
            target_id.startswith("group.") or target_id.startswith("private.")
        )

    async def _handle_push_focus(self, params: dict, history_entry_base: dict) -> bool:
        """处理 'push_focus' 指令，现在会根据目标ID的类型来决定如何切换焦点."""
        target_id = params.get("target_id")
        if not target_id:
            return False

        current_path = self.current_focus.get("target_path", "core")
        level, platform_id, _ = parse_focus_path(current_path)

        new_path = None

        if level == "core":
            # 1. 优先检查目标ID是否为一个已知的平台ID
            if self._is_platform_id(target_id):
                new_path = target_id
                logger.info(f"顶层跳转：识别到平台ID '{target_id}'，将进入平台层。")
            else:
                # 2. 如果不是平台ID，则尝试将其解析为完整的会话实体UID (格式: platform_type_id)
                try:
                    p_id, conv_type, actual_id = target_id.split("_", 2)
                    # 2.1 验证解析出的平台部分是否有效
                    if self._is_platform_id(p_id):
                        # 2.2 如果有效，直接构建通往细胞层的完整路径
                        new_path = f"{p_id}.{conv_type}.{actual_id}"
                        logger.info(
                            f"顶层跳转：识别到会话实体UID '{target_id}'，将直接进入细胞层。"
                        )
                    else:
                        logger.error(
                            f"顶层跳转失败：'{target_id}' 看起来像会话实体UID，"
                            f"但其平台部分 '{p_id}' 不是已知的平台。"
                        )
                except ValueError:
                    # 3. 如果两种格式都匹配失败，则判定为无效ID
                    logger.error(
                        f"在顶层(core)执行 push_focus 失败：目标ID '{target_id}' "
                        f"既不是有效的平台ID，也不是格式正确的会话实体UID (platform_type_id)。"
                    )

        elif level == "platform":
            try:
                p_id, conv_type, actual_id = target_id.split("_", 2)
                if p_id == platform_id:
                    new_path = f"{p_id}.{conv_type}.{actual_id}"
                else:
                    logger.error(
                        f"无效操作: 不能从平台 '{platform_id}' "
                        f"push_focus 到另一个平台 '{p_id}' 的会话。"
                    )
            except ValueError:
                logger.error(
                    f"在平台 '{platform_id}' 层，"
                    f"push_focus 的 target_id '{target_id}' 不是有效的会话实体UID。"
                )

        if new_path:
            switched = await self._switch_focus(new_path, history_entry_base)
            if switched:
                # 如果是进入平台层，则初始化其视图状态
                new_level, _, _ = parse_focus_path(new_path)
                if new_level == "platform":
                    self.platform_view_states[target_id] = {"scroll_offset": 0}
                    logger.info(f"已为平台 '{target_id}' 初始化视图状态。")
            return switched

        logger.error(f"在层级 '{level}' 执行 push_focus(target_id='{target_id}') 失败。")
        return False

    async def _handle_pop_focus(self, params: dict, history_entry_base: dict) -> bool:
        """处理 'pop_focus' 指令，现在会返回到上一个层级或核心层."""
        current_path = self.current_focus.get("target_path", "core")

        if current_path == "core":
            logger.warning("在顶层Core-Level尝试执行 'pop_focus'，无效操作，已忽略。")
            return False

        # 使用更健壮的路径分割方法来确定父路径
        path_parts = current_path.split(".")

        # 如果路径有多段 (如 'qq.private.12345')，父路径就是第一段 ('qq')
        parent_path = path_parts[0] if len(path_parts) > 1 else "core"

        # 如果是从平台层返回，需要清除视图状态
        if len(path_parts) == 1:
            platform_id = path_parts[0]
            if platform_id in self.platform_view_states:
                del self.platform_view_states[platform_id]
                logger.info(f"已清除平台 '{platform_id}' 的视图状态。")

        return await self._switch_focus(parent_path, history_entry_base)

    async def _handle_swap_focus(self, params: dict, history_entry_base: dict) -> bool:
        """处理 'swap_focus' 指令，切换到指定的会话实体UID.

        如果 target_id 是会话实体UID，则切换到该会话。
        """
        target_id = params.get("target_id")  # target_id 是会话实体UID
        if not target_id:
            return False

        current_path = self.current_focus.get("target_path", "core")
        level, platform_id, _ = parse_focus_path(current_path)

        if level != "cellular":
            logger.error(f"'swap_focus' 只能在会话层级使用，当前层级为 '{level}'。")
            return False

        try:
            p_id, conv_type, actual_id = target_id.split("_", 2)
            if p_id != platform_id:
                logger.error(
                    f"无法在平台 '{platform_id}' 切换到另一个平台 '{p_id}' 的会话。"
                    "请使用 teleport_focus。"
                )
                return False
            new_path = f"{p_id}.{conv_type}.{actual_id}"
            return await self._switch_focus(new_path, history_entry_base)
        except ValueError:
            logger.error(f"swap_focus 的 target_id '{target_id}' 不是有效的会话实体UID。")
            return False

    async def _handle_teleport_focus(self, params: dict, history_entry_base: dict) -> bool:
        """处理 'teleport_focus' 指令，直接专注于指定的目标."""
        target_path = params.get("target_path")
        if not target_path:
            return False
        return await self._switch_focus(target_path, history_entry_base)

    async def _handle_back(self, params: dict, history_entry_base: dict) -> bool:
        """处理 'back' 指令，返回到上一个注意力焦点.

        如果当前焦点是核心层，则返回 False。
        """
        if len(self.focus_history) < 2:
            logger.warning("历史记录不足，'back' 操作无法执行。")
            return False
        # 1. 如果当前焦点是核心层，直接返回 False
        target_entry = self.focus_history[-2]  # T-1 是倒数第二个元素
        target_path = target_entry.get("target_path", "core")

        return await self._switch_focus(target_path, history_entry_base)

    async def _handle_jump_to_history(self, params: dict, history_entry_base: dict) -> bool:
        """处理 'jump_to_history' 指令，跳转到指定的历史条目.

        这里的 history_index 是 T-n 的 n，表示从 T-1 开始的偏移量。
        """
        try:
            history_index = int(params.get("history_index", 0))  # 注意，这里history_index是T-n的n
            history_len = len(self.focus_history)

            # 将 T-n 转换为 deque 的负数索引 (-n)
            deque_index = -history_index

            # 验证索引是否在有效范围内 (T-1 到 T-(len-1))
            if not (1 <= history_index < history_len):
                logger.error(f"历史索引 T-{history_index} 超出范围 [T-1, T-{history_len - 1}]。")
                return False
            # 获取目标历史条目
            target_entry = self.focus_history[deque_index]
            target_path = target_entry.get("target_path", "core")

            return await self._switch_focus(target_path, history_entry_base)
        except (IndexError, TypeError, ValueError) as e:
            logger.error(f"处理 'jump_to_history' 时发生错误: {e}")
            return False
