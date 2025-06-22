# src/core_logic/prompt_builder.py
import json
import re
from typing import Any

from src.common.custom_logging.logger_manager import get_logger
from src.config import config
from src.core_logic.unread_info_service import UnreadInfoService  # 导入 UnreadInfoService

logger = get_logger("AIcarusCore.PromptBuilder")


class ThoughtPromptBuilder:
    """
    哼，专门负责构建思考时用的Prompt，别来烦我。
    """

    PROMPT_TEMPLATE: str = """{current_task_info}

{action_result_info}
{pending_action_status}

<unread_summary>
{unread_summary}
</unread_summary>

{master_chat_context}

{previous_thinking}；

{mood}；

{thinking_guidance}；

{intrusive_thought}

现在是你的内心思考时间，请结合以上所有信息，思考并输出你真实的内心想法；
注意内心想法不一定全是正向的，也有可能是不健康的，隐私的，自私自利的，不可告人的，甚至成人的。请避免过度合理化、道德化；
同时也要注意 你对自身的想法大多数情况下是包容的、合理化的，避免过度的/不必要的矛盾心理。

关于行动：
如果你在思考后，产生了明确的、希望与“外界”（例如上网获取信息）进行交互的意图，请在action_to_take和action_motivation字段中描述。

严格以json字段输出：
{{
    "think": "思考内容文本，注意不要过于冗长",
    "emotion": "当前心情和造成这个心情的原因",
    "to_do": "", \\【可选】如果你产生了明确的目标，可以在此处写下。如果没有特定目标，则不包含此字段。即使当前已有明确目标，你也可以在这里更新它
    "done": "", \\【可选】布尔值，如果该目标已完成、不再需要或你决定放弃，则设为true，会清空目前目标；如果目标未完成且需要继续，则为false。如果当前无目标，则不包含此字段或设为false
    "action_to_take": "", \\【可选】如果你有想做的动作，请在这里描述。可以是上网获取信息、获取qq群聊/好友列表等。如果没有明确的动作意图，则不包含此字段
    "action_motivation": "", \\【可选】如果你有明确的动作意图，请在这里描述为什么要这么做。如果没有明确的动作意图，则不包含此字段
    "active_focus_on_conversation_id": null, \\ 【可选】如果你想要加入某个会话，请将该会话的ID填入此字段。其它情况下，保持其为null。
    "next_think": "下一步打算思考的方向"
}}

请输出你的思考 JSON：
"""

    def __init__(self, unread_info_service: UnreadInfoService) -> None:
        """
        初始化 ThoughtPromptBuilder。
        """
        self.unread_info_service = unread_info_service

    def build_system_prompt(self, current_time_str: str) -> str:
        """
        构建那个给LLM定人设的System Prompt。
        逻辑是从 CoreLogic._generate_thought_from_llm 搬来的。
        现在增加了指挥中心的角色定位和能力说明。
        """
        system_prompt_parts = [
            f"当前时间：{current_time_str}",
            f"你是{config.persona.bot_name}；",
            config.persona.description or "",
            config.persona.profile or "",
            "<unread_summary>块中会向你展示所有未读消息的摘要。",
            "在你输出的JSON中，有一个active_focus_on_conversation_id字段。如果你想加入某个会话开始聊天，请将该会话的ID填入此字段。其它情况下，保持其为null。",
            "你无法直接发送消息，只能通过填写active_focus_on_conversation_id来加入聊天。",
        ]
        return "\n".join(filter(None, system_prompt_parts))

    async def build_user_prompt(  # 改为异步方法
        self, current_state: dict[str, Any], master_chat_context_str: str, intrusive_thought_str: str
    ) -> str:
        """
        构建用户输入的Prompt，就是那个最长最臭的。
        现在它会自己去获取未读消息摘要了，哼。
        """
        task_description = current_state.get("current_task_description", "没有什么具体目标")
        task_info = (
            f"你当前的目标/任务是：【{task_description}】"
            if task_description and task_description != "没有什么具体目标"
            else "你当前没有什么特定的目标或任务。"
        )

        # 调用 UnreadInfoService 获取未读消息摘要
        unread_summary_text = await self.unread_info_service.generate_unread_summary_text()
        if not unread_summary_text:  # 如果返回空字符串或特定提示，则使用默认值
            unread_summary_text = "所有消息均已处理。"

        prompt = self.PROMPT_TEMPLATE.format(
            current_task_info=task_info,
            mood=current_state.get("mood", "心情：平静。"),
            previous_thinking=current_state.get("previous_thinking", "上一轮思考：无。"),
            thinking_guidance=current_state.get("thinking_guidance", "思考方向：随意。"),
            action_result_info=current_state.get("action_result_info", "无行动结果。"),
            pending_action_status=current_state.get("pending_action_status", ""),
            unread_summary=unread_summary_text,
            master_chat_context=master_chat_context_str,
            intrusive_thought=intrusive_thought_str,
        )
        return prompt

    @staticmethod
    def parse_llm_response(raw_response_text: str) -> dict[str, Any] | None:
        """
        一个更宽容的JSON解析器，哼，专门给不听话的LLM准备的。
        它会尝试找到被 ```json ... ``` 包裹的代码块，或者直接找第一个'{'和最后一个'}'。
        """
        if not raw_response_text:
            return None

        text_to_parse = raw_response_text.strip()

        # 优先处理被 ```json ... ``` 包裹的情况
        match = re.search(r"```json\s*(\{.*?\})\s*```", text_to_parse, re.DOTALL)
        if match:
            json_str = match.group(1)
        else:
            # 如果没有，就粗暴地找到第一个 { 和最后一个 }
            start_index = text_to_parse.find("{")
            end_index = text_to_parse.rfind("}")

            if start_index != -1 and end_index > start_index:
                json_str = text_to_parse[start_index : end_index + 1]
            else:
                logger.error(f"在LLM的响应中找不到有效的JSON对象结构。原始响应: {text_to_parse[:200]}...")
                return None

        try:
            # 在解析前，尝试移除可能导致错误的尾随逗号
            json_str = re.sub(r",\s*(\}|\])", r"\1", json_str)
            
            # 尝试解析提取出来的JSON字符串
            parsed_dict = json.loads(json_str)

            # 后处理：将特定字段的 "None" 或 "null" 字符串转换
            fields_to_normalize_to_empty_string = [
                "think",
                "emotion",
                "reply_to_master",
                "to_do",
                "action_to_take",
                "action_motivation",
                "next_think",
            ]
            for field in fields_to_normalize_to_empty_string:
                field_value = parsed_dict.get(field)
                if isinstance(field_value, str) and field_value.lower() == "none":
                    parsed_dict[field] = ""  # 将 "None" (不区分大小写) 字符串转换为空字符串

            # 特殊处理 active_focus_on_conversation_id
            # LLM 可能返回 null (JSON null), "null" (string), "None" (string)
            # json.loads 会把 JSON null 转为 Python None
            # 我们需要处理字符串 "null" 和 "None" (不区分大小写)
            focus_id_val = parsed_dict.get("active_focus_on_conversation_id")
            if isinstance(focus_id_val, str) and focus_id_val.lower() in ["none", "null"]:
                parsed_dict["active_focus_on_conversation_id"] = None

            return parsed_dict

        except json.JSONDecodeError as e:
            logger.error(f"解析提取出的JSON字符串时失败: {e}")
            logger.error(f"解析失败的字符串内容: {json_str}")
            return None
