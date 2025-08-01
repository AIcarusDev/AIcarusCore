# src/core_logic/prompt_builder.py
from typing import TYPE_CHECKING, Any, Optional

from aicarus_protocols import Event
from src.action.action_handler import ActionHandler
from src.common.custom_logging.logging_config import get_logger
from src.common.focus_chat_history_builder.chat_history_formatter import format_chat_history_for_llm
from src.common.time_utils import format_relative_time, get_formatted_time_for_llm
from src.common.utils import parse_focus_path
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

    async def _build_action_response_desc(self, handover_result: dict | None) -> str:
        latest_thought = await self.thought_storage.get_latest_thought_document()
        if not latest_thought:
            return ""

        action_result_text = None
        action_payload = {}

        if handover_result:
            action_result_text = handover_result.get("result_text")
        elif thought_action_result := latest_thought.get("action_result"):
            action_result_text = thought_action_result
            action_payload = latest_thought.get("action_payload", {})

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
                    if action_name == "web_search":
                        if query := action_params.get("query"):
                            # 特例：为 web_search 构建包含关键词的丰富描述
                            action_desc = (
                                f"你刚才执行了网页搜索，搜索的关键词是“{query}”，得到了以下结果："
                            )
                    elif action_name:
                        # 通用情况：为所有其他动作构建清晰的描述
                        # 例如: "你刚才执行了动作 “qq.get_list”，得到了以下结果："
                        action_desc = (
                            f"你刚才执行了动作 “{platform_key}.{action_name}”，得到了以下结果："
                        )

        except Exception as e:
            logger.warning(f"解析上一个动作的 payload 以增强描述时出错: {e}。将使用通用描述。")
            # 如果解析过程中出现任何意外，程序不会崩溃，而是安全地使用上面的默认描述

        return f"<action_response>\n{action_desc}\n{action_result_text}\n</action_response>"

    async def _build_navigation_log_block(self) -> str:
        """构建导航日志块，展示最近的焦点移动轨迹."""
        if not self.chat_session_manager or len(self.chat_session_manager.focus_history) <= 1:
            return ""

        log_lines = ["<!-- 这是你最近的意识焦点移动轨迹 -->"]

        history = list(self.chat_session_manager.focus_history)  # 创建副本以安全迭代
        history_len = len(history)

        for i, entry in enumerate(reversed(history)):
            if not isinstance(entry, dict):
                continue

            time_index = history_len - 1 - i
            relative_index = time_index - (history_len - 1)
            motivation = entry.get("motivation", "未知动机")

            # [优化点] 调用异步方法获取丰富描述
            desc = await self.chat_session_manager._get_focus_description(entry)

            log_lines.append(f"[T{relative_index}] 聚焦于 {desc} (动机: {motivation})")

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
            # // 从 action_handler 那里借用我们已经写好的、绝对安全的工作区路径解析逻辑！
            # // 这样可以保证我们绝对不会读到工作区外面的文件！
            workspace_root = self.action_handler._get_safe_workspace_root()
            prompt_file_path = workspace_root / "self_prompt.md"

            if prompt_file_path.exists() and prompt_file_path.is_file():
                content = prompt_file_path.read_text(encoding="utf-8")
                # // 如果文件是空的，也给个提示，免得LLM以为是出错了
                if not content.strip():
                    return "<!-- `self_prompt.md` 文件是空的，你可以在其中写入任何想让自己记住的设定或规则。 -->"  # noqa: E501
                return content
            else:
                # // 文件不存在，就返回你设计的那个超棒的 fallback 提示！
                return "<!-- `self_prompt.md` 文件尚不存在, 如希望编辑此处内容, 请在工作区根目录中创建并编辑该文件。 -->"  # noqa: E501
        except Exception as e:
            logger.error(f"读取 self_prompt.md 时发生意外错误: {e}", exc_info=True)
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

    async def build_prompts_components(
        self,
        focus_path: str | None,
        session: Optional["ChatSession"] = None,
        handover_result: dict | None = None,
    ) -> tuple[PromptComponents, list[Event] | None]:
        """构建系统和用户提示组件.

        Args:
            focus_path (str | None): 当前的焦点路径，用于确定上下文.
            session (ChatSession | None): 可选的会话对象，用于获取会话相关信息.
            handover_result (dict | None): 可选的动作结果，用于构建动作响应描述.

        Returns:
            tuple[PromptComponents, list[Event] | None]: 包含系统和用户提示块的组件对象，
                以及处理过的原始事件列表.
        """
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
        if (current_level == "core" and "focus" in final_ctrl_schema_props) and (
            all_platform_ids := [
                pid for pid in platform_builder_registry.get_all_builders() if pid != "core"
            ]
        ):
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
        navigation_log_block = await self._build_navigation_log_block()
        friend_request_block = ""  # 初始化为空字符串，如果在顶层，那么就是空字符。
        if current_level in ["platform", "cellular"]:  # 仅在平台或细胞层级构建好友请求块
            friend_request_block = await self._build_friend_request_block(current_platform_id)

        system_prompt_blocks = {
            "aicarus_rule_block": AICARUS_RULE,
            "current_time": get_formatted_time_for_llm(),
            "persona_block": self._get_persona_block(),
            "available_platforms_block": await self._get_available_platforms_block(),
            "current_state_block": await self._get_current_state_block(
                current_level, current_platform_id, current_conv_id
            ),
            "navigation_log_block": navigation_log_block,
            "behavior_guidelines_block": self._get_behavior_guidelines_block(current_level),
            "internal_info_block": internal_info_block,
            "input_XML_block_description": self._get_input_xml_block_description(current_level),
            "available_consciousness_controls": self._get_controls_descriptions(
                current_level, builder, core_builder
            ),
            "available_actions": self._get_actions_descriptions(
                current_level, builder, core_builder
            ),
            "self_prompt_block": await self._build_self_prompt_block(),
        }

        user_prompt_blocks = {
            "action_response_block": action_response_block,
            "meta_info_block": meta_info_block,
            "external_info_block": external_info_block,
            "friend_request_block": friend_request_block,
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
        """将 PromptComponents 转换为最终的系统和用户提示字符串.

        Args:
            components (PromptComponents): 包含系统和用户提示块的组件对象.

        Returns:
            tuple: 包含系统提示字符串、用户提示字符串和响应模式的元组.
        """
        system_prompt = prompt_templates.CORE_CYCLE_SYSTEM_PROMPT.format(
            **components.system_prompt_blocks
        )
        user_prompt = prompt_templates.CORE_CYCLE_USER_PROMPT.format(
            **components.user_prompt_blocks
        )
        return system_prompt, user_prompt, components.response_schema

    async def get_last_valid_text_message(self, conversation_id: str) -> str | None:
        """获取指定会话的最后有效文本消息.

        Args:
            conversation_id (str): 会话的唯一标识符.

        Returns:
            str | None: 最后有效的文本消息，如果没有找到则返回 None.
        """
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
        """获取当前状态块.

        Args:
            level (str): 当前层级，可能是 'core', 'platform' 或 'cellular'.
            platform_id (str): 当前平台的唯一标识符.
            conv_id (str | None): 当前会话的唯一标识符，如果有的话.

        Returns:
            str: 描述当前状态的字符串.
        """
        if not self.chat_session_manager:
            raise PromptBuilderError("会话管理器尚未准备就绪，无法构建当前状态块。")

        if level == "core":
            return "你当前专注于：发呆/自我思考。"

        if level == "platform":
            return f"你当前专注于：{platform_id} 平台。"

        # 如果不是 'cellular' 层级或者没有 conv_id，直接返回未知状态
        if level != "cellular" or not conv_id:
            return "未知状态"

        # 主逻辑 (现在只处理 cellular 层级)
        session = self.chat_session_manager.sessions.get(conv_id)
        if not session:
            raise PromptBuilderError(f"找不到会话 {conv_id} 的档案，无法构建当前状态块。")

        bot_profile = await session.get_bot_profile()

        if session.conversation_type == "group":
            return (
                f'你当前正在 qq 群"{session.conversation_name or "未知群聊"}"中参与 qq 群聊，'
                f'你在该群的群名片是"{bot_profile.get("card", config.persona.bot_name)}"'
            )

        # 如果代码能走到这里，那它一定是 'private' 类型，无需再用 else
        is_temporary = session.conversation_info.extra.get("is_temporary", False)

        # 先处理 'not is_temporary' 这种更简单的私聊情况
        if not is_temporary:
            return f"你当前正在 qq 上与{session.conversation_name or '对方'}私聊"

        # 最后，处理最复杂的“临时会话”情况
        source_group_id = session.conversation_info.extra.get("source_group_id")
        source_group_name = "未知群聊"  # 默认值
        if source_group_id:

            # 1. 根据约定，构建群聊实体的 UID
            #    临时会话的来源必然是群聊，所以 conv_type 硬编码为 "group"
            source_group_entity_uid = f"{session.platform}_group_{source_group_id}"

            # 2. 通过 UID 获取群聊实体
            source_group_entity = await self.entity_service.get_entity_by_key(
                source_group_entity_uid
            )

            # 3. 从实体文档中安全地提取名称
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
        self, level: str, builder: BasePlatformBuilder, core_builder: CoreBuilder
    ) -> str:
        """获取当前层级的行动描述."""
        core_desc = core_builder.get_level_actions_descriptions(level)
        plat_desc = (
            builder.get_level_actions_descriptions(level) if level != "core" and builder else ""
        )
        final_descs = [core_desc, plat_desc]

        # [通用化改造]
        if level == "core":
            # 遍历所有已注册的平台
            for platform_id, p_builder in platform_builder_registry.get_all_builders().items():
                # 如果这个平台自称是“工具平台”
                if p_builder.is_tool_platform and (
                    tool_actions_desc := p_builder.get_level_actions_descriptions("platform")
                ):
                    tool_block = (
                        f"\n- 工具平台 '{platform_id}' 提供了以下特殊工具:\n{tool_actions_desc}"
                    )
                    final_descs.append(tool_block)

        return "\n".join(filter(None, final_descs)) or "你当前没有可用的外部行动。"

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
            external_info = await self.unread_info_service.get_conversation_list_summary(
                platform_id
            )
        elif level == "cellular" and conv_id:
            # 根据 platform_id 和 conv_id (格式如 'group.123') 重新组装正确的 entity_uid
            try:
                conv_type, actual_id = conv_id.split('.', 1)
                session_key = f"{platform_id}_{conv_type}_{actual_id}"
            except ValueError:
                # 如果 conv_id 格式不正确，记录错误并抛出异常
                raise PromptBuilderError(
                    f"无法从 conv_id '{conv_id}' 中解析出会话类型和ID。"
                ) from None

            session = self.chat_session_manager.sessions.get(session_key)
            if not session:
                # 错误信息现在会显示我们尝试使用的正确key，方便调试
                raise PromptBuilderError(
                    f"找不到会话实体UID '{session_key}' 的档案，无法构建外部信息块。"
                )
            # 获取会话的历史记录和元信息
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
                f"<unread_summary>\n{unread_summary_str or '所有其他会话均无未读消息。'}"
                f"\n</unread_summary>"
            )
            guidance_generator = BehavioralGuidanceGenerator(session)
            meta_info = guidance_generator.generate_guidance()
        return external_info, meta_info, history_components, processed_raw_events
