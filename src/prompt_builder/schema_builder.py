# src/prompt_builder/schema_builder.py
from typing import TYPE_CHECKING, Any

from src.common.custom_logging.logging_config import get_logger
from src.common.utils import build_conversation_entity_uid
from src.platform_builders.base_builder import BasePlatformBuilder
from src.platform_builders.registry import platform_builder_registry

if TYPE_CHECKING:
    from src.core_communication.core_ws_server import CoreWebsocketServer
    from src.focus_chat_mode.chat_session_manager import ChatSessionManager

logger = get_logger(__name__)


class SchemaBuilder:
    """负责根据当前上下文，动态构建 LLM 响应所需的 JSON Schema."""

    def __init__(
        self,
        chat_session_manager: "ChatSessionManager",
        core_ws_server: "CoreWebsocketServer",
    ) -> None:
        self.chat_session_manager = chat_session_manager
        self.core_ws_server = core_ws_server

    def build_response_schema(
        self, level: str, platform_id: str, conv_id: str | None, can_go_back: bool
    ) -> dict[str, Any]:
        """构建 LLM 响应的 JSON Schema."""
        builder = platform_builder_registry.get_builder(platform_id)
        core_builder = platform_builder_registry.get_builder("core")

        # --- 步骤 1: 获取当前会话实例 (如果适用) ---
        session = None
        if level == "cellular" and self.chat_session_manager and conv_id:
            try:
                if "." not in conv_id:
                    raise ValueError("会话部分必须是 'type.id' 格式")
                conv_type, actual_id = conv_id.split(".", 1)
                session_key = build_conversation_entity_uid(platform_id, conv_type, actual_id)
                session = self.chat_session_manager.sessions.get(session_key)
            except (ValueError, IndexError):
                logger.warning(f"无法从 conv_id '{conv_id}' 解析会话，将使用默认动作列表。")

        # --- 步骤 2: 构建意识控制 (Consciousness Controls) 的 Schema ---
        consciousness_controls_schema, _ = (
            core_builder.get_level_consciousness_controls_definitions(level)
        )
        if builder:
            plat_controls_schema, _ = builder.get_level_consciousness_controls_definitions(level)
            consciousness_controls_schema["properties"].update(plat_controls_schema["properties"])

        self._filter_navigation_controls(consciousness_controls_schema["properties"], can_go_back)

        if level == "cellular":
            consciousness_controls_schema["properties"].pop("focus", None)

        # --- 步骤 3: 构建外部动作 (Action) 的 Schema，并根据会话状态进行动态修改 ---
        action_properties = self._build_action_schema_properties(level, builder)

        if session and session.membership_status == "left":
            logger.info(
                f"会话 '{session.conversation_id}' 处于“只读观察模式”，正在从可用动作列表中移除互动类指令..."  # noqa: E501
            )
            interactive_actions_to_remove = [
                "send_message",
                "poke_user",
                "kick_member",
                "ban_member",
                "ban_all_members",
                "set_member_card",
                "set_member_title",
            ]
            if platform_id in action_properties and "properties" in action_properties[platform_id]:
                for action_name in interactive_actions_to_remove:
                    action_properties[platform_id]["properties"].pop(action_name, None)

        return {
            "type": "object",
            "properties": {
                "internal_state": {
                    "type": "object",
                    "properties": {
                        "mood": {
                            "type": "string",
                            "description": "你当前的情绪状态和原因，是你的第一本能反应，可以适当衔接`<history_internal_info>`中你之前的心情",  # noqa: E501
                        },
                        "think": {
                            "type": "string",
                            "description": "你当前的内心想法。它应该是对当前所有情况的反应和思考，你的思考过程应该**自然、连贯且丰富**。在这里，你可以分析自己的情绪，揣测他人的意图，对未来的行动进行规划或犹豫。且应该衔接`<history_internal_info>`中你之前的内心想法",  # noqa: E501
                        },
                        "intent": {
                            "type": "string",
                            "description": "你当前的意图，是短期的、直接的、主观的意图或打算。",
                        },
                    },
                    "required": ["mood", "think", "intent"],
                },
                "consciousness_control": consciousness_controls_schema,
                "action": {"type": "object", "properties": action_properties},
            },
            "required": ["internal_state"],
        }

    def _build_action_schema_properties(
        self, level: str, builder: BasePlatformBuilder | None
    ) -> dict:
        """[Helper] 构建动作部分的 JSON Schema properties."""
        core_builder = platform_builder_registry.get_builder("core")
        core_act_schema, _ = core_builder.get_level_actions_definitions(level)

        action_props = {"core": core_act_schema}

        if builder and level != "core":
            plat_act_schema, _ = builder.get_level_actions_definitions(level)
            action_props[builder.platform_id] = plat_act_schema

        if level == "core" and self.core_ws_server:
            connected_adapter_ids = self.core_ws_server.action_sender.connected_adapters.keys()
            for pid in connected_adapter_ids:
                p_builder = platform_builder_registry.get_builder(pid)
                if p_builder and p_builder.is_tool_platform:
                    tool_schema, _ = p_builder.get_level_actions_definitions("platform")
                    action_props[pid] = tool_schema

        return action_props

    def _filter_navigation_controls(self, properties: dict[str, Any], can_go_back: bool) -> None:
        """[Helper] 根据历史记录情况，过滤掉 'back' 指令."""
        if not can_go_back:
            properties.pop("back", None)
