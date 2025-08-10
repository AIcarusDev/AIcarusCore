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
from src.domain.models import Stimulus
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
        last_shown_core_summary: str | None = None,
    ) -> tuple[PromptComponents, list[Event] | None, str | None]:
        """编排构建系统和用户提示组件的过程."""
        current_level, current_platform_id, current_conv_id = parse_focus_path(focus_path)

        # 在构建任何组件之前，先判断是否可以执行 back/jump 操作
        can_go_back = False
        if self.chat_session_manager and self.chat_session_manager.focus_manager:
            # 历史记录大于1条，意味着除了 T-0 (当前) 之外，至少还有 T-1
            can_go_back = len(self.chat_session_manager.focus_manager.focus_history) > 1

        # 1. 构建外部信息和元信息
        (
            external_info_block,
            meta_info_block,
            history_components,
            processed_raw_events,
            summary_to_show,
        ) = await self._get_external_and_meta_info_blocks(
            current_level, current_platform_id, current_conv_id, session, last_shown_core_summary
        )

        # 2. 构建响应 Schema
        response_schema = self._build_response_schema(
            current_level,
            current_platform_id,
            current_conv_id,
            can_go_back=can_go_back,
        )

        # 3. 构建 System Prompt 的各个部分
        # 传入 can_go_back 标志
        system_prompt_blocks = await self._build_system_prompt_blocks(
            level=current_level,
            platform_id=current_platform_id,
            conv_id=current_conv_id,
            session=session,
            user_map=history_components.user_map if history_components else None,
            can_go_back=can_go_back,
        )

        # 4. 构建 User Prompt 的各个部分
        user_prompt_blocks = await self._build_user_prompt_blocks(
            handover_result=handover_result,
            meta_info_block=meta_info_block,
            external_info_block=external_info_block,
            platform_id=current_platform_id,
            level=current_level,
            session=session,
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

        return prompt_components_obj, processed_raw_events, summary_to_show

    def _build_action_schema_properties(
        self, level: str, builder: BasePlatformBuilder | None
    ) -> dict:
        """[Helper] 构建动作部分的 JSON Schema properties."""
        core_builder = platform_builder_registry.get_builder("core")
        core_act_schema, _ = core_builder.get_level_actions_definitions(level)

        # 1. 总是包含核心动作
        action_props = {"core": core_act_schema}

        # 2. 如果在平台/细胞层，添加平台专属动作
        if builder and level != "core":
            plat_act_schema, _ = builder.get_level_actions_definitions(level)
            action_props[builder.platform_id] = plat_act_schema

        # 3. 如果在核心层，动态添加所有在线工具平台的能力
        if level == "core" and self.core_ws_server:
            connected_adapter_ids = self.core_ws_server.action_sender.connected_adapters.keys()
            for pid in connected_adapter_ids:
                p_builder = platform_builder_registry.get_builder(pid)
                if p_builder and p_builder.is_tool_platform:
                    tool_schema, _ = p_builder.get_level_actions_definitions("platform")
                    action_props[pid] = tool_schema

        return action_props

    def _filter_navigation_controls(self, properties: dict[str, Any], can_go_back: bool) -> None:
        """[Helper] 根据历史记录情况，过滤掉 'back' 和 'jump_to_history' 指令."""
        if not can_go_back:
            properties.pop("back", None)
            properties.pop("jump_to_history", None)

    # 接收 can_go_back 标志
    def _build_response_schema(
        self, level: str, platform_id: str, conv_id: str | None, can_go_back: bool
    ) -> dict[str, Any]:
        """构建 LLM 响应的 JSON Schema (重构后)."""
        builder = platform_builder_registry.get_builder(platform_id)
        core_builder = platform_builder_registry.get_builder("core")

        # 先获取完整的 controls schema
        consciousness_controls_schema, _ = (
            core_builder.get_level_consciousness_controls_definitions(level)
        )
        if builder:
            plat_controls_schema, _ = builder.get_level_consciousness_controls_definitions(level)
            consciousness_controls_schema["properties"].update(plat_controls_schema["properties"])

        # 根据上下文动态过滤指令
        self._filter_navigation_controls(consciousness_controls_schema["properties"], can_go_back)

        if level == "cellular":
            consciousness_controls_schema["properties"].pop("focus", None)

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
                "action": {
                    "type": "object",
                    "properties": self._build_action_schema_properties(level, builder),
                },
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
        can_go_back: bool,
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
                level, builder, core_builder, can_go_back=can_go_back
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
        session: Optional["ChatSession"] = None,
    ) -> dict[str, Any]:
        """(提取出的新方法) 构建 User Prompt 的所有部分."""
        feedback_text = ""
        if session and session.last_command_feedback:
            feedback_text = session.last_command_feedback
            session.last_command_feedback = None  # 清除会话层反馈
        elif self.chat_session_manager and self.chat_session_manager.global_command_feedback:
            feedback_text = self.chat_session_manager.global_command_feedback
            self.chat_session_manager.global_command_feedback = None  # 清除全局反馈

        command_feedback_block = ""
        if feedback_text:
            command_feedback_block = f"<command_feedback>\n{feedback_text}\n</command_feedback>"

        action_response_block = await self._build_action_response_desc(handover_result)

        friend_request_block = ""
        if level in {"platform", "cellular"}:
            friend_request_block = await self._build_friend_request_block(platform_id)

        return {
            "action_response_block": action_response_block,
            "command_feedback_block": command_feedback_block,
            "meta_info_block": meta_info_block,
            "external_info_block": external_info_block,
            "friend_request_block": friend_request_block,
        }

    async def _get_latest_action_context(
        self, handover_result: dict | None
    ) -> tuple[dict | None, str | None, dict | None]:
        """[Helper] 获取最新的思考文档、动作结果文本和动作载荷."""
        latest_thought = await self.thought_storage.get_latest_thought_document()
        if not latest_thought:
            return None, None, None

        action_result_text = None
        if handover_result:
            action_result_text = handover_result.get("result_text")
        elif thought_action_result := latest_thought.get("action_result"):
            action_result_text = thought_action_result

        action_payload = latest_thought.get("action_payload") or {}
        return latest_thought, action_result_text, action_payload

    def _parse_action_details_from_payload(
        self, action_payload: dict
    ) -> tuple[str | None, str | None, dict | None]:
        """[Helper] 从动作载荷中解析出平台、动作名和参数."""
        try:
            action_part = action_payload.get("action", {})
            if not action_part or not isinstance(action_part, dict):
                return None, None, None

            # 规范化动作载荷
            known_platform_keys = platform_builder_registry.get_all_builders().keys()
            if (
                all(key not in known_platform_keys for key in action_part)
                and "core" not in action_part
            ):
                action_part = {"core": action_part}

            platform_key = next(iter(action_part), None)
            platform_actions = action_part.get(platform_key, {}) if platform_key else {}
            action_name = next(iter(platform_actions), None)
            action_params = platform_actions.get(action_name, {}) if action_name else {}

            return platform_key, action_name, action_params
        except (StopIteration, AttributeError) as e:
            logger.warning(f"解析动作载荷时出错: {e}。载荷: {action_payload}")
            return None, None, None

    def _create_action_description_prefix(
        self, platform_key: str | None, action_name: str | None, action_params: dict | None
    ) -> str:
        """[Helper] 根据动作细节创建描述性前缀文本."""
        if not action_name:
            return "你刚才的行动成功了，返回了以下信息："

        if action_name == "web_search" and action_params and (query := action_params.get("query")):
            return f"你刚才执行了网页搜索，搜索的关键词是“{query}”，得到了以下结果："

        return f"你刚才执行了动作 “{platform_key}.{action_name}”，得到了以下结果："

    async def _post_process_get_list_result(self, result_text: str, platform_key: str) -> str:
        """对 get_list 动作的结果进行后处理，修复实体ID格式、过滤自身和无用字段."""
        try:
            self_entity = await self.entity_service.get_self_entity_by_platform(platform_key)
            self_platform_id = (
                str(self_entity.get("details", {}).get("platform_id")) if self_entity else None
            )

            result_list = json.loads(result_text)
            if not isinstance(result_list, list) or not result_list:
                logger.warning("get_list 结果不是一个列表或为空，无法后处理。")
                return result_text

            # 从第一个条目推断列表类型（'friend' 或 'group'）
            first_item = result_list[0]
            list_type = (
                "friend"
                if "user_id" in first_item
                else "group"
                if "group_id" in first_item
                else "unknown"
            )

            # 动态地从平台构建器获取要保留的键
            builder = platform_builder_registry.get_builder(platform_key)
            if builder:
                keys_to_keep = builder.get_list_keys_to_keep(list_type)
            else:
                logger.warning(
                    f"无法为平台 '{platform_key}' 找到构建器。将使用默认键进行 get_list 后处理。"
                )
                # 回退到一个通用的默认键集合
                keys_to_keep = {
                    "user_id",
                    "group_id",
                    "nickname",
                    "remark",
                    "group_name",
                }

            cleaned_list = []
            for item in result_list:
                if not isinstance(item, dict):
                    logger.debug(f"跳过 get_list 结果中的非字典项: {item}")
                    continue

                user_id = item.get("user_id")
                group_id = item.get("group_id")

                item_type = None
                item_id = None
                if user_id:
                    item_type = "private"
                    item_id = str(user_id)
                elif group_id:
                    item_type = "group"
                    item_id = str(group_id)

                if not item_id:
                    logger.warning(
                        f"跳过 get_list 结果中的一个项目，"
                        f"因为它缺少 user_id 或 group_id: {str(item)[:100]}"
                    )
                    continue

                if item_type == "private" and self_platform_id and item_id == self_platform_id:
                    logger.debug(f"在 get_list 结果中过滤掉 AI 自身 (ID: {item_id})。")
                    continue

                # 使用从构建器获取的 keys_to_keep 集合进行过滤
                cleaned_item = {k: v for k, v in item.items() if k in keys_to_keep}

                entity_uid = build_conversation_entity_uid(platform_key, item_type, item_id)
                if item_type == "private":
                    if "user_id" in cleaned_item:
                        cleaned_item["user_id"] = entity_uid
                else:  # item_type == "group"
                    if "group_id" in cleaned_item:
                        cleaned_item["group_id"] = entity_uid

                cleaned_list.append(cleaned_item)

            return json.dumps(cleaned_list, indent=4, ensure_ascii=False)
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning(
                f"后处理 get_list 动作结果时失败: {e}. 原始文本: '{result_text[:200]}...'"
            )
            return result_text
        except Exception as e:
            logger.error(f"在 _post_process_get_list_result 中发生意外错误: {e}", exc_info=True)
            return result_text

    async def _build_action_response_desc(self, handover_result: dict | None) -> str:
        """[Orchestrator] 构建上一个动作的结果描述块 (重构后)."""
        # 1. 获取上下文
        _, action_result_text, action_payload = await self._get_latest_action_context(
            handover_result
        )

        # 2. 守卫子句：如果没有有效结果，则提前返回
        if not action_result_text or "决定不行动" in action_result_text:
            return ""

        # 3. 解析动作细节
        platform_key, action_name, action_params = self._parse_action_details_from_payload(
            action_payload
        )

        # 4. 生成描述前缀
        action_desc = self._create_action_description_prefix(
            platform_key, action_name, action_params
        )

        # 5. 对特定动作结果进行后处理
        if action_name == "get_list" and platform_key:
            try:
                parsed_result = json.loads(action_result_text)
                if isinstance(parsed_result, list):
                    logger.debug("get_list 结果被识别为原始JSON列表，将进行后处理。")
                    action_result_text = await self._post_process_get_list_result(
                        action_result_text, platform_key
                    )
            except (json.JSONDecodeError, TypeError):
                logger.debug("get_list 结果不是原始JSON列表，将按原样使用。")

        # 6. 格式化并返回最终的XML块
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
            opinions_block_lines.extend(
                [
                    f'            <pipeline tag="{tag}">',
                    f"                <initial_thought>{thought}</initial_thought>",
                    "            </pipeline>",
                ]
            )

        opinions_block = "\n".join(opinions_block_lines)

        # 2. 填充 User Prompt 模板
        user_prompt = DELIBERATION_USER_PROMPT.format(
            mood=current_internal_state.get("mood", "未知"),
            think=current_internal_state.get("think", "未知"),
            intent=current_internal_state.get("intent", "未知"),
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
            f"--- [JSON SCHEMA (慢思考)] ---\n{json.dumps(response_schema, ensure_ascii=False)}"
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
            return "你当前似乎没有干什么。"

        if level == "platform":
            return f"你当前专注于：{platform_id} 平台。"

        if level != "cellular" or not conv_id:
            return "未知状态"

        try:
            if "." not in conv_id:
                raise PromptBuilderError(f"无效的会话ID格式 '{conv_id}'。它必须是 'type.id' 格式。")

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
        self,
        level: str,
        builder: BasePlatformBuilder | None,
        core_builder: CoreBuilder,
        can_go_back: bool,
    ) -> str:
        """获取当前层级的意识控制描述."""
        # 先获取完整的 schema，再根据标志进行过滤
        schema, _ = core_builder.get_level_consciousness_controls_definitions(level)
        if builder:
            plat_schema, _ = builder.get_level_consciousness_controls_definitions(level)
            schema["properties"].update(plat_schema["properties"])

        available_controls = schema.get("properties", {})

        self._filter_navigation_controls(available_controls, can_go_back)

        if level == "cellular":
            available_controls.pop("focus", None)

        descs = []
        for name, definition in available_controls.items():
            params_list = definition.get("required", [])
            params_str = ", ".join(params_list)
            description = definition.get("description", "（无可用描述）")
            desc_line = f"      - `{name}({params_str})`: {description}"
            descs.append(desc_line)

        return "\n".join(sorted(descs)) or "你当前没有可用的导航指令。"

    def _get_actions_descriptions(
        self, level: str, builder: BasePlatformBuilder | None, core_builder: CoreBuilder
    ) -> str:
        """获取当前层级的行动描述."""
        descs = []

        # 1. 核心动作描述永远存在
        if core_desc := core_builder.get_level_actions_descriptions(level):
            # 为核心动作描述添加命名空间前缀
            namespaced_core_desc = re.sub(r"(`)(\w+)", r"\1core.\2", core_desc)
            descs.append(f"- 基础能力:\n{namespaced_core_desc}")

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
        self,
        level: str,
        platform_id: str,
        conv_id: str | None,
        session: Optional["ChatSession"] = None,
        last_shown_core_summary: str | None = None,
    ) -> tuple[str, str, PromptComponents | None, list[Stimulus] | None, str | None]:
        """获取外部信息和元信息块。现在返回 Stimulus 列表."""
        external_info, meta_info, history_components, processed_stimuli = "", "", None, None
        summary_to_show_this_turn: str | None = None

        if not self.chat_session_manager:
            raise PromptBuilderError("会话管理器尚未准备就绪，无法构建外部信息块。")

        if level == "core":
            # 1. 获取当前最新的未读摘要
            current_unread_summary = await self.unread_info_service.get_platform_summary()

            # 2. 核心判断逻辑
            # 如果当前摘要是新的(和上次展示的不一样)且不为空，就展示它。
            if current_unread_summary and current_unread_summary != last_shown_core_summary:
                external_info = current_unread_summary
                summary_to_show_this_turn = current_unread_summary  # 记录我们这次展示了什么
                logger.info("检测到新的未读消息，将在顶层Prompt中展示。")
            else:
                # 如果是旧闻或者根本没消息，就不展示
                external_info = "所有平台均无新的未读消息。"
                # 如果当前没消息了，也要重置“记忆”，这样下次来新消息时才能正确显示
                if not current_unread_summary:
                    summary_to_show_this_turn = None
                else:
                    summary_to_show_this_turn = current_unread_summary
                logger.debug("顶层未读消息为旧闻或为空，本次不予展示。")

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
                if "." not in conv_id:
                    raise PromptBuilderError(
                        f"无效的会话ID格式 '{conv_id}'。它必须是 'type.id' 格式。"
                    )

                # 1. 将路径的会话部分 (e.g., 'group.123') 分割成类型和ID
                conv_type, actual_id = conv_id.split(".", 1)
                # 2. 重新组装出完整的实体UID
                session_key = build_conversation_entity_uid(platform_id, conv_type, actual_id)
            except (ValueError, IndexError):
                # 如果 conv_id 格式不正确，则无法组装key，直接抛出错误
                raise PromptBuilderError(
                    f"无法从会话部分 '{conv_id}' 解析出类型和ID，无法构建外部信息块。"
                ) from None

            # 3. 使用正确的 key 进行查找
            if not session:
                session = self.chat_session_manager.sessions.get(session_key)

            if not session:
                # 错误信息现在会显示我们尝试使用的正确key，方便调试
                raise PromptBuilderError(
                    f"在 'cellular' 层级，找不到会话实体UID为 '{session_key}' 的活跃会话档案，无法构建外部信息块。"  # noqa: E501
                )

            # 获取会话的历史记录和元信息
            bot_profile = await session.get_bot_profile()
            # 调用新的格式化函数，它现在返回 Stimulus 列表
            history_components, processed_stimuli = await format_chat_history_for_llm(
                event_storage=self.event_storage,
                conversation_id=session.conversation_info.conversation_id,
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

        last_shown_core_summary = summary_to_show_this_turn
        return (
            external_info,
            meta_info,
            history_components,
            processed_stimuli,  # <-- 返回的是 processed_stimuli
            summary_to_show_this_turn,
        )
