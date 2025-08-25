# src/prompt_builder/user_prompt_parts_builder.py
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from src.core_logic.state_manager import AIStateManager
    from src.database.services.thought_storage_service import ThoughtStorageService
    from src.focus_chat_mode.chat_session import ChatSession


class UserPromptPartsBuilder:
    """[AIC-OS 重构版]负责构建填充 User Prompt 模板所需的所有部分.

    在 AIC-OS 模式下，它的职责被大大简化。
    """

    def __init__(
        self,
        thought_storage_service: "ThoughtStorageService",
        state_manager: "AIStateManager",
    ) -> None:
        self.thought_storage = thought_storage_service
        self.state_manager = state_manager

    async def build(
        self,
        handover_result: dict | None,
        external_info_block: str,
        session: Optional["ChatSession"] = None,
    ) -> dict[str, Any]:
        """构建 User Prompt 的所有部分."""
        action_response_block = await self._build_action_response_desc(handover_result)
        command_feedback_block = self._get_command_feedback_block(session)

        return {
            "action_response_block": action_response_block,
            "command_feedback_block": command_feedback_block,
            "external_info_block": external_info_block,
            # 在 AIC-OS 模式下，以下 block 被废弃，由 external_info_block 统一呈现
            "meta_info_block": "",
            "friend_request_block": "",
        }

    def _get_command_feedback_block(self, session: Optional["ChatSession"]) -> str:
        """获取上一次指令的反馈。这个逻辑可以保持，但需要确保 session 能被正确传递."""
        # TODO: 需要一个全局的反馈机制，因为动作可能不在 session 上下文中执行
        feedback_text = ""
        if session and session.last_command_feedback:
            feedback_text = session.last_command_feedback
            session.last_command_feedback = None

        return f"<command_feedback>\n{feedback_text}\n</command_feedback>" if feedback_text else ""

    async def _build_action_response_desc(self, handover_result: dict | None) -> str:
        """构建动作响应描述."""
        _, action_result_text, action_payload = await self._get_latest_action_context(
            handover_result
        )
        if not action_result_text or "决定不行动" in action_result_text:
            return ""
        platform_key, action_name, action_params = self._parse_action_details_from_payload(
            action_payload
        )
        action_desc = self._create_action_description_prefix(
            platform_key, action_name, action_params
        )
        if action_name == "get_list" and platform_key:
            action_result_text = await self._post_process_get_list_result(
                action_result_text, platform_key
            )
        return f"<action_response>\n{action_desc}\n{action_result_text}\n</action_response>"
