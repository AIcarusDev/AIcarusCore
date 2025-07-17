import time
import json
from typing import TYPE_CHECKING, Any, Optional

from src.common.custom_logging.logging_config import get_logger
from src.common.focus_chat_history_builder.chat_history_formatter import format_chat_history_for_llm
from src.common.time_utils import get_formatted_time_for_llm
from src.config import config
from src.core_logic.internal_info_builder import InternalInfoBuilder
from src.focus_chat_mode.behavioral_guidance_generator import BehavioralGuidanceGenerator
from src.focus_chat_mode.components import PromptComponents
from src.platform_builders.registry import platform_builder_registry
from src.prompt_templates import prompt_templates
from src.prompt_templates.aicarus_rule import AICARUS_RULE
from src.prompt_templates.core_prompts import CORE_BEHAVIOR_GUIDELINES, CORE_INPUT_XML_DESCRIPTION
from src.prompt_templates.focus_chat_prompts import (
    FOCUS_BEHAVIOR_GUIDELINES,
    FOCUS_INPUT_XML_DESCRIPTION,
)
from src.prompt_templates.platform_prompts import PLATFORM_INPUT_XML_DESCRIPTION

if TYPE_CHECKING:
    from src.common.unread_info_service.unread_info_service import UnreadInfoService
    from src.core_communication.core_ws_server import CoreWebsocketServer
    from src.database.services.event_storage_service import EventStorageService
    from src.focus_chat_mode.chat_session import ChatSession
    from src.focus_chat_mode.chat_session_manager import ChatSessionManager

logger = get_logger(__name__)


class ThoughtPromptBuilder:
    """负责构建符合三层信息块模型的系统和用户提示."""

    def __init__(
        self,
        unread_info_service: "UnreadInfoService",
        internal_info_builder: "InternalInfoBuilder",
        event_storage_service: "EventStorageService",
        chat_session_manager: "ChatSessionManager",
        core_ws_server: "CoreWebsocketServer",
    ) -> None:
        """初始化统一的Prompt构建器，注入所有必要的情报来源."""
        self.unread_info_service = unread_info_service
        self.internal_info_builder = internal_info_builder
        self.event_storage = event_storage_service
        self.chat_session_manager = chat_session_manager
        self.core_ws_server = core_ws_server
        self.is_context_switch_flag: bool = False

    async def build_prompts_components(
        self, focus_path: str | None, session: Optional["ChatSession"] = None
    ) -> PromptComponents:
        """第一步：构建思考所需的所有组件，但不最终组装.

        返回一个 PromptComponents 数据容器对象.
        """
        current_level, current_platform_id, current_conv_id = self._parse_focus_path(focus_path)

        builder = platform_builder_registry.get_builder(current_platform_id)
        core_builder = platform_builder_registry.get_builder("core")

        # --- 获取基础的 Schema 定义 ---
        plat_ctrl_schema, _ = (
            builder.get_level_consciousness_controls_definitions(current_level)
            if builder
            else ({}, {})
        )
        core_ctrl_schema, _ = core_builder.get_level_consciousness_controls_definitions(
            current_level
        )
        final_ctrl_schema_props = {
            **core_ctrl_schema.get("properties", {}),
            **plat_ctrl_schema.get("properties", {}),
        }

        # 如果当前在顶层，并且 focus 指令存在，我们动态注入 enum 约束
        if current_level == 'core' and 'focus' in final_ctrl_schema_props:
            # 从注册中心获取所有已注册的平台构建器的ID
            # 我们要排除 'core' 本身，因为它不是一个可以 focus 的外部平台
            all_platform_ids = [
                pid for pid in platform_builder_registry.get_all_builders().keys() if pid != 'core'
            ]

            if all_platform_ids:
                logger.debug(f"动态生成 focus.platform_id 的 enum 列表: {all_platform_ids}")
                # 找到 focus -> properties -> platform_id，然后注入 enum
                focus_properties = final_ctrl_schema_props['focus'].get('properties', {})
                if 'platform_id' in focus_properties:
                    focus_properties['platform_id']['enum'] = all_platform_ids

        # --- 外部行动 Schema ---
        plat_act_schema, _ = (
            builder.get_level_actions_definitions(current_level) if builder else ({}, {})
        )
        core_act_schema, _ = core_builder.get_level_actions_definitions(current_level)
        final_act_schema_props = {
            **core_act_schema.get("properties", {}),
            **plat_act_schema.get("properties", {}),
        }

        # --- 最终响应 Schema ---
        response_schema = {
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
                },
                "consciousness_control": {
                    "type": "object",
                    "properties": final_ctrl_schema_props,
                    "maxProperties": 1,
                },
                "action": {"type": "object", "properties": final_act_schema_props},
            },
            "required": ["internal_state"],
        }

        # --- 组装 System Prompt 的描述部分 ---
        # 注意，这里用的是 get_level_consciousness_controls_descriptions
        # 而不是get_level_consciousness_controls_definitions
        core_ctrl_desc = core_builder.get_level_consciousness_controls_descriptions(current_level)
        if current_level != "core" and builder:
            plat_ctrl_desc = builder.get_level_consciousness_controls_descriptions(current_level)
            available_controls_desc = "\n".join(filter(None, [core_ctrl_desc, plat_ctrl_desc]))
        else:
            available_controls_desc = core_ctrl_desc

        # 注意，这里用的是 get_level_actions_descriptions
        # 而不是 get_level_actions_definitions
        core_act_desc = core_builder.get_level_actions_descriptions(current_level)
        if current_level != "core" and builder:
            plat_act_desc = builder.get_level_actions_descriptions(current_level)
            available_actions_desc = "\n".join(filter(None, [core_act_desc, plat_act_desc]))
        else:
            available_actions_desc = core_act_desc

        system_prompt_blocks = {
            "aicarus_rule_block": AICARUS_RULE,
            "current_time": get_formatted_time_for_llm(),
            "persona_block": self._get_persona_block(),
            "available_platforms_block": await self._get_available_platforms_block(),
            "current_state_block": await self._get_current_state_block(
                current_level, current_platform_id, current_conv_id
            ),
            "behavior_guidelines_block": self._get_behavior_guidelines_block(current_level),
            "input_XML_block_description": self._get_input_xml_block_description(current_level),
            "available_consciousness_controls": available_controls_desc
            or "你当前没有可用的导航指令。",
            "available_actions": available_actions_desc or "你当前没有可用的外部行动。",
        }

        # 3. 构建 User Prompt 的信息块
        internal_info_block = await self.internal_info_builder.build_internal_info_block(
            is_context_switch=self.is_context_switch_flag, session=session
        )
        (
            external_info_block,
            meta_info_block,
            history_components,
        ) = await self._get_external_and_meta_info_blocks(
            current_level, current_platform_id, current_conv_id
        )

        user_prompt_blocks = {
            "external_info_block": external_info_block,
            "meta_info_block": meta_info_block,
            "internal_info_block": internal_info_block,
        }

        # 4. 组装并返回 PromptComponents 数据容器
        return PromptComponents(
            system_prompt_blocks=system_prompt_blocks,
            user_prompt_blocks=user_prompt_blocks,
            response_schema=response_schema,
            last_valid_text_message=history_components.last_valid_text_message
            if history_components
            else None,
            image_references=history_components.image_references if history_components else [],
        )

    def finalize_prompts(self, components: PromptComponents) -> tuple[str, str, dict[str, Any]]:
        """第二步：使用准备好的组件，最终组装系统和用户提示.
        返回系统提示、用户提示和响应Schema的元组。
        """
        system_prompt = prompt_templates.CORE_CYCLE_SYSTEM_PROMPT.format(
            **components.system_prompt_blocks
        )
        user_prompt = prompt_templates.CORE_CYCLE_USER_PROMPT.format(
            **components.user_prompt_blocks
        )

        try:
            schema_json_str = json.dumps(
                components.response_schema,
                ensure_ascii=False,
                indent=2
            )
            logger.debug(
                f"为本次思考生成的 JSON Schema 如下：\n"
                f"==================== RESPONSE SCHEMA ====================\n"
                f"{schema_json_str}\n"
                f"======================================================="
            )
        except Exception as e:
            logger.error(f"序列化 response_schema 用于日志记录时失败: {e}")

        logger.debug(
            f"准备发送给LLM的完整Prompt:\n"
            f"==================== SYSTEM PROMPT ====================\n"
            f"{system_prompt}\n"
            f"==================== USER PROMPT ======================\n"
            f"{user_prompt}\n"
            f"=========================================================="
        )

        return system_prompt, user_prompt, components.response_schema

    # --- 私有辅助方法 ---
    async def get_last_valid_text_message(self, conversation_id: str) -> str | None:
        """
        一个专门的方法，只为获取指定会话的最后一条有效文本消息。
        这在中断检查时非常有用。
        """
        if not conversation_id or not self.chat_session_manager:
            return None

        session = self.chat_session_manager.sessions.get(conversation_id)
        if not session:
            return None

        # 复用 format_chat_history_for_llm 来获取我们想要的信息，但只取所需
        history_components = await format_chat_history_for_llm(
            event_storage=self.event_storage,
            conversation_id=session.conversation_id,
            bot_id=session.bot_id,
            platform=session.platform,
            bot_profile=await session.get_bot_profile(),
            conversation_type=session.conversation_type,
            conversation_name=session.conversation_name,
            last_processed_timestamp=session.last_processed_timestamp,
            is_first_turn=False, # 不需要切换上下文标志
        )
        return history_components.last_valid_text_message

    def _parse_focus_path(self, focus_path: str | None) -> tuple[str, str, str | None]:
        """解析焦点路径，返回层级、平台ID和会话ID."""
        if focus_path and focus_path != "core":
            path_parts = focus_path.split(".")
            current_platform_id = path_parts[0]
            if len(path_parts) >= 2:
                current_level = "cellular"
                current_conv_id = ".".join(path_parts[1:])
            else:
                current_level = "platform"
                current_conv_id = None
        else:
            current_level = "core"
            current_platform_id = "core"
            current_conv_id = None
        return current_level, current_platform_id, current_conv_id

    def _get_persona_block(self) -> str:
        return (
            f'你是"{config.persona.bot_name}"；\n'
            f"{config.persona.description}\n"
            f"{config.persona.profile}"
        )

    async def _get_available_platforms_block(self) -> str:
        """获取当前所有已连接平台的信息."""
        return await self.core_ws_server.get_connected_platforms_info()

    async def _get_current_state_block(
        self, level: str, platform_id: str, conv_id: str | None
    ) -> str:
        """获取当前状态的描述信息."""
        if level == "core":
            return "你当前专注于：发呆/自我思考。"
        elif level == "platform":
            return f"你当前专注于：{platform_id} 平台。"
        elif level == "cellular" and conv_id:
            session = self.chat_session_manager.sessions.get(conv_id)
            if not session:
                return "错误：找不到当前会话的档案。"
            bot_profile = await session.get_bot_profile()
            if session.conversation_type == "group":
                return (
                    f'你当前正在 qq 群"{session.conversation_name or "未知群聊"}"中参与 qq 群聊，'
                    f'你在该群的群名片是"{bot_profile.get("card", config.persona.bot_name)}"'
                )
            else:
                return f"你当前正在 qq 上与{session.conversation_name or '对方'}私聊"
        return "未知状态"

    def _get_behavior_guidelines_block(self, level: str) -> str:
        if level in ["core", "platform"]:
            return CORE_BEHAVIOR_GUIDELINES
        if level == "cellular":
            return FOCUS_BEHAVIOR_GUIDELINES
        return ""

    def _get_input_xml_block_description(self, level: str) -> str:
        if level == "core":
            return CORE_INPUT_XML_DESCRIPTION
        if level == "platform":
            return PLATFORM_INPUT_XML_DESCRIPTION
        if level == "cellular":
            return FOCUS_INPUT_XML_DESCRIPTION
        return ""

    async def _get_external_and_meta_info_blocks(
        self, level: str, platform_id: str, conv_id: str | None
    ) -> tuple[str, str, PromptComponents | None]:
        """根据层级获取外部信息和元信息."""
        external_info = ""
        meta_info = ""
        history_components = None

        if level == "core":
            external_info = await self.unread_info_service.get_platform_summary()
        elif level == "platform":
            external_info = await self.unread_info_service.get_conversation_list_summary(
                platform_id, exclude_conversation_id=None
            )
        elif level == "cellular" and conv_id:
            session = self.chat_session_manager.sessions.get(conv_id)
            if not session:
                return "错误：找不到会话档案，无法构建上下文。", "", None

            bot_profile = await session.get_bot_profile()
            history_components = await format_chat_history_for_llm(
                event_storage=self.event_storage,
                conversation_id=session.conversation_id,
                bot_id=session.bot_id,
                platform=session.platform,
                bot_profile=bot_profile,
                conversation_type=session.conversation_type,
                conversation_name=session.conversation_name,
                last_processed_timestamp=session.last_processed_timestamp,
                is_first_turn=self.is_context_switch_flag,
            )

            # 获取后立即更新时间戳
            if history_components.processed_event_ids:
                # 获取最后一个事件的时间戳
                last_event = await self.event_storage.get_events_by_ids(
                    [history_components.processed_event_ids[-1]]
                )
                if last_event:
                    session.last_processed_timestamp = last_event[0].get(
                        "timestamp", time.time() * 1000
                    )
            else:
                session.last_processed_timestamp = time.time() * 1000

            if history_components.conversation_name:
                session.conversation_name = history_components.conversation_name

            unread_summary_str = await self.unread_info_service.generate_unread_summary_text(
                exclude_conversation_id=session.conversation_id
            )
            external_info = (
                f"{history_components.conversation_info_block}\n"
                f"{history_components.user_list_block}\n"
                f"{history_components.chat_history_log_block}\n"
                f"<unread_summary>\n{unread_summary_str or '所有其他会话均无未读消息。'}\n"
                f"</unread_summary>"
            )

            guidance_generator = BehavioralGuidanceGenerator(session)
            meta_info = guidance_generator.generate_guidance()

        return external_info, meta_info, history_components
