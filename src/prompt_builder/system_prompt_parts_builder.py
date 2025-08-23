# src/prompt_builder/system_prompt_parts_builder.py
import asyncio
import json
import re
from typing import TYPE_CHECKING, Any, Optional

from src.common.time_utils import get_formatted_time_for_llm
from src.common.utils import build_conversation_entity_uid
from src.config import config
from src.database.models import ConversationDetails
from src.platform_builders.base_builder import BasePlatformBuilder
from src.platform_builders.core_builder import CoreBuilder
from src.platform_builders.registry import platform_builder_registry
from src.prompt_templates.core_prompts import CORE_BEHAVIOR_GUIDELINES, CORE_INPUT_XML_DESCRIPTION
from src.prompt_templates.focus_chat_prompts import (
    FOCUS_BEHAVIOR_GUIDELINES,
    FOCUS_INPUT_XML_DESCRIPTION,
)
from src.prompt_templates.platform_prompts import PLATFORM_INPUT_XML_DESCRIPTION

from .error import (
    InvalidConversationIdError,
    SessionManagerError,
    SessionNotFoundError,
)

if TYPE_CHECKING:
    from src.action.action_handler import ActionHandler
    from src.core_communication.core_ws_server import CoreWebsocketServer
    from src.core_logic.internal_info_builder import InternalInfoBuilder
    from src.core_logic.state_manager import AIStateManager
    from src.database.services.entity_graph_service import EntityGraphService
    from src.focus_chat_mode.chat_session import ChatSession
    from src.focus_chat_mode.chat_session_manager import ChatSessionManager


class SystemPromptPartsBuilder:
    """负责构建填充 System Prompt 模板所需的所有部分."""

    def __init__(
        self,
        internal_info_builder: "InternalInfoBuilder",
        state_manager: "AIStateManager",
        chat_session_manager: "ChatSessionManager",
        core_ws_server: "CoreWebsocketServer",
        action_handler: "ActionHandler",
        entity_service: "EntityGraphService",
    ) -> None:
        self.internal_info_builder = internal_info_builder
        self.state_manager = state_manager
        self.chat_session_manager = chat_session_manager
        self.core_ws_server = core_ws_server
        self.action_handler = action_handler
        self.entity_service = entity_service

    async def build(
        self,
        level: str,
        platform_id: str,
        conv_id: str | None,
        session: Optional["ChatSession"],
        user_map: dict | None,
        can_go_back: bool,
        is_context_switch_flag: bool,
    ) -> dict[str, Any]:
        """构建并返回一个包含 System Prompt 所有组件的字典."""
        # --- Parallel Data Fetching ---
        internal_info_task = self.internal_info_builder.build_internal_info_block(
            is_context_switch=is_context_switch_flag,
            session=session,
            user_map_from_prompt_builder=user_map,
        )
        platforms_task = self._get_available_platforms_block()
        state_task = self._get_current_state_block(level, platform_id, conv_id)
        stickers_task = self._get_sticker_collection_block(platform_id)
        memories_task = self._build_working_memories_block()

        (
            internal_info_block,
            available_platforms_block,
            current_state_block,
            sticker_collection_block,
            working_memories_block,
        ) = await asyncio.gather(
            internal_info_task, platforms_task, state_task, stickers_task, memories_task
        )

        # --- Synchronous Logic ---
        deliberation_summary_block = self._get_deliberation_summary_block(session)
        current_goals_block = self.state_manager.goal_manager.get_formatted_goals()
        core_builder = platform_builder_registry.get_builder("core")
        builder = platform_builder_registry.get_builder(platform_id)

        return {
            "aicarus_rule_block": self._get_aicarus_rule(),
            "current_time": get_formatted_time_for_llm(),
            "persona_block": self._get_persona_block(),
            "sticker_collection_block": sticker_collection_block,
            "available_platforms_block": available_platforms_block,
            "current_state_block": current_state_block,
            "deliberation_summary_block": deliberation_summary_block,
            "working_memories_block": working_memories_block,
            "current_goals_block": current_goals_block,
            "behavior_guidelines_block": self._get_behavior_guidelines_block(level),
            "internal_info_block": internal_info_block,
            "input_XML_block_description": self._get_input_xml_block_description(level),
            "available_consciousness_controls": self._get_controls_descriptions(
                level, builder, core_builder, can_go_back=can_go_back
            ),
            "available_actions": self._get_actions_descriptions(level, builder, core_builder),
        }

    def _get_aicarus_rule(self) -> str:
        from src.prompt_templates.aicarus_rule import AICARUS_RULE

        return AICARUS_RULE

    def _get_persona_block(self) -> str:
        return (
            f'你是"{config.persona.bot_name}"；\n'
            f"{config.persona.description}\n"
            f"{config.persona.profile}"
        )

    async def _get_available_platforms_block(self) -> str:
        if not self.core_ws_server:
            return "平台通信服务尚未准备就绪。"
        return await self.core_ws_server.get_connected_platforms_info()

    async def _get_current_state_block(
        self, level: str, platform_id: str, conv_id: str | None
    ) -> str:
        """构建当前状态块."""
        if not self.chat_session_manager:
            raise SessionManagerError("ChatSessionManager 尚未初始化。")
        if level == "core":
            return "你当前似乎没有干什么。"

        if level == "platform":
            return f"你当前专注于：{platform_id} 平台。"

        if level != "cellular" or not conv_id:
            return "未知状态"
        try:
            if "." not in conv_id:
                raise InvalidConversationIdError(
                    f"无效的会话ID格式 '{conv_id}'。它必须是 'type.id' 格式。"
                )
            conv_type, actual_id = conv_id.split(".", 1)
            session_key = build_conversation_entity_uid(platform_id, conv_type, actual_id)

        except (ValueError, IndexError):
            raise InvalidConversationIdError(
                f"无法从会话部分 '{conv_id}' 解析出类型和ID。"
            ) from None

        session = self.chat_session_manager.sessions.get(session_key)

        if not session:
            raise SessionNotFoundError(f"找不到会话实体UID为 '{session_key}' 的活跃会话档案。")

        if session.membership_status == "left":
            return (
                f"你当前正在观察一个你【已退出】的QQ群"
                f' "{session.conversation_name or "未知群聊"}"。'
                f"你无法在此发送消息或进行任何互动，只能回顾历史消息。"
            )
        bot_profile = await session.get_bot_profile()
        if session.conversation_type == "group":
            group_name = session.conversation_name or "未知群聊"
            bot_card = bot_profile.get("card")
            bot_nickname = bot_profile.get("nickname")

            # 如果没有群名片，或者群名片等于机器人的昵称（没有特殊设置群名片），则不显示群名片部分
            if not bot_card or (bot_nickname and bot_card == bot_nickname):
                return f'你当前正在 qq 群"{group_name}"中参与 qq 群聊。'
            else:
                return (
                    f'你当前正在 qq 群"{group_name}"中参与 qq 群聊，你在该群的群名片是"{bot_card}"'
                )
        is_temporary = session.conversation_info.extra.get("is_temporary", False)

        if not is_temporary:
            return f"你当前正在 qq 上与{session.conversation_name or '对方'}私聊"

        source_group_id = session.conversation_info.extra.get("source_group_id")

        source_group_name = "未知群聊"
        if source_group_id:
            source_group_entity_uid = build_conversation_entity_uid(
                session.platform, "group", source_group_id
            )

            source_group_entity = await self.entity_service.get_entity_by_key(
                source_group_entity_uid
            )
            if source_group_entity and isinstance(source_group_entity.details, ConversationDetails):
                source_group_name = source_group_entity.details.name or source_group_id
        return (
            f"你当前正在 qq 上处理"
            f"来自“{source_group_name}”群聊中“{session.conversation_name or '对方'}”的临时会话私聊"
        )

    async def _build_working_memories_block(self) -> str:
        """构建 <working_memories> 块，包含最近的行动和意识控制历史."""
        recent_thoughts = await self.state_manager.thought_service.get_recent_thought_documents(
            limit=8, max_age_seconds=60
        )

        if not recent_thoughts:
            return ""

        # FIX 1: 移除外层标签，模板中已有
        memory_lines = [
            '  <desc>以下是你的有印象/记得的，之前自己做的事。</desc>',
        ]

        for i, thought in enumerate(recent_thoughts):
            payload = thought.get("action_payload")

            # FIX 2: 增强过滤逻辑，确保 payload 包含有效指令
            if not payload or (
                not payload.get("action") and not payload.get("consciousness_control")
            ):
                continue

            # 移除 internal_state，因为它太大且与此处目的无关
            payload.pop("internal_state", None)

            # 再次检查，如果移除后只剩下 thought_id，也跳过
            if set(payload.keys()) <= {"thought_id"}:
                continue

            try:
                # FIX 3: 使用 separators 参数生成最紧凑的单行 JSON
                json_content = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                memory_lines.append(f'  <memory cycle_ago="{i + 1}">')
                memory_lines.append(f'    <![CDATA[{json_content}]]>')
                memory_lines.append("  </memory>")
            except (TypeError, ValueError):
                continue

        if len(memory_lines) == 1:  # 只有 <desc>，没有实际内容
            return ""

        return "\n".join(memory_lines)

    def _get_deliberation_summary_block(self, session: Optional["ChatSession"]) -> str:
        """构建慢脑思考决策摘要块."""
        if session and session.working_memory:
            remaining = session.working_memory.get("remaining_turns", 0)
            if remaining > 0:
                summary = session.working_memory.get("summary", "无内容。")
                session.working_memory["remaining_turns"] -= 1
                return (
                    f'<deliberation_summary duration="{remaining}_cycles">\n'
                    f"<!-- 这是你仔细思考后的总结，将在 {remaining} 轮思考后遗忘 -->\n"
                    f"{summary}\n"
                    f"</deliberation_summary>"
                )
            else:
                session.working_memory.clear()
        return ""

    async def _get_sticker_collection_block(self, platform_id: str) -> str:
        """构建表情包收藏夹块."""
        if platform_id == "qq" and self.action_handler.sticker_service:
            stickers = await self.action_handler.sticker_service.get_all_stickers(platform_id="qq")
            sticker_lines = (
                [f"{s['sticker_id']}: {s['impression']}" for s in stickers]
                if stickers
                else ["你还没有收藏任何表情包。"]
            )
            return (
                f'<sticker_collection_preview filename="qq_stickers_preview.jpg">\n'
                f"<!-- 这是你当前收藏的表情包列表，编号与预览图一一对应 -->\n"
                f"{'\n'.join(sticker_lines)}\n"
                f"</sticker_collection_preview>"
            )
        return ""

    def _get_behavior_guidelines_block(self, level: str) -> str:
        """选择行为准则."""
        return (
            CORE_BEHAVIOR_GUIDELINES if level in {"core", "platform"} else FOCUS_BEHAVIOR_GUIDELINES
        )

    def _get_input_xml_block_description(self, level: str) -> str:
        """获取输入 XML 块描述."""
        return (
            CORE_INPUT_XML_DESCRIPTION
            if level == "core"
            else (
                PLATFORM_INPUT_XML_DESCRIPTION
                if level == "platform"
                else FOCUS_INPUT_XML_DESCRIPTION
            )
        )

    def _get_controls_descriptions(
        self,
        level: str,
        builder: BasePlatformBuilder | None,
        core_builder: CoreBuilder,
        can_go_back: bool,
    ) -> str:
        """获取可用的导航指令描述."""
        schema, _ = core_builder.get_level_consciousness_controls_definitions(level)
        if builder:
            plat_schema, _ = builder.get_level_consciousness_controls_definitions(level)
            schema["properties"].update(plat_schema["properties"])
        available_controls = schema.get("properties", {})
        if not can_go_back:
            available_controls.pop("back", None)
        if level == "cellular":
            available_controls.pop("focus", None)
        descs = [
            f"      - `{name}({', '.join(definition.get('required', []))})`: "
            f"{definition.get('description', '（无可用描述）')}"
            for name, definition in available_controls.items()
        ]
        return "\n".join(sorted(descs)) or "你当前没有可用的导航指令。"

    def _get_actions_descriptions(
        self, level: str, builder: BasePlatformBuilder | None, core_builder: CoreBuilder
    ) -> str:
        """获取可用的行动描述."""
        descs = []
        if core_desc := core_builder.get_level_actions_descriptions(level):
            descs.append(f"- 基础能力:\n{re.sub(r'(`)(\\w+)', r'\\1core.\\2', core_desc)}")
        if (
            level != "core"
            and builder
            and (plat_desc := builder.get_level_actions_descriptions(level))
        ):
            descs.append(
                f"- 平台 '{builder.platform_id}' 专属能力:\n"
                f"{re.sub(r'(`)(\\w+)', rf'\\1{builder.platform_id}.\\2', plat_desc)}"
            )
        if level == "core" and self.core_ws_server:
            tool_descs = []
            for platform_id in self.core_ws_server.action_sender.connected_adapters:
                if (
                    (p_builder := platform_builder_registry.get_builder(platform_id))
                    and p_builder.is_tool_platform
                    and (tool_actions_desc := p_builder.get_level_actions_descriptions("platform"))
                ):
                    tool_descs.append(
                        f"- 工具平台 '{platform_id}' 提供了以下能力:\n"
                        f"{re.sub(r'(`)(\\w+)', rf'\\1{platform_id}.\\2', tool_actions_desc)}"
                    )
            if tool_descs:
                descs.append("\n".join(tool_descs))
        return "\n".join(filter(None, descs)).strip() or "你当前没有可用的外部行动。"
    