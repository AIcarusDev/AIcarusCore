# 文件路径: src/prompting/orchestrator.py

from typing import TYPE_CHECKING, Any, Optional

from src.prompting.components import PromptComponents
from src.prompting.strategies import IPromptBuildingStrategy
from src.prompting.templates import prompt_templates

if TYPE_CHECKING:
    from src.os.apps.interfaces import ISession


class ThoughtPromptBuilder:
    """
    构建思维提示的策略执行器。
    它不关心如何构建提示，只负责调用当前设置的策略。
    """

    def __init__(self, prompt_strategy: IPromptBuildingStrategy) -> None:
        self._strategy = prompt_strategy
        self.container = None # 保持与旧代码的兼容性，尽管在策略模式下可能不再需要

    async def build_prompts_components(
        self,
        last_external_info_snapshot: Optional[str],
        ui_message: Optional[str] = None,
    ) -> tuple[PromptComponents, Optional["ISession"], dict, str]:
        """直接调用当前策略来构建Prompt组件。"""
        # 将 container 传递给策略（如果策略需要）
        if hasattr(self._strategy, "container"):
            self._strategy.container = self.container

        return await self._strategy.build_prompts_components(
            last_external_info_snapshot, ui_message=ui_message
        )

    def finalize_prompts(self, components: PromptComponents) -> tuple[str, str, dict[str, Any]]:
        """将所有 Prompt 组件格式化为最终的字符串。"""
        system_prompt = prompt_templates.CORE_CYCLE_SYSTEM_PROMPT.format(
            **components.system_prompt_blocks
        )
        user_prompt = prompt_templates.CORE_CYCLE_USER_PROMPT.format(
            **components.user_prompt_blocks
        )
        return system_prompt, user_prompt, components.response_schema
