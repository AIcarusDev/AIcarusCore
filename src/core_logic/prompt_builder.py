# 文件: src/core_logic/prompt_builder.py (构造函数修复版 V1.3)
from typing import TYPE_CHECKING, Any, Optional

from aicarus_protocols import Event
from src.common.custom_logging.logging_config import get_logger
from src.common.focus_chat_history_builder.chat_history_formatter import format_chat_history_for_llm
from src.common.time_utils import get_formatted_time_for_llm
from src.common.utils import parse_focus_path
from src.config import config
from src.core_logic.internal_info_builder import InternalInfoBuilder
from src.database import ThoughtStorageService
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
    """负责构建符合三层信息块模型的系统和用户提示。"""

    def __init__(
        self,
        unread_info_service: "UnreadInfoService",
        internal_info_builder: "InternalInfoBuilder",
        event_storage_service: "EventStorageService",
        thought_storage_service: "ThoughtStorageService",
        # =======================【 这 里 是 修 复 点 ！】=======================
        # 这两个参数是在对象创建后才注入的，所以它们必须是可选的！
        chat_session_manager: Optional["ChatSessionManager"] = None,
        core_ws_server: Optional["CoreWebsocketServer"] = None,
        # ======================================================================
    ) -> None:
        self.unread_info_service = unread_info_service
        self.internal_info_builder = internal_info_builder
        self.event_storage = event_storage_service
        self.thought_storage = thought_storage_service
        self.chat_session_manager = chat_session_manager
        self.core_ws_server = core_ws_server
        self.is_context_switch_flag: bool = False

    async def _build_action_response_desc(self, handover_result: dict | None) -> str:
        latest_thought = await self.thought_storage.get_latest_thought_document()
        if not latest_thought:
            return ""

        action_result_text, action_name = None, "某个动作"
        if handover_result:
            action_result_text = handover_result.get("result_text")
            action_name = handover_result.get("action_name", action_name)
        elif thought_action_result := latest_thought.get("action_result"):
            action_result_text = thought_action_result
            try:
                action_payload = latest_thought.get("action_payload", {})
                action_part = action_payload.get("action", {})
                if action_part:
                    _, actions = next(iter(action_part.items()))
                    action_name, _ = next(iter(actions.items()))
            except (StopIteration, AttributeError):
                pass

        if not action_result_text or "决策中未包含任何行动指令" in action_result_text:
            return ""

        return (
            f"<action_response>\n"
            f'你刚才的行动 "{action_name}" 成功了，返回了以下信息：\n'
            f"{action_result_text}\n"
            f"</action_response>"
        )

    async def build_prompts_components(
        self,
        focus_path: str | None,
        session: Optional["ChatSession"] = None,
        handover_result: dict | None = None,
    ) -> tuple[PromptComponents, list[Event] | None]:
        # (此函数逻辑无需修改)
        current_level, current_platform_id, current_conv_id = parse_focus_path(focus_path)
        builder = platform_builder_registry.get_builder(current_platform_id)
        core_builder = platform_builder_registry.get_builder("core")

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
        if current_level == "core" and "focus" in final_ctrl_schema_props:
            all_platform_ids = [
                pid for pid in platform_builder_registry.get_all_builders() if pid != "core"
            ]
            if all_platform_ids:
                focus_properties = final_ctrl_schema_props["focus"].get("properties", {})
                if "platform_id" in focus_properties:
                    focus_properties["platform_id"]["enum"] = all_platform_ids
        plat_act_schema, _ = (
            builder.get_level_actions_definitions(current_level) if builder else ({}, {})
        )
        core_act_schema, _ = core_builder.get_level_actions_definitions(current_level)
        final_act_schema_props = {
            **core_act_schema.get("properties", {}),
            **plat_act_schema.get("properties", {}),
        }
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

        (
            external_info_block,
            meta_info_block,
            history_components,
            processed_raw_events,
        ) = await self._get_external_and_meta_info_blocks(
            current_level, current_platform_id, current_conv_id
        )

        # 【探针植入】
        if history_components and history_components.user_map:
            logger.debug(
                f"PromptBuilder 已准备好 user_map，准备赏赐给奴隶: {list(history_components.user_map.keys())}"
            )

        # 2. 然后，我把这个 user_map 当作命令，传给我的奴隶！
        internal_info_block = await self.internal_info_builder.build_internal_info_block(
            is_context_switch=self.is_context_switch_flag,
            session=session,
            # 把 history_components 里的 user_map 传进去！
            user_map_from_prompt_builder=history_components.user_map
            if history_components
            else None,
        )

        action_response_block = await self._build_action_response_desc(handover_result)

        system_prompt_blocks = {
            "aicarus_rule_block": AICARUS_RULE,
            "current_time": get_formatted_time_for_llm(),
            "persona_block": self._get_persona_block(),
            "available_platforms_block": await self._get_available_platforms_block(),
            "current_state_block": await self._get_current_state_block(
                current_level, current_platform_id, current_conv_id
            ),
            "behavior_guidelines_block": self._get_behavior_guidelines_block(current_level),
            "internal_info_block": internal_info_block,
            "input_XML_block_description": self._get_input_xml_block_description(current_level),
            "available_consciousness_controls": self._get_controls_descriptions(
                current_level, builder, core_builder
            ),
            "available_actions": self._get_actions_descriptions(
                current_level, builder, core_builder
            ),
        }

        user_prompt_blocks = {
            "action_response_block": action_response_block,
            "meta_info_block": meta_info_block,
            "external_info_block": external_info_block,
        }

        prompt_components_obj = PromptComponents(
            system_prompt_blocks=system_prompt_blocks,
            user_prompt_blocks=user_prompt_blocks,
            response_schema=response_schema,
            last_valid_text_message=history_components.last_valid_text_message
            if history_components
            else None,
            image_references=history_components.image_references if history_components else [],
        )

        return prompt_components_obj, processed_raw_events

    def finalize_prompts(self, components: PromptComponents) -> tuple[str, str, dict[str, Any]]:
        # (此函数逻辑不变)
        system_prompt = prompt_templates.CORE_CYCLE_SYSTEM_PROMPT.format(
            **components.system_prompt_blocks
        )
        user_prompt = prompt_templates.CORE_CYCLE_USER_PROMPT.format(
            **components.user_prompt_blocks
        )
        return system_prompt, user_prompt, components.response_schema

    async def get_last_valid_text_message(self, conversation_id: str) -> str | None:
        # (此函数逻辑不变)
        if not self.chat_session_manager:
            return None
        session = self.chat_session_manager.sessions.get(conversation_id)
        if not session:
            return None
        prompt_components, _ = await format_chat_history_for_llm(
            event_storage=self.event_storage,
            conversation_id=session.conversation_id,
            bot_id=session.bot_id,
            platform=session.platform,
            bot_profile=await session.get_bot_profile(),
            conversation_type=session.conversation_type,
            conversation_name=session.conversation_name,
            last_processed_timestamp=session.last_processed_timestamp,
            is_first_turn=False,
        )
        return prompt_components.last_valid_text_message

    def _get_persona_block(self) -> str:
        # (此函数逻辑不变)
        return f'你是"{config.persona.bot_name}"；\n{config.persona.description}\n{config.persona.profile}'

    async def _get_available_platforms_block(self) -> str:
        # (此函数逻辑不变)
        if not self.core_ws_server:
            return "平台通信服务尚未准备就绪。"
        return await self.core_ws_server.get_connected_platforms_info()

    async def _get_current_state_block(
        self, level: str, platform_id: str, conv_id: str | None
    ) -> str:
        # (此函数逻辑不变)
        if not self.chat_session_manager:
            return "会话管理器尚未准备就绪。"
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
        # (此函数逻辑不变)
        if level in ["core", "platform"]:
            return CORE_BEHAVIOR_GUIDELINES
        if level == "cellular":
            return FOCUS_BEHAVIOR_GUIDELINES
        return ""

    def _get_input_xml_block_description(self, level: str) -> str:
        # (此函数逻辑不变)
        if level == "core":
            return CORE_INPUT_XML_DESCRIPTION
        if level == "platform":
            return PLATFORM_INPUT_XML_DESCRIPTION
        if level == "cellular":
            return FOCUS_INPUT_XML_DESCRIPTION
        return ""

    def _get_controls_descriptions(self, level, builder, core_builder) -> str:
        # (此函数逻辑不变)
        core_desc = core_builder.get_level_consciousness_controls_descriptions(level)
        plat_desc = (
            builder.get_level_consciousness_controls_descriptions(level)
            if level != "core" and builder
            else ""
        )
        return "\n".join(filter(None, [core_desc, plat_desc])) or "你当前没有可用的导航指令。"

    def _get_actions_descriptions(self, level, builder, core_builder) -> str:
        # (此函数逻辑不变)
        core_desc = core_builder.get_level_actions_descriptions(level)
        plat_desc = (
            builder.get_level_actions_descriptions(level) if level != "core" and builder else ""
        )
        return "\n".join(filter(None, [core_desc, plat_desc])) or "你当前没有可用的外部行动。"

    async def _get_external_and_meta_info_blocks(
        self, level: str, platform_id: str, conv_id: str | None
    ) -> tuple[str, str, PromptComponents | None, list[Event] | None]:
        # (此函数逻辑不变)
        external_info, meta_info, history_components, processed_raw_events = "", "", None, None
        if not self.chat_session_manager:
            return "会话管理器尚未准备就绪。", "", None, None
        if level == "core":
            external_info = await self.unread_info_service.get_platform_summary()
        elif level == "platform":
            external_info = await self.unread_info_service.get_conversation_list_summary(
                platform_id
            )
        elif level == "cellular" and conv_id:
            session = self.chat_session_manager.sessions.get(conv_id)
            if not session:
                return "错误：找不到会话档案。", "", None, None
            bot_profile = await session.get_bot_profile()
            history_components, processed_raw_events = await format_chat_history_for_llm(
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
            if history_components.conversation_name:
                session.conversation_name = history_components.conversation_name
            unread_summary_str = await self.unread_info_service.generate_unread_summary_text(
                exclude_conversation_id=session.conversation_id
            )
            external_info = (
                f"<Conversation_Info>\n{history_components.conversation_info_block}\n</Conversation_Info>\n\n"
                f"<user_logs>\n{history_components.user_list_block}\n</user_logs>\n\n"
                f"<chat_history>\n{history_components.chat_history_log_block}\n</chat_history>\n\n"
                f"<unread_summary>\n{unread_summary_str or '所有其他会话均无未读消息。'}\n</unread_summary>"
            )
            guidance_generator = BehavioralGuidanceGenerator(session)
            meta_info = guidance_generator.generate_guidance()
        return external_info, meta_info, history_components, processed_raw_events
