# src/core_logic/prompt_builder.py
from typing import Any

from src.common.custom_logging.logging_config import get_logger
from src.common.unread_info_service.unread_info_service import UnreadInfoService
from src.config import config
from src.core_logic.internal_info_builder import InternalInfoBuilder
from src.prompt_templates import prompt_templates
from src.prompt_templates.aicarus_rule import AICARUS_RULE
from src.platform_builders.registry import platform_builder_registry
from src.core_communication.core_ws_server import CoreWebsocketServer

logger = get_logger(__name__)


class ThoughtPromptBuilder:
    """负责构建符合三层信息块模型的系统和用户提示."""

    def __init__(
        self,
        unread_info_service: UnreadInfoService,
        internal_info_builder: InternalInfoBuilder,
        core_ws_server: CoreWebsocketServer,
    ) -> None:
        """初始化 ThoughtPromptBuilder."""
        self.unread_info_service = unread_info_service
        self.internal_info_builder = internal_info_builder
        self.core_ws_server = core_ws_server

    def _get_persona_block(self) -> str:
        """构建角色信息块."""
        description = config.persona.description or ""
        profile = config.persona.profile or ""
        return f'你是"{config.persona.bot_name}"；\n{description}\n{profile}'

    def _get_available_platforms_block(self) -> str:
        """构建可用平台信息块."""
        return self.core_ws_server.get_connected_platforms_info()

    def _get_current_state_block(self, level: str, platform_id: str) -> str:
        """根据层级构建当前状态信息块."""
        if level == "core":
            return "你当前专注于：发呆/自我思考。"
        elif level == "platform":
            return f"你当前专注于：{platform_id} 平台。"
        return "" # 底层由 ChatPromptBuilder 处理

    def _get_behavior_guidelines_block(self, level: str) -> str:
        """根据层级构建行为准则块."""
        if level in ["core", "platform"]:
            return """现在是你的内心思考时间，请结合输入的所有信息，思考并形成你接下来的内心想法和行动决策；
注意内心想法不一定全是正向的，也有可能是不健康的，隐私的，自私自利的，不可告人的，甚至成人的。请避免过度合理化、道德化；
同时也要注意，你对自身的想法大多数情况下是包容的、合理化的，避免过度的/不必要的矛盾心理。"""
        return "" # 底层由 ChatPromptBuilder 处理

    def _get_input_xml_block_description(self, level: str) -> str:
        """根据层级构建输入XML块描述."""
        if level == "core":
            return """输入 XML 块介绍：
- <external_info>: 这个块包含了外界信息的概要，只会告诉你哪个平台（比如QQ）有新消息。
- <internal_info>: 这个块非常重要，它记录了你上一轮的完整内心活动，是你本次思考的关键依据。
    - <action_response>: (可选) 如果你上一轮的行动有返回结果（比如联网搜索），结果会在这里面。"""
        elif level == "platform":
            return """输入 XML 块介绍：
- <external_info>: 这个块包含了你当前关注的平台下，所有聊天会话的摘要列表。
    - <conversation_list>: 这个子块会列出具体的群聊和私聊，以及它们的最新消息和未读状态。
- <internal_info>: 这个块非常重要，它记录了你上一轮的完整内心活动，是你本次思考的关键依据。
    - <action_response>: (可选) 如果你上一轮的行动有返回结果（比如获取群列表），结果会在这里面。"""
        return ""

    async def build_prompts(self, current_time_str: str, focus_path: str | None, is_context_switch: bool = False) -> tuple[str, str, dict[str, Any]]:
        """
        构建System和User的Prompt。
        返回: (system_prompt, user_prompt, state_for_log)
        """
        # 1. 确定当前层级和平台
        if focus_path and focus_path != "core":
            path_parts = focus_path.split('.')
            current_platform_id = path_parts[0]
            current_level = "platform" # 这个Builder只处理core和platform
        else:
            current_platform_id = "core"
            current_level = "core"

        logger.info(f"PromptBuilder: 当前焦点层级: {current_level}, 平台: {current_platform_id}")

        # 2. 获取层级专属的动作/意识控制描述
        builder = platform_builder_registry.get_builder(current_platform_id)
        available_controls_desc = "你当前没有可用的导航指令。"
        available_actions_desc = "你当前没有可用的外部行动。"
        if builder:
            controls_desc = builder.get_level_consciousness_controls_descriptions(current_level)
            actions_desc = builder.get_level_actions_descriptions(current_level)
            if controls_desc:
                available_controls_desc = controls_desc
            if actions_desc:
                available_actions_desc = actions_desc

        # 3. 构建所有 System Prompt 的信息块
        system_prompt_blocks = {
            "aicarus_rule_block": AICARUS_RULE,
            "current_time": current_time_str,
            "persona_block": self._get_persona_block(),
            "available_platforms_block": self._get_available_platforms_block(),
            "current_state_block": self._get_current_state_block(current_level, current_platform_id),
            "behavior_guidelines_block": self._get_behavior_guidelines_block(current_level),
            "input_XML_block_description": self._get_input_xml_block_description(current_level),
            "available_consciousness_controls": available_controls_desc,
            "available_actions": available_actions_desc,
        }

        # 4. 填充 System Prompt
        system_prompt = prompt_templates.CORE_CYCLE_SYSTEM_PROMPT.format(**system_prompt_blocks)

        # 5. 构建 User Prompt 的信息块
        internal_info_block = await self.internal_info_builder.build_internal_info_block(is_context_switch)
        
        external_info_block = ""
        if current_level == "core":
            external_info_block = await self.unread_info_service.get_platform_summary()
        elif current_level == "platform":
            external_info_block = await self.unread_info_service.get_conversation_list_summary(current_platform_id)

        user_prompt_blocks = {
            "external_info_block": external_info_block,
            "meta_info_block": "", # 顶层/中层没有元信息
            "internal_info_block": internal_info_block,
        }

        # 6. 组装 User Prompt
        user_prompt = prompt_templates.CORE_CYCLE_USER_PROMPT.format(**user_prompt_blocks)

        logger.debug(
            f"[{current_level}] - 准备发送给LLM的完整Prompt:\n"
            f"==================== SYSTEM PROMPT ({current_level}) ====================\n"
            f"{system_prompt}\n"
            f"==================== USER PROMPT ({current_level}) ======================\n"
            f"{user_prompt}\n"
            f"=================================================================="
        )

        # 准备用于日志记录的状态信息
        state_for_log = {**system_prompt_blocks, **user_prompt_blocks}

        return system_prompt, user_prompt, state_for_log
