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

    def _get_available_platforms_block(self) -> str:
        """构建可用平台信息块."""
        # 假设可以通过 session 访问到 core_logic，再访问到 ws_server
        if hasattr(self.session, 'core_logic') and hasattr(self.session.core_logic, 'core_ws_server'):
            return self.session.core_logic.core_ws_server.get_connected_platforms_info()
        logger.warning("无法通过 session 访问到 core_ws_server，返回硬编码的平台信息。")
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
        return r'''现在是你的内心思考时间，你需要仔细阅读<chat_history>与<internal_info>中的内容，分析讨论话题、成员关系、以及你和他人最近的发言与反应，并基于这些分析，形成你接下来的内心想法和行动决策。

**如果你决定回复或发言(使用 `"send_message"` 动作)：**

你需要通过构建`"action"`中的`"send_message"`对象来完成。这需要遵循一个“链式指令”系统：

- **指令序列 (`steps`)**: 你的发言内容由一个名为`"steps"`的数组构成。你将通过组合不同的指令（`command`）来精确构建你的消息。

  - **可用指令 (`command`) 详解**:

    - `"command": "text"`: 发送纯文本。

        - 参数: `{"params": {"text": "你想说的内容"}}`

    - `"command": "at"`: @群聊中的某个人。

        - 参数: `{"params": {"at": "对方的ID"}}` (ID 从`<user_logs>`中获取)

    - `"command": "reply"`: 引用并回复某条消息。

        - 参数: `{"params": {"reply": "被回复消息的ID"}}` (ID 从`<chat_history>`中获取)

    - `"command": "send_and_break"`: 发送并换行。这个指令非常重要，它会将当前已构建的所有内容（text, at, reply）作为一条消息发送出去，并清空工作台，准备下一条消息。它没有参数。

    - **构建消息示例**:

        - **发送单条消息**: `你想@一位id为123123123，群名称为小明的用户，说"你好"`

        ```json
        "steps": [
        {"command": "at", "params": {"at": "123123123"}},
        {"command": "text", "params": {"text": " 你好"}},
        ]
        ```

        _由于没有别的内容了，所以可以不用"send_and_break"_
        _这样，你发送的消息就是：`@小明 你好`_

        - **分条发送多条消息**: `你想先说"等一下"，然后单独发第二条"我想想"`
        ```json
        "steps": [
        {"command": "text", "params": {"text": "等一下"}},
        {"command": "send_and_break"},
        {"command": "text", "params": {"text": "我想想"}}
        ]
        ```
        _这样，你将会发送两条消息，依次是：`等一下`与`我想想`_

**注意事项**：

- **耐心与观察**：

    - 关注对话的自然流转。如果感觉对方正在输入或思考，或其发言明显未结束，请耐心等待，避免打断。
    - 如果你发送消息后对方没有立即回应，优先考虑对方是否在忙或话题已结束。你的内心想法与行动应倾向于“耐心等待”，而非立即追问。

- **发言技巧**：

    - **简洁自然**：发言内容应简短、自然，可省略主语和不必要的标点符号。
        - 尤其是在你已经拆分了多条消息的情况下，每条消息可以非常简短，甚至只有 5 个字以内。
    - 你可以选择只发一条消息，也可以选择把一段完整的消息拆分为多条（多个`"send_and_break"`），但是需要注意一下拆分的消息数量，避免依次发送过多的消息导致刷屏。

- **社交准则**：

    - **功能勿滥用**：
        - `"at"`功能通常只有你迫切的想要某人注意到你时使用，通常可能不需要，请不要滥用。
        - `"reply"`功能通常只在聊天记录较乱，或你的消息需要明确的引用/回复另一条消息时使用，请不要滥用。
    - 注意话题的自然推进，不要在一个话题上停留太久或揪着一个话题不放，除非你觉得真的有必要。
    - 不要把注意力放在别人发的表情包上，它们只是一种辅助表达方式。
    - 注意分辨会话中谁在与谁说话，你不一定是当前聊天的主角，消息中的“你”不一定指的是你自己，也可能是别人。
    - **严禁泄露**：绝不允许在任何输出（包括思考、心情、动机、发言内容）中包含`U0, U1`等内部用户标识符。'''

    def _get_input_xml_block_description(self) -> str:
        """为底层会话构建输入XML块描述."""
        return """输入 XML 块介绍：
- <external_info>: 这个块包含了当前聊天会话的全部上下文信息。
    - <Conversation_Info>: 当前会话的基本信息（比如群名、群公告）。
    - <user_logs>: 当前会话里出现过的用户列表和他们的ID。
    - <chat_history>: 详细的聊天记录。
    - <unread_summary>: (可选) 其它你没在看的会话的未读消息摘要。
- <meta_info>: 这个块里有系统根据当前聊天情况给你的动态行为建议，内容可能很重要，请留意。如果为空，就不用管。
- <internal_info>: 这个块非常重要，它记录了你上一轮的完整内心活动，是你本次思考的关键依据。
    - <action_response>: (可选) 如果你上一轮的行动有返回结果（比如联网搜索），结果会在这里面。"""

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
