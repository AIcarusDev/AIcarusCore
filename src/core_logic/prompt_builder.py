# src/core_logic/prompt_builder.py
from typing import Any

from src.common.custom_logging.logging_config import get_logger
from src.config import config
from src.core_logic.state_manager import AIStateManager  # 导入状态管理器
from src.core_logic.unread_info_service import UnreadInfoService  # 导入 UnreadInfoService
from src.prompt_templates import prompt_templates  # 导入新模板

logger = get_logger(__name__)


class ThoughtPromptBuilder:
    """
    哼，专门负责构建思考时用的Prompt，别来烦我。
    我只负责拼接，材料都让 state_manager 和 unread_info_service 给我准备好。
    """

    def __init__(
        self,
        unread_info_service: UnreadInfoService,
        state_manager: AIStateManager,
    ) -> None:
        """
        初始化 ThoughtPromptBuilder。
        """
        self.unread_info_service = unread_info_service
        self.state_manager = state_manager
        # 缓存机器人档案，免得每次都查
        self.bot_profile_cache: dict[str, str | None] = {
            "id": None,
            "nickname": None,
        }

    async def _get_bot_profile(self) -> dict[str, str | None]:
        """获取并缓存机器人档案，懒得每次都去问。"""
        # 简单实现：目前只从config读。未来可以扩展成从适配器动态获取。
        if not self.bot_profile_cache.get("id"):
            self.bot_profile_cache["id"] = config.persona.qq_id
            self.bot_profile_cache["nickname"] = config.persona.bot_name
        return self.bot_profile_cache

    async def build_prompts(self, current_time_str: str) -> tuple[str, str, dict[str, Any]]:
        """
        构建System和User的Prompt，返回一个包含所有填充块的字典。
        """
        # 1. 准备 System Prompt 的材料
        bot_profile = await self._get_bot_profile()
        system_prompt = prompt_templates.CORE_SYSTEM_PROMPT.format(
            current_time=current_time_str,
            bot_name=config.persona.bot_name,
            optional_description=config.persona.description,
            optional_profile=config.persona.profile,
            bot_id=bot_profile.get("id", "未知ID"),
            bot_nickname=bot_profile.get("nickname", "未知昵称"),
        )

        # 2. 从 StateManager 获取状态块
        state_blocks = await self.state_manager.get_current_state_for_prompt()

        # 3. 获取未读消息摘要
        unread_summary = await self.unread_info_service.generate_unread_summary_text()
        state_blocks["unread_summary"] = unread_summary or "所有会话均无未读消息。"

        # 4. 组装 User Prompt
        user_prompt = prompt_templates.CORE_USER_PROMPT.format(**state_blocks)

        return system_prompt, user_prompt, state_blocks
