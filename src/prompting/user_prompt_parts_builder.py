# 文件路径: src/prompting/user_prompt_parts_builder.py

from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from src.mind.state_manager import AIStateManager
    from src.os.apps.qq.qq_chat_session import ChatSession
    from src.services.database.services.thought_storage_service import ThoughtStorageService


class UserPromptPartsBuilder:
    """[AIC-OS 重构版]负责构建填充 User Prompt 模板所需的所有部分."""

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

        return {
            "action_response_block": action_response_block,
            "external_info_block": external_info_block,
        }

    async def _build_action_response_desc(self, handover_result: dict | None) -> str:
        """构建动作响应描述."""
        latest_thought = await self.thought_storage.get_latest_thought_document()
        if not latest_thought:
            return ""

        action_result_text = latest_thought.get("action_result")
        if not action_result_text or "决定不行动" in action_result_text:
            return ""

        # 在 AIC-OS 模式下，我们简化描述，只显示结果
        return f"<action_response>\n{action_result_text}\n</action_response>"
