# src/core_logic/prompt_builder.py
import json
import re
import time
from typing import TYPE_CHECKING, Any, Optional

from aicarus_protocols import Event
from src.action.action_handler import ActionHandler
from src.common.custom_logging.logging_config import get_logger
from src.common.focus_chat_history_builder.chat_history_formatter import format_chat_history_for_llm
from src.common.time_utils import (
    format_relative_time,
    format_relative_time_for_attention_log,
    get_formatted_time_for_llm,
)
from src.common.utils import build_conversation_entity_uid, parse_focus_path
from src.config import config
from src.core_logic.internal_info_builder import InternalInfoBuilder
from src.database import EntityGraphService, ThoughtStorageService
from src.database.models import ConversationDetails
from src.focus_chat_mode.behavioral_guidance_generator import BehavioralGuidanceGenerator
from src.focus_chat_mode.components import PromptComponents
from src.platform_builders.base_builder import BasePlatformBuilder
from src.platform_builders.core_builder import CoreBuilder
from src.platform_builders.registry import platform_builder_registry
from src.prompt_templates import prompt_templates
from src.prompt_templates.aicarus_rule import AICARUS_RULE
from src.prompt_templates.core_prompts import CORE_BEHAVIOR_GUIDELINES, CORE_INPUT_XML_DESCRIPTION
from src.prompt_templates.deliberation_prompts import (
    DELIBERATION_RESPONSE_SCHEMA,
    DELIBERATION_SYSTEM_PROMPT,
    DELIBERATION_USER_PROMPT,
)
from src.prompt_templates.focus_chat_prompts import (
    FOCUS_BEHAVIOR_GUIDELINES,
    FOCUS_INPUT_XML_DESCRIPTION,
)
from src.prompt_templates.platform_prompts import PLATFORM_INPUT_XML_DESCRIPTION

if TYPE_CHECKING:
    from src.action.action_handler import ActionHandler
    from src.common.unread_info_service.unread_info_service import UnreadInfoService
    from src.core_communication.core_ws_server import CoreWebsocketServer
    from src.database.services.event_storage_service import EventStorageService
    from src.focus_chat_mode.chat_session import ChatSession
    from src.focus_chat_mode.chat_session_manager import ChatSessionManager

logger = get_logger(__name__)


class PromptBuilderError(Exception):
    """当构建Prompt过程中发生不可恢复的错误时抛出."""

    pass


class ThoughtPromptBuilder:
    """负责构建符合三层信息块模型的系统和用户提示."""

    def __init__(
        self,
        unread_info_service: "UnreadInfoService",
        internal_info_builder: "InternalInfoBuilder",
        event_storage_service: "EventStorageService",
        thought_storage_service: "ThoughtStorageService",
        entity_graph_service: "EntityGraphService",
        action_handler: "ActionHandler",
        chat_session_manager: Optional["ChatSessionManager"] = None,
        core_ws_server: Optional["CoreWebsocketServer"] = None,
    ) -> None:
        self.unread_info_service = unread_info_service
        self.internal_info_builder = internal_info_builder
        self.event_storage = event_storage_service
        self.thought_storage = thought_storage_service
        self.entity_service = entity_graph_service
        self.action_handler = action_handler
        self.chat_session_manager = chat_session_manager
        self.core_ws_server = core_ws_server
        self.is_context_switch_flag: bool = False

    async def build_prompts_components(
        self,
        level: str,
        focus_path: str | None,
        session: Optional["ChatSession"] = None,
        handover_result: dict | None = None,
    ) -> tuple[PromptComponents, list[Event] | None]:
        """编排构建系统和用户提示组件的过程."""
        current_level, current_platform_id, current_conv_id = parse_focus_path(focus_path)

        # 1. 构建外部信息和元信息
        (
            external_info_block,
            meta_info_block,
            history_components,
            processed_raw_events,
        ) = await self._get_external_and_meta_info_blocks(
            current_level, current_platform_id, current_conv_id
        )

        # 2. 构建响应 Schema
        response_schema = self._build_response_schema(current_level, current_platform_id)

        # 3. 构建 System Prompt 的各个部分
        system_prompt_blocks = await self._build_system_prompt_blocks(
            level=current_level,
            platform_id=current_platform_id,
            conv_id=current_conv_id,
            session=session,
            user_map=history_components.user_map if history_components else None,
        )

        # 4. 构建 User Prompt 的各个部分
        user_prompt_blocks = await self._build_user_prompt_blocks(
            handover_result=handover_result,
            meta_info_block=meta_info_block,
            external_info_block=external_info_block,
            platform_id=current_platform_id,
            level=current_level,
        )

        # 5. 组装最终的组件对象
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

    def _build_response_schema(self, level: str, platform_id: str) -> dict[str, Any]:
        """(提取出的新方法) 构建 LLM 响应的 JSON Schema."""
        builder = platform_builder_registry.get_builder(platform_id)
        core_builder = platform_builder_registry.get_builder("core")

        # 构建意识控制 Schema
        plat_ctrl_schema, _ = (
            builder.get_level_consciousness_controls_definitions(level) if builder else ({}, {})
        )
        core_ctrl_schema, _ = core_builder.get_level_consciousness_controls_definitions(level)
        final_ctrl_schema_props = core_ctrl_schema.get("properties", {})
        final_ctrl_schema_props.update(plat_ctrl_schema.get("properties", {}))

        # 构建动作 Schema
        final_act_schema_props = {}
        core_act_schema, _ = core_builder.get_level_actions_definitions(level)
        final_act_schema_props["core"] = core_act_schema

        if builder and level != "core":
            plat_act_schema, _ = builder.get_level_actions_definitions(level)
            final_act_schema_props[builder.platform_id] = plat_act_schema

        if level == "core" and self.core_ws_server:
            connected_adapter_ids = self.core_ws_server.action_sender.connected_adapters.keys()
            for pid in connected_adapter_ids:
                p_builder = platform_builder_registry.get_builder(pid)
                if p_builder and p_builder.is_tool_platform:
                    tool_schema, _ = p_builder.get_level_actions_definitions("platform")
                    final_act_schema_props[pid] = tool_schema

        return {
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

    async def _build_system_prompt_blocks(
        self,
        level: str,
        platform_id: str,
        conv_id: str | None,
        session: Optional["ChatSession"],
        user_map: dict | None,
    ) -> dict[str, Any]:
        """(提取出的新方法) 构建 System Prompt 的所有部分."""
        builder = platform_builder_registry.get_builder(platform_id)
        core_builder = platform_builder_registry.get_builder("core")

        internal_info_block = await self.internal_info_builder.build_internal_info_block(
            is_context_switch=self.is_context_switch_flag,
            session=session,
            user_map_from_prompt_builder=user_map,
        )

        working_memory_block = ""
        if session and session.working_memory:
            remaining = session.working_memory.get("remaining_turns", 0)
            if remaining > 0:
                summary = session.working_memory.get("summary", "无内容。")
                working_memory_block = (
                    f"<!-- 以下是你“慢思考”后的决策摘要，将在 {remaining} 轮思考后遗忘 -->\n"
                    f"<summary_from_deliberation>\n{summary}\n</summary_from_deliberation>"
                )
                session.working_memory["remaining_turns"] -= 1
            else:
                session.working_memory.clear()

        return {
            "aicarus_rule_block": AICARUS_RULE,
            "current_time": get_formatted_time_for_llm(),
            "persona_block": self._get_persona_block(),
            "available_platforms_block": await self._get_available_platforms_block(),
            "current_state_block": await self._get_current_state_block(level, platform_id, conv_id),
            "attentional_trajectory_block": await self._build_attentional_trajectory_block(),
            "working_memory_block": working_memory_block,
            "behavior_guidelines_block": self._get_behavior_guidelines_block(level),
            "internal_info_block": internal_info_block,
            "input_XML_block_description": self._get_input_xml_block_description(level),
            "available_consciousness_controls": self._get_controls_descriptions(
                level, builder, core_builder
            ),
            "available_actions": self._get_actions_descriptions(level, builder, core_builder),
            "self_prompt_block": await self._build_self_prompt_block(),
        }

    async def _build_user_prompt_blocks(
        self,
        handover_result: dict | None,
        meta_info_block: str,
        external_info_block: str,
        platform_id: str,
        level: str,
    ) -> dict[str, Any]:
        """(提取出的新方法) 构建 User Prompt 的所有部分."""
        command_feedback_block = ""
        if self.chat_session_manager and self.chat_session_manager.last_command_feedback:
            feedback_text = self.chat_session_manager.last_command_feedback
            command_feedback_block = f"<command_feedback>\n{feedback_text}\n</command_feedback>"
            self.chat_session_manager.last_command_feedback = None

        action_response_block = await self._build_action_response_desc(handover_result)

        friend_request_block = ""
        if level in ["platform", "cellular"]:
            friend_request_block = await self._build_friend_request_block(platform_id)

        return {
            "action_response_block": action_response_block,
            "command_feedback_block": command_feedback_block,
            "meta_info_block": meta_info_block,
            "external_info_block": external_info_block,
            "friend_request_block": friend_request_block,
        }

    async def _build_action_response_desc(self, handover_result: dict | None) -> str:
        """构建上一个动作的结果描述块."""
        latest_thought = await self.thought_storage.get_latest_thought_document()
        if not latest_thought:
            return ""

        action_result_text = None
        action_payload = latest_thought.get("action_payload") or {}

        if handover_result:
            action_result_text = handover_result.get("result_text")
        elif thought_action_result := latest_thought.get("action_result"):
            action_result_text = thought_action_result

        if not action_result_text or "决策中未包含任何行动指令" in action_result_text:
            return ""

        # 如果没有提供动作结果文本，则使用默认的描述
        action_desc = "你刚才的行动成功了，返回了以下信息："  # 默认回退描述

        try:
            # 1. 尝试从 payload 中解析出平台、动作名和参数
            action_part = action_payload.get("action", {})
            if not action_part or not isinstance(action_part, dict):
                # 如果没有 action 部分或格式不正确，则直接返回默认描述
                return f"<action_response>\n{action_desc}\n{action_result_text}\n</action_response>"

            # 2. 在这里对 action_part 进行规范化处理
            # 检查它是否已经是规范的嵌套结构
            known_platform_keys = platform_builder_registry.get_all_builders().keys()
            is_normalized = any(key in known_platform_keys for key in action_part)

            if not is_normalized:
                # 如果是扁平结构 (如 {"web_search": ...})，则假定它是核心动作并包装它
                action_part = {"core": action_part}

            # 3. 现在可以安全地使用之前的解析逻辑，因为 action_part 结构已统一
            if action_part and isinstance(action_part, dict):
                # 动态获取平台名 (e.g., 'core', 'qq')
                platform_key = next(iter(action_part), None)
                platform_actions = action_part.get(platform_key, {}) if platform_key else {}

                if platform_actions and isinstance(platform_actions, dict):
                    # 动态获取动作名 (e.g., 'web_search', 'get_list')
                    action_name = next(iter(platform_actions), None)
                    action_params = platform_actions.get(action_name, {}) if action_name else {}

                    # 根据解析出的动作名，构建不同的描述
                    if action_name == "web_search" and (query := action_params.get("query")):
                        # 特例：为 web_search 构建包含关键词的丰富描述
                        action_desc = (
                            f"你刚才执行了网页搜索，搜索的关键词是“{query}”，得到了以下结果："
                        )

                    # 特例：为 get_list 修正实体ID格式
                    if action_name == "get_list" and action_result_text:
                        try:
                            # action_result_text 此时是一个JSON字符串，我们先解析它
                            result_list = json.loads(action_result_text)
                            if isinstance(result_list, list):
                                list_type = action_params.get("list_type")  # 获取列表类型
                                # 根据 list_type 修复实体ID格式
                                for item in result_list:
                                    if list_type == "friend" and "user_id" in item:
                                        # 修复好友ID
                                        raw_id = item["user_id"]
                                        item["user_id"] = f"{platform_key}_private_{raw_id}"
                                    elif list_type == "group" and "group_id" in item:
                                        # 修复群聊ID
                                        raw_id = item["group_id"]
                                        item["group_id"] = f"{platform_key}_group_{raw_id}"

                                # 将修复后的列表重新序列化为格式化的JSON字符串
                                action_result_text = json.dumps(
                                    result_list, indent=4, ensure_ascii=False
                                )
                                logger.info(
                                    f"已成功对 get_list (type: {list_type}) "
                                    f"的返回结果进行实体ID格式化。"
                                )
                        except Exception as e_postprocess:
                            logger.warning(f"后处理 get_list 动作结果时失败: {e_postprocess}")
                            # 如果失败，就保持原始结果不变
                    elif action_name:
                        # 通用情况：为所有其他动作构建清晰的描述
                        # 例如: "你刚才执行了动作 “xxx”，得到了以下结果："
                        action_desc = (
                            f"你刚才执行了动作 “{platform_key}.{action_name}”，得到了以下结果："
                        )

        except Exception as e:
            logger.warning(f"解析上一个动作的 payload 以增强描述时出错: {e}。将使用通用描述。")
            # 如果解析过程中出现任何意外，程序不会崩溃，而是安全地使用上面的默认描述

        return f"<action_response>\n{action_desc}\n{action_result_text}\n</action_response>"

    async def _build_attentional_trajectory_block(self) -> str:
        """构建导航日志块，展示最近的注意力焦点历史，并包含相对时间."""
        if not self.chat_session_manager or not hasattr(self.chat_session_manager, "focus_manager"):
            return ""

        history = list(self.chat_session_manager.focus_manager.focus_history)
        if not history:
            return ""

        log_lines = ["<!-- 这是你最近的注意力焦点历史 -->"]
        history_len = len(history)
        current_timestamp_ms = int(time.time() * 1000)

        for i, entry in enumerate(reversed(history)):
            if not isinstance(entry, dict):
                continue

            time_index = history_len - 1 - i
            relative_index = time_index - (history_len - 1)
            index_str = f"T{relative_index}"
            motivation = entry.get("motivation", "未知动机")
            entry_timestamp = entry.get("timestamp", 0)

            time_str = ""
            if relative_index == 0:
                time_str = "当前"
            else:
                time_str = format_relative_time_for_attention_log(
                    entry_timestamp, current_timestamp_ms
                )

            desc = await self.chat_session_manager.focus_manager._get_focus_description(entry)

            log_lines.append(f"- [{index_str}] {time_str} 专注于 {desc} (动机: {motivation})")

        return "\n".join(log_lines)

    async def _build_self_prompt_block(self) -> str:
        """构建 <self_prompt> 块，从工作区读取 self_prompt.md 文件.

        如果文件不存在，就返回一个友好的提示.
        """
        if not self.action_handler:
            logger.error(
                "在读取 self_prompt.md 时，ThoughtPromptBuilder 的 action_handler 未被初始化！"
            )
            return "<!-- 错误：ActionHandler未初始化，无法读取 self_prompt.md -->"

        try:
            # 从工作区安全路径读取文件
            workspace_root = self.action_handler._get_safe_workspace_root()
            prompt_file_path = workspace_root / "self_prompt.md"

            if prompt_file_path.exists() and prompt_file_path.is_file():
                content = prompt_file_path.read_text(encoding="utf-8")
                # 如果文件是空的，就返回一个友好的提示
                if not content.strip():
                    return "<!-- `self_prompt.md` 文件是空的，你可以在其中写入任何想让自己记住的设定或规则。 -->"  # noqa: E501
                return content
            else:
                # 如果文件尚不存在，也返回一个友好的提示
                return "<!-- `self_prompt.md` 文件尚不存在, 如希望编辑此处内容, 请在工作区根目录中创建并编辑该文件。 -->"  # noqa: E501
        except Exception as e:
            logger.error(f"读取 self_prompt.md 时发生意外错误: {e}", exc_info=True)
            # 出错了也要返回一个友好的提示
            return "<!-- 读取 self_prompt.md 时发生内部错误。 -->"

    async def _build_friend_request_block(self, platform_id: str) -> str:
        requests = await self.action_handler.entity_service.get_pending_friend_requests(platform_id)
        """构建好友请求块，展示当前平台的未处理好友请求."""
        if not requests:
            return (
                "<friend_request>\n你在该平台暂时没有来自他人的未处理好友请求。\n</friend_request>"
            )

        lines = ["<friend_request>", "你在该平台有以下来自他人的未处理好友请求："]
        for req in sorted(requests, key=lambda r: r.get("timestamp", 0), reverse=True):
            time_str = format_relative_time(req.get("timestamp", 0))
            lines.append(
                f"- 来自“{req.get('nickname', '未知用户')}”(ID: {req.get('user_id')})的请求, "
                f"flag: `{req.get('flag')}` ({time_str}): "
                f"“验证消息：{req.get('comment', '无验证消息')}”"
            )
        lines.append("</friend_request>")
        return "\n".join(lines)

    def build_deliberation_prompts(
        self, pipeline_params: dict, current_internal_state: dict
    ) -> tuple[str, str, dict[str, Any]]:
        """构建用于“慢思考”内部辩论的专属 Prompt 和 Schema."""
        # 1. 格式化 <opinions> XML 块
        opinions_block_lines = []
        opinions = pipeline_params.get("opinions", [])
        for i, p in enumerate(opinions):
            tag = p.get("tag", f"观点 {i + 1}")
            thought = p.get("initial_thought", "无具体想法。")
            opinions_block_lines.append(f'            <pipeline tag="{tag}">')
            opinions_block_lines.append(
                f"                <initial_thought>{thought}</initial_thought>"
            )
            opinions_block_lines.append("            </pipeline>")

        opinions_block = "\n".join(opinions_block_lines)

        # 2. 填充 User Prompt 模板
        user_prompt = DELIBERATION_USER_PROMPT.format(
            mood=current_internal_state.get("mood", "未知"),
            think=current_internal_state.get("think", "未知"),
            goal=current_internal_state.get("goal", "未知"),
            motivation=pipeline_params.get("motivation", "无明确动机"),
            opinions_block=opinions_block,
        )

        # 3. 系统 Prompt 是静态的，直接使用
        system_prompt = DELIBERATION_SYSTEM_PROMPT

        # 4. Response Schema 也是固定的
        response_schema = DELIBERATION_RESPONSE_SCHEMA

        # 打印调试信息
        logger.debug("=" * 30 + " 慢思考辩论 PROMPT " + "=" * 30)
        logger.debug(f"--- [SYSTEM PROMPT (慢思考)] ---\n{system_prompt}")
        logger.debug(f"--- [USER PROMPT (慢思考)] ---\n{user_prompt}")
        logger.debug(
            f"--- [JSON SCHEMA (慢思考)] ---\n{
                json.dumps(response_schema, indent=2, ensure_ascii=False)
            }"
        )
        logger.debug("=" * 31 + " END OF DEBUG " + "=" * 31)

        return system_prompt, user_prompt, response_schema

    def finalize_prompts(self, components: PromptComponents) -> tuple[str, str, dict[str, Any]]:
        """将 PromptComponents 转换为最终的系统和用户提示字符串."""
        system_prompt = prompt_templates.CORE_CYCLE_SYSTEM_PROMPT.format(
            **components.system_prompt_blocks
        )
        user_prompt = prompt_templates.CORE_CYCLE_USER_PROMPT.format(
            **components.user_prompt_blocks
        )
        return system_prompt, user_prompt, components.response_schema

    async def get_last_valid_text_message(self, conversation_id: str) -> str | None:
        """获取指定会话的最后有效文本消息."""
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
        """获取 Persona 块，包含机器人的名称、描述和档案信息."""
        return (
            f'你是"{config.persona.bot_name}"；'
            f"\n{config.persona.description}\n{config.persona.profile}"
        )

    async def _get_available_platforms_block(self) -> str:
        if not self.core_ws_server:
            return "平台通信服务尚未准备就绪。"
        return await self.core_ws_server.get_connected_platforms_info()

    async def _get_current_state_block(
        self, level: str, platform_id: str, conv_id: str | None
    ) -> str:
        """获取当前状态块."""
        if not self.chat_session_manager:
            raise PromptBuilderError("会话管理器尚未准备就绪，无法构建当前状态块。")

        if level == "core":
            return "你当前专注于：发呆/自我思考。"

        if level == "platform":
            return f"你当前专注于：{platform_id} 平台。"

        if level != "cellular" or not conv_id:
            return "未知状态"

        try:
            # 1. 将路径的会话部分 (e.g., 'group.123456') 分割成类型和ID
            conv_type, actual_id = conv_id.split(".", 1)

            # 2. 根据平台ID、类型和真实ID，重新组装出完整的实体UID
            #    这与 ChatSessionManager.sessions 字典的 key 格式完全匹配
            session_key = build_conversation_entity_uid(platform_id, conv_type, actual_id)

        except (ValueError, IndexError):
            # 如果 conv_id 格式不正确 (例如不包含'.')，则无法组装key，直接抛出错误
            raise PromptBuilderError(
                f"无法从会话部分 '{conv_id}' 解析出类型和ID，无法构建当前状态块。"
            ) from None

        # 3. 使用这个正确的 key 进行查找
        session = self.chat_session_manager.sessions.get(session_key)
        if not session:
            raise PromptBuilderError(
                f"在 'cellular' 层级，找不到会话实体UID为 '{session_key}' 的活跃会话档案，无法构建当前状态块。"  # noqa: E501
            )

        bot_profile = await session.get_bot_profile()

        if session.conversation_type == "group":
            return (
                f'你当前正在 qq 群"{session.conversation_name or "未知群聊"}"中参与 qq 群聊，'
                f'你在该群的群名片是"{bot_profile.get("card", config.persona.bot_name)}"'
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
            f"你当前正在 qq 上处理来自“{source_group_name}”群聊中"
            f"“{session.conversation_name or '对方'}”的临时会话私聊"
        )

    def _get_behavior_guidelines_block(self, level: str) -> str:
        if level in {"core", "platform"}:
            return CORE_BEHAVIOR_GUIDELINES
        elif level == "cellular":
            return FOCUS_BEHAVIOR_GUIDELINES
        return ""

    def _get_input_xml_block_description(self, level: str) -> str:
        if level == "core":
            return CORE_INPUT_XML_DESCRIPTION
        elif level == "platform":
            return PLATFORM_INPUT_XML_DESCRIPTION
        return FOCUS_INPUT_XML_DESCRIPTION if level == "cellular" else ""

    def _get_controls_descriptions(
        self, level: str, builder: BasePlatformBuilder, core_builder: CoreBuilder
    ) -> str:
        """获取当前层级的意识控制描述."""
        core_desc = core_builder.get_level_consciousness_controls_descriptions(level)
        plat_desc = (
            builder.get_level_consciousness_controls_descriptions(level)
            if level != "core" and builder
            else ""
        )
        return "\n".join(filter(None, [core_desc, plat_desc])) or "你当前没有可用的导航指令。"

    def _get_actions_descriptions(
        self, level: str, builder: BasePlatformBuilder | None, core_builder: CoreBuilder
    ) -> str:
        """获取当前层级的行动描述."""
        descs = []

        # 1. 核心动作描述永远存在
        if core_desc := core_builder.get_level_actions_descriptions(level):
            # 为核心动作描述添加命名空间前缀
            namespaced_core_desc = re.sub(r"(`)(\w+)", r"\1core.\2", core_desc)
            descs.append(f"- 核心能力:\n{namespaced_core_desc}")

        # 2. 如果在平台/细胞层，添加当前平台的动作描述
        if (
            level != "core"
            and builder
            and (plat_desc := builder.get_level_actions_descriptions(level))
        ):
            # 为平台动作描述添加命名空间前缀
            namespaced_plat_desc = re.sub(r"(`)(\w+)", rf"\1{builder.platform_id}.\2", plat_desc)
            descs.append(f"- 平台 '{builder.platform_id}' 专属能力:\n{namespaced_plat_desc}")

        # 3. 如果是在核心层，动态查找在线的工具平台并添加它们的描述
        if level == "core" and self.core_ws_server:
            # 从 ActionSender 获取当前已连接的适配器ID列表
            connected_adapter_ids = self.core_ws_server.action_sender.connected_adapters.keys()
            # 遍历所有在线的工具平台，获取它们的动作描述
            tool_descs = []
            for platform_id in connected_adapter_ids:
                if (
                    (p_builder := platform_builder_registry.get_builder(platform_id))
                    and p_builder.is_tool_platform
                ) and (tool_actions_desc := p_builder.get_level_actions_descriptions("platform")):
                    # 为工具平台动作描述添加命名空间前缀
                    namespaced_tool_desc = re.sub(
                        r"(`)(\w+)", rf"\1{platform_id}.\2", tool_actions_desc
                    )
                    tool_descs.append(
                        f"- 工具平台 '{platform_id}' 提供了以下能力:\n{namespaced_tool_desc}"
                    )
            if tool_descs:
                descs.append("\n".join(tool_descs))

        return "\n".join(filter(None, descs)).strip() or "你当前没有可用的外部行动。"

    async def _get_external_and_meta_info_blocks(
        self, level: str, platform_id: str, conv_id: str | None
    ) -> tuple[str, str, PromptComponents | None, list[Event] | None]:
        """获取外部信息和元信息块."""
        external_info, meta_info, history_components, processed_raw_events = "", "", None, None
        if not self.chat_session_manager:
            raise PromptBuilderError("会话管理器尚未准备就绪，无法构建外部信息块。")
        if level == "core":
            external_info = await self.unread_info_service.get_platform_summary()
        elif level == "platform":
            scroll_offset = (
                self.chat_session_manager.platform_view_states.get(platform_id, {}).get(
                    "scroll_offset", 0
                )
                if self.chat_session_manager
                else 0
            )
            external_info = await self.unread_info_service.get_conversation_list_summary(
                platform_id, scroll_offset=scroll_offset
            )
        elif level == "cellular" and conv_id:
            try:
                # 1. 将路径的会话部分 (e.g., 'group.123') 分割成类型和ID
                conv_type, actual_id = conv_id.split(".", 1)
                # 2. 重新组装出完整的实体UID
                session_key = build_conversation_entity_uid(platform_id, conv_type, actual_id)
            except (ValueError, IndexError):
                # 如果 conv_id 格式不正确，则无法组装key，直接抛出错误
                raise PromptBuilderError(
                    f"无法从会话部分 '{conv_id}' 解析出类型和ID，无法构建外部信息块。"
                ) from None

            # 3. 使用这个正确的 key 进行查找
            session = self.chat_session_manager.sessions.get(session_key)
            if not session:
                # 错误信息现在会显示我们尝试使用的正确key，方便调试
                raise PromptBuilderError(
                    f"在 'cellular' 层级，找不到会话实体UID为 '{session_key}' 的活跃会话档案，无法构建外部信息块。"  # noqa: E501
                )

            # 获取会话的历史记录和元信息
            bot_profile = await session.get_bot_profile()
            history_components, processed_raw_events = await format_chat_history_for_llm(
                event_storage=self.event_storage,
                conversation_id=session.conversation_info.conversation_id,
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
            event_types_block_str = (
                "## Event Types\n"
                "[MSG]: 普通消息，在消息后的（id:xxx）为消息的id\n"
                "[SYS]: 系统通知\n"
                '[MOTIVE]: 对应你的"motivation"，帮助你更好的了解自己的心路历程，代表你发出该条消息的“背后动机”或“原因”\n'  # noqa: E501
                "[FILE]: 文件分享\n"
                "[表情包: xxx] 或 [图片: xxx]: 这代表早些时候的图片，你已经不能直接看到了，只能通过文字来理解它的“印象”。\n"  # noqa: E501
                "[NOTICE]: 来自平台的通知\n"
            )
            external_info = (
                f"<Conversation_Info>\n{history_components.conversation_info_block}\n</Conversation_Info>\n\n"
                f"<user_logs>\n{history_components.user_list_block}\n</user_logs>\n\n"
                f"<event_types>\n{event_types_block_str}\n</event_types>\n\n"
                f"<chat_history>\n{history_components.chat_history_log_block}\n</chat_history>\n\n"
                f"<unread_summary>\n{unread_summary_str or '所有其他会话均无未读消息。'}\n</unread_summary>"  # noqa: E501
            )
            guidance_generator = BehavioralGuidanceGenerator(session)
            meta_info = guidance_generator.generate_guidance()
        return external_info, meta_info, history_components, processed_raw_events
