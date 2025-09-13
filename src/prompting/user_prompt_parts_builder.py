# 文件路径: src/prompting/user_prompt_parts_builder.py

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.mind.state_manager import AIStateManager
    from src.services.database.services.thought_storage_service import ThoughtStorageService


class UserPromptPartsBuilder:
    """构建用户提示的各个部分，用于组合成完整的用户提示."""

    def __init__(
        self,
        thought_storage_service: "ThoughtStorageService",
        state_manager: "AIStateManager",
    ) -> None:
        self.thought_storage = thought_storage_service
        self.state_manager = state_manager

    async def build(self, external_info_block: str) -> dict[str, Any]:
        """构建 User Prompt 的所有部分."""
        # 只保留 external_info_block
        # action_response_block 现在是工作记忆的一部分了
        return {
            "action_response_block": "",  # 确保模板中有这个键，但内容为空
            "external_info_block": external_info_block,
        }
