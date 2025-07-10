# src/core_logic/prompt_builder.py
from typing import Any

from src.common.custom_logging.logging_config import get_logger
from src.common.unread_info_service.unread_info_service import UnreadInfoService  # 导入未读消息服务
from src.config import config
from src.core_logic.state_manager import AIStateManager  # 导入状态管理器
from src.prompt_templates import prompt_templates  # 导入新模板
from src.platform_builders.registry import platform_builder_registry

logger = get_logger(__name__)


class ThoughtPromptBuilder:
    """负责构建主意识的系统和用户提示.

    这个类会从状态管理器获取当前状态，并从未读消息服务获取未读消息摘要，
    然后将这些信息填充到系统和用户提示模板中。它还会缓存机器人的档案信息，
    以避免每次都去查询.

    Attributes:
        unread_info_service (UnreadInfoService): 用于获取未读消息摘要的服务实例.
        state_manager (AIStateManager): 用于获取当前状态的状态管理器实例.
        bot_profile_cache (dict[str, str | None]): 缓存机器人的档案信息，包含ID和昵称.
    """

    def __init__(
        self,
        unread_info_service: UnreadInfoService,
        state_manager: AIStateManager,
    ) -> None:
        """初始化 ThoughtPromptBuilder."""
        self.unread_info_service = unread_info_service
        self.state_manager = state_manager
        # 缓存机器人档案，免得每次都查
        self.bot_profile_cache: dict[str, str | None] = {
            "id": None,
            "nickname": None,
        }

    async def _get_bot_profile(self) -> dict[str, str | None]:
        """获取并缓存机器人档案，懒得每次都去问."""
        # 简单实现：目前只从config读。未来可以扩展成从适配器动态获取。
        if not self.bot_profile_cache.get("id"):
            self.bot_profile_cache["id"] = config.persona.qq_id
            self.bot_profile_cache["nickname"] = config.persona.bot_name
        return self.bot_profile_cache

    async def build_prompts(self, current_time_str: str, focus_path: str | None) -> tuple[str, str, dict[str, Any], dict[str, Any]]:
        """
        构建System和User的Prompt，并动态生成最终的Response Schema。
        返回: (system_prompt, user_prompt, response_schema, state_blocks)
        """
        # 1. 确定当前层级和平台 (这部分逻辑不变)
        if focus_path:
            path_parts = focus_path.split('.')
            current_platform_id = path_parts[0]
            current_level = "cellular" if len(path_parts) > 2 else "platform"
        else:
            current_platform_id = "core"
            current_level = "core"

        logger.info(f"PromptBuilder: 当前焦点层级: {current_level}, 平台: {current_platform_id}")

        # 2. 获取层级专属的【描述】
        builder = platform_builder_registry.get_builder(current_platform_id)
        available_controls_desc = "你当前没有可用的导航指令。"
        available_actions_desc = "你当前没有可用的外部行动。"
        if builder:
            controls_desc, actions_desc = builder.get_level_specific_descriptions(current_level)
            if controls_desc: available_controls_desc = controls_desc
            if actions_desc: available_actions_desc = actions_desc

        # 3. 填充System Prompt
        bot_profile = await self._get_bot_profile()
        system_prompt_template = prompt_templates.CORE_SYSTEM_PROMPT # 仍然可以先用一个通用的
        system_prompt = system_prompt_template.format(
            current_time=current_time_str,
            bot_name=config.persona.bot_name,
            optional_description=config.persona.description,
            optional_profile=config.persona.profile,
            bot_id=bot_profile.get("id", "未知ID"),
            bot_nickname=bot_profile.get("nickname", "未知昵称"),
            available_consciousness_controls_desc=available_controls_desc,
            available_actions_desc=available_actions_desc
        )

        # 4. 获取层级专属的【Schema】并组装最终的 Response Schema
        final_response_schema = {
            "type": "object",
            "properties": {
                "internal_state": {
                    "type": "object",
                    "properties": {
                        "mood": {"type": "string"},
                        "think": {"type": "string"},
                        "goal": {"type": "string"},
                    },
                    "required": ["mood", "think", "goal"],
                }
            },
            "required": ["internal_state"]
        }

        if builder:
            controls_schema, actions_schema = builder.get_level_specific_definitions(current_level)
            if controls_schema and controls_schema.get("properties"):
                final_response_schema["properties"]["consciousness_control"] = controls_schema
            if actions_schema and actions_schema.get("properties"):
                final_response_schema["properties"]["action"] = actions_schema

        # 5. 构建 User Prompt (这部分不变)
        state_blocks = await self.state_manager.get_current_state_for_prompt()

        # 3. 获取未读消息摘要
        unread_summary = await self.unread_info_service.generate_unread_summary_text()
        state_blocks["unread_summary"] = unread_summary or "所有会话均无未读消息。"

        # 4. 组装 User Prompt
        user_prompt = prompt_templates.CORE_USER_PROMPT.format(**state_blocks)

        logger.debug(
            f"[顶层会话]  - 准备发送给LLM的完整Prompt:\n"
            f"==================== SYSTEM PROMPT (顶层会话) ====================\n"
            f"{system_prompt}\n"
            f"==================== USER PROMPT (顶层会话) ======================\n"
            f"{user_prompt}\n"
            f"=================================================================="
        )
        logger.debug(f"动态生成的最终Response Schema: {final_response_schema}")

        return system_prompt, user_prompt, final_response_schema, state_blocks
