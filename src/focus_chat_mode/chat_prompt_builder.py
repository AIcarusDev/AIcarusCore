# src/focus_chat_mode/chat_prompt_builder.py
from typing import TYPE_CHECKING, Any

from src.platform_builders.registry import platform_builder_registry
from src.common.custom_logging.logging_config import get_logger
from src.common.focus_chat_history_builder.chat_history_formatter import format_chat_history_for_llm
from src.common.time_utils import get_formatted_time_for_llm
from src.config import config
from src.database.services.event_storage_service import EventStorageService
from src.prompt_templates import prompt_templates
from src.prompt_templates.aicarus_rule import AICARUS_RULE
from src.core_logic.internal_info_builder import InternalInfoBuilder
from src.prompt_templates.focus_chat_prompts import (
    FOCUS_BEHAVIOR_GUIDELINES,
    FOCUS_INPUT_XML_DESCRIPTION,
)
if TYPE_CHECKING:
    from .chat_session import ChatSession

logger = get_logger(__name__)


class ChatPromptBuilder:
    """专注聊天模式下的Prompt构建器，遵循三层信息块模型."""

    def __init__(
        self,
        session: "ChatSession",
        event_storage: EventStorageService,
        internal_info_builder: InternalInfoBuilder,
    ) -> None:
        self.session = session
        self.event_storage = event_storage
        self.internal_info_builder = internal_info_builder
        logger.info(f"[ChatPromptBuilder][{self.session.conversation_id}] 实例已创建。")

    def _get_persona_block(self) -> str:
        """构建角色信息块."""
        description = config.persona.description or ""
        profile = config.persona.profile or ""
        return f'你是"{config.persona.bot_name}"；\n{description}\n{profile}'

    async def _get_available_platforms_block(self) -> str:
        """构建可用平台信息块."""
        if hasattr(self.session, 'core_logic') and self.session.core_logic.core_comm_layer:
            # 同样加上 await！
            return await self.session.core_logic.core_comm_layer.get_connected_platforms_info()
        logger.warning("无法通过 session 访问到 core_comm_layer，返回硬编码的平台信息。")
        return "你暂时没有可用平台，可能是与平台连接断开或程序刚刚启动，请稍等。"

    async def _get_current_state_block(self) -> str:
        """构建底层会话的当前状态信息块."""
        bot_profile = await self.session.get_bot_profile()
        if self.session.conversation_type == "group":
            conversation_details = await self.session.get_conversation_details()
            return (f'你当前正在 qq 群"{self.session.conversation_name or "未知群聊"}"中参与 qq 群聊，'
                    f'（该群现在包括你共有{conversation_details.get("member_count", "未知")}个成员）\n'
                    f'你在该群的群名片是"{bot_profile.get("card", config.persona.bot_name)}"')
        else: # private
            user_nick = self.session.conversation_name or "对方"
            return f"你当前正在 qq 上与{user_nick}私聊"

    def _get_behavior_guidelines_block(self) -> str:
        """为底层会话构建包含链式指令的行为准则块."""
        return FOCUS_BEHAVIOR_GUIDELINES

    def _get_input_xml_block_description(self) -> str:
        """为底层会话构建输入XML块描述."""
        return FOCUS_INPUT_XML_DESCRIPTION

    async def build_prompts(
        self,
        focus_path: str,
        last_processed_timestamp: float,
        is_context_switch: bool = False,
    ) -> tuple[str, str, dict[str, Any]]:
        """
        构建专注聊天模式下给LLM的System Prompt和User Prompt。
        返回: (system_prompt, user_prompt, state_for_log)
        """
        logger.debug(f"[{self.session.conversation_id}] ChatPromptBuilder 开始构建Prompt...")

        # 1. 获取层级专属的动作/意识控制描述
        path_parts = focus_path.split('.')
        current_platform_id = path_parts[0]
        current_level = "cellular"
        builder = platform_builder_registry.get_builder(current_platform_id)
        available_controls_desc, available_actions_desc = "你当前没有可用的导航指令。", "你当前没有可用的外部行动。"
        if builder:
            controls_desc, actions_desc = builder.get_level_specific_descriptions(current_level)
            if controls_desc: available_controls_desc = controls_desc
            if actions_desc: available_actions_desc = actions_desc

        # 2. 构建所有 System Prompt 的信息块
        system_prompt_blocks = {
            "aicarus_rule_block": AICARUS_RULE,
            "current_time": get_formatted_time_for_llm(),
            "persona_block": self._get_persona_block(),
            "available_platforms_block": self._get_available_platforms_block(),
            "current_state_block": await self._get_current_state_block(),
            "behavior_guidelines_block": self._get_behavior_guidelines_block(),
            "input_XML_block_description": self._get_input_xml_block_description(),
            "available_consciousness_controls": available_controls_desc,
            "available_actions": available_actions_desc,
        }

        # 3. 填充 System Prompt
        system_prompt = prompt_templates.CORE_CYCLE_SYSTEM_PROMPT.format(**system_prompt_blocks)

        # 4. 构建 User Prompt 的信息块
        # 4.1 external_info_block
        bot_profile = await self.session.get_bot_profile()
        history_components = await format_chat_history_for_llm(
            event_storage=self.event_storage,
            conversation_id=self.session.conversation_id,
            bot_id=self.session.bot_id,
            platform=self.session.platform,
            bot_profile=bot_profile,
            conversation_type=self.session.conversation_type,
            conversation_name=self.session.conversation_name,
            last_processed_timestamp=last_processed_timestamp,
            is_first_turn=is_context_switch,
        )
        # 确保调用的是正确的方法
        unread_summary_str = await self.session.core_logic.unread_info_service.generate_unread_summary_text(
            exclude_conversation_id=self.session.conversation_id
        )
        external_info_block = (
            f"{history_components.conversation_info_block}\n"
            f"{history_components.user_list_block}\n"
            f"{history_components.chat_history_log_block}\n"
            f"<unread_summary>\n{unread_summary_str or '所有其他会话均无未读消息。'}\n</unread_summary>"
        )

        # 4.2 meta_info_block
        meta_info_block = self.session.guidance_generator.generate_guidance()

        # 4.3 internal_info_block
        internal_info_block = await self.internal_info_builder.build_internal_info_block(
            is_context_switch=is_context_switch
        )

        user_prompt_blocks = {
            "external_info_block": external_info_block,
            "meta_info_block": meta_info_block,
            "internal_info_block": internal_info_block,
        }

        # 5. 组装 User Prompt
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
