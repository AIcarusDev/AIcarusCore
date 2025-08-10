# src/focus_chat_mode/focus_manager.py
import asyncio
import time
from collections import deque
from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING, Any, Optional

from src.common.custom_logging.logging_config import get_logger
from src.common.utils import build_conversation_entity_uid, parse_focus_path
from src.database.models import ConversationDetails
from src.platform_builders.registry import platform_builder_registry

if TYPE_CHECKING:
    from src.database.services.entity_graph_service import EntityGraphService
    from src.focus_chat_mode.chat_session_manager import ChatSessionManager

    from .chat_session import ChatSession


logger = get_logger(__name__)


class FocusManager:
    """专门负责管理“意识焦点”，包括焦点的切换、历史记录和状态."""

    def __init__(
        self,
        chat_session_manager: "ChatSessionManager",
        entity_graph_service: "EntityGraphService",
        trigger_thought_cycle_callback: Callable[[], None],
    ) -> None:
        self.chat_session_manager = chat_session_manager
        self.entity_graph_service = entity_graph_service
        self.trigger_thought_cycle_callback = trigger_thought_cycle_callback
        self.lock = asyncio.Lock()

        # 1. focus_history 是纯粹的历史日志
        self.focus_history = deque(maxlen=10)

        # 2. current_focus 是一个字典，包含当前的注意力焦点状态
        self.current_focus: dict[str, Any] = {
            "target_path": "core",
            "motivation": "初始化",
            "timestamp": int(time.time() * 1000),
        }
        self.focus_history.append(self.current_focus)  # 初始化时，日志和状态一致

        self._last_switch_description: str = "你刚刚从发呆的状态中回过神来"
        self._command_handlers: dict[
            str, Callable[[dict, dict], Coroutine[Any, Any, tuple[bool, str]]]
        ] = {
            "focus": self._handle_focus,
            "return": self._handle_return,
            "back": self._handle_back,
            "shift_focus": self._handle_shift_focus,
            "teleport_focus": self._handle_teleport_focus,
            "jump_to_history": self._handle_jump_to_history,
        }
        logger.info("FocusManager 初始化完成。")

    @property
    def current_focus_path(self) -> dict[str, Any] | None:
        """属性：返回当前的注意力焦点状态字典."""
        return self.current_focus

    async def handle_focus_control(self, command: str, params: dict) -> tuple[bool, str | None]:
        """处理来自ChatSessionManager的意识控制指令."""
        handler = self._command_handlers.get(command)
        if not handler:
            logger.error(f"收到未知的注意力管理指令: '{command}'，无法处理。")
            return False, f"未知的意识控制指令: '{command}'。"

        previous_path_for_desc = self.current_focus_path
        history_entry_base = {
            "timestamp": int(time.time() * 1000),
            "command": command,
            "motivation": params.get("motivation", "没有明确动机"),
        }

        switched, feedback_message = await handler(params, history_entry_base)

        if switched:
            from_desc = await self._get_focus_description(previous_path_for_desc)
            to_desc = await self._get_focus_description(self.current_focus_path)
            self._last_switch_description = f"你刚刚从“{from_desc}”来到了“{to_desc}”"
            new_session = await self._get_session_from_path(
                self.current_focus_path.get("target_path")
            )
            if new_session:
                new_session.last_command_feedback = self._last_switch_description
            logger.info(
                f"AI 决定 [{command}]，{self._last_switch_description} "
                f"(动机: {history_entry_base['motivation']})"
            )
            self.trigger_thought_cycle_callback()
            return True, None
        else:
            detailed_feedback = (
                f"[ERROR] 你刚才的指令 '{command}' 执行失败 | "
                f"错误信息: {feedback_message},"
                f"参数: {params}。"
            )

            current_session = await self._get_session_from_path(
                self.current_focus_path.get("target_path")
            )
            if current_session:
                current_session.last_command_feedback = detailed_feedback
            else:
                # 如果不在任何会话中（即在 core 或 platform 层），则使用全局反馈槽
                self.chat_session_manager.global_command_feedback = detailed_feedback

            logger.warning(f"意识转向指令 '{command}' 执行失败: {feedback_message}")
            # 注意：返回给上层的原始 feedback_message 保持不变，只修改注入到 Prompt 的内容
            return False, feedback_message

    async def _get_session_from_path(self, focus_path: str | None) -> Optional["ChatSession"]:
        """一个辅助函数，根据焦点路径安全地获取会话实例."""
        if not focus_path:
            return None

        level, platform_id, conv_part = parse_focus_path(focus_path)
        if level != "cellular" or not platform_id or not conv_part:
            return None

        try:
            # 确保 conv_part 包含 "."，否则 split 会抛出 ValueError
            if "." not in conv_part:
                logger.warning(f"无法解析会话路径部分 '{conv_part}'，缺少分隔符。")
                return None
            conv_type, actual_id = conv_part.split(".", 1)
            entity_uid = build_conversation_entity_uid(platform_id, conv_type, actual_id)
            return self.chat_session_manager.sessions.get(entity_uid)
        except (ValueError, IndexError):
            return None

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
            try:
                conv_type, actual_id = conv_id.split(".", 1)
                entity_uid = build_conversation_entity_uid(platform_id, conv_type, actual_id)
                session = self.chat_session_manager.sessions.get(entity_uid)
                if session and session.conversation_name:
                    conv_type_str = "群会话" if session.conversation_type == "group" else "私聊会话"
                    return (
                        f"{conv_type_str}'{session.conversation_name}'"
                        f"(ID: {session.conversation_info.conversation_id})"
                    )
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
            except (ValueError, IndexError):
                logger.error(f"解析会话路径 '{focus_path}' 失败。")
                return f"一个位于平台'{platform_id}'下的未知会话"

        return f"一个未知的地方: {focus_path}"

    def get_last_switch_description(self) -> str:
        """获取上次注意力切换的格式化描述."""
        return self._last_switch_description

    async def _switch_focus(self, new_path: str, history_entry_base: dict) -> tuple[bool, str]:
        """核心切换逻辑：停用旧会话，激活新会话，更新状态和日志."""
        old_focus_entry = self.current_focus
        old_path = old_focus_entry.get("target_path")

        if old_path == new_path:
            logger.info(f"目标焦点 '{new_path}' 与当前焦点相同，无需切换。")
            return False, "目标焦点与当前焦点相同。"

        new_level, new_platform, new_conv_part = parse_focus_path(new_path)
        if new_level == "cellular" and new_platform and new_conv_part:
            try:
                if "." not in new_conv_part:
                    raise ValueError("路径必须包含会话类型")
                new_conv_type, new_actual_id = new_conv_part.split(".", 1)
                new_entity_uid = f"{new_platform}_{new_conv_type}_{new_actual_id}"
                if not await self.chat_session_manager.get_or_create_session(new_entity_uid):
                    error_msg = f"激活新会话 '{new_entity_uid}' 失败！"
                    logger.error(f"{error_msg} 将回退到上一焦点 '{old_path}'。")
                    return False, error_msg
            except (ValueError, IndexError) as e:
                error_msg = f"无法从新路径 '{new_path}' 中解析并激活会话: {e}。"
                logger.error(f"{error_msg} 将回退。")
                return False, error_msg

        old_level, old_platform, old_conv_part = parse_focus_path(old_path)
        if old_level == "cellular" and old_platform and old_conv_part:
            try:
                if "." in old_conv_part:
                    old_conv_type, old_actual_id = old_conv_part.split(".", 1)
                    old_entity_uid = f"{old_platform}_{old_conv_type}_{old_actual_id}"
                    await self.chat_session_manager.deactivate_session(
                        old_entity_uid, history_entry_base
                    )
            except (ValueError, IndexError) as e:
                logger.warning(f"无法从旧路径 '{old_path}' 中解析并停用会话: {e}。")

        if old_level == "platform" and old_platform:
            self.chat_session_manager.clear_platform_view_state(old_platform)
        # 如果是进入平台层，初始化新的视图状态
        if new_level == "platform" and new_platform:
            self.chat_session_manager.initialize_platform_view_state(new_platform)

        new_focus_entry = {**history_entry_base, "target_path": new_path}
        self.current_focus = new_focus_entry
        self.focus_history.append(new_focus_entry)

        return True, "SUCCESS"

    def _is_platform_id(self, target_id: str) -> bool:
        """辅助函数，判断一个ID是否为平台ID."""
        return target_id in platform_builder_registry.get_all_builders()

    async def _handle_focus(self, params: dict, history_entry_base: dict) -> tuple[bool, str]:
        """处理 'focus' 指令."""
        target_id = params.get("target_id")
        if not target_id:
            return False, "缺少 target_id 参数。"

        current_path = self.current_focus.get("target_path", "core")
        level, platform_id, _ = parse_focus_path(current_path)
        new_path = None
        error_message = None

        if level == "core":
            if self._is_platform_id(target_id):
                new_path = target_id
            else:
                try:
                    p_id, conv_type, actual_id = target_id.split("_", 2)
                    if self._is_platform_id(p_id):
                        new_path = f"{p_id}.{conv_type}.{actual_id}"
                    else:
                        error_message = f"目标ID '{target_id}' 的平台部分 '{p_id}' 不是已知平台。"
                except ValueError:
                    # {# FIX: 恢复详细的错误信息 #}
                    error_message = (
                        f"目标ID '{target_id}' 既不是有效的平台ID，"
                        f"也不是格式正确的会话实体UID (platform_type_id)。"
                    )
                    logger.error(f"在顶层(core)执行 focus 失败：{error_message}")
        elif level == "platform":
            try:
                p_id, conv_type, actual_id = target_id.split("_", 2)
                if p_id == platform_id:
                    new_path = f"{p_id}.{conv_type}.{actual_id}"
                else:
                    error_message = (
                        f"不能从平台 '{platform_id}' focus 到另一个平台 '{p_id}' 的会话。"
                    )
            except ValueError:
                error_message = (
                    f"在平台 '{platform_id}' 层，"
                    f"focus 的 target_id '{target_id}' 不是有效的会话实体UID。"
                )
                logger.error(error_message)

        if new_path:
            return await self._switch_focus(new_path, history_entry_base)

        final_error = error_message or f"在层级 '{level}' 执行 focus 失败。"
        logger.error(final_error)
        return False, final_error

    async def _handle_return(self, params: dict, history_entry_base: dict) -> tuple[bool, str]:
        """处理 'return' 指令."""
        current_path = self.current_focus.get("target_path", "core")
        if current_path == "core":
            # {# FIX: 恢复详细的错误信息 #}
            error_message = "该状态执行 'return' 为无效操作，已忽略。"
            logger.warning("在顶层Core-Level尝试执行 'return'，无效操作，已忽略。")
            return False, error_message

        path_parts = current_path.split(".")
        parent_path = path_parts[0] if len(path_parts) > 1 else "core"

        return await self._switch_focus(parent_path, history_entry_base)

    async def _handle_shift_focus(self, params: dict, history_entry_base: dict) -> tuple[bool, str]:
        """处理 'shift_focus' 指令."""
        target_id = params.get("target_id")
        if not target_id:
            return False, "缺少 target_id"

        current_path = self.current_focus.get("target_path", "core")
        level, platform_id, _ = parse_focus_path(current_path)

        if level != "cellular":
            error_message = "'shift_focus' 只能在会话中使用，当前状态不支持。"
            logger.error(f"'shift_focus' 只能在会话层级使用，当前层级为 '{level}'。")
            return False, error_message

        try:
            p_id, conv_type, actual_id = target_id.split("_", 2)
            if p_id != platform_id:
                # {# FIX: 恢复详细的错误信息 #}
                error_message = (
                    f"无法在平台 '{platform_id}' 切换到另一个平台 '{p_id}' 的会话。"
                    "请使用 teleport_focus。"
                )
                logger.error(error_message)
                return False, error_message

            new_path = f"{p_id}.{conv_type}.{actual_id}"
            return await self._switch_focus(new_path, history_entry_base)
        except ValueError:
            error_message = f"shift_focus 的 target_id '{target_id}' 不是有效的会话实体UID。"
            logger.error(error_message)
            return False, error_message

    async def _handle_teleport_focus(
        self, params: dict, history_entry_base: dict
    ) -> tuple[bool, str]:
        """处理 'teleport_focus' 指令."""
        if target_path := params.get("target_path"):
            return await self._switch_focus(target_path, history_entry_base)
        return False, "缺少 target_path 参数。"

    async def _handle_back(self, params: dict, history_entry_base: dict) -> tuple[bool, str]:
        """处理 'back' 指令."""
        try:
            if len(self.focus_history) < 2:
                return False, "'back' 没有可用（除T-0外）的历史记录。"
            target_entry = self.focus_history[-2]
            target_path = target_entry.get("target_path", "core")
            return await self._switch_focus(target_path, history_entry_base)
        except Exception as e:
            logger.error(f"处理 'back' 指令时发生错误: {e}")
            return False, f"处理 'back' 指令时发生错误: {e}"

    async def _handle_jump_to_history(
        self, params: dict, history_entry_base: dict
    ) -> tuple[bool, str]:
        """处理 'jump_to_history' 指令."""
        try:
            history_index = int(params.get("history_index", 0))
            history_len = len(self.focus_history)
            deque_index = -history_index
            if not (1 <= history_index < history_len):
                error_message = f"历史索引 T-{history_index} 超出范围 [T-1, T-{history_len - 1}]。"
                logger.error(error_message)
                return False, error_message
            target_entry = self.focus_history[deque_index]
            target_path = target_entry.get("target_path", "core")
            return await self._switch_focus(target_path, history_entry_base)
        except (IndexError, TypeError, ValueError) as e:
            return False, f"处理 'jump_to_history' 时发生错误: {e}"
