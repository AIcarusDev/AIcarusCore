# src/core_logic/prompt_builder.py
import re
from typing import Any, Dict, Optional
import json

from src.config import config
from src.common.custom_logging.logger_manager import get_logger

logger = get_logger("AIcarusCore.PromptBuilder")

class ThoughtPromptBuilder:
    """
    哼，专门负责构建思考时用的Prompt，别来烦我。
    """
    PROMPT_TEMPLATE: str = """{current_task_info}

{action_result_info}
{pending_action_status}

{recent_contextual_information}

{master_chat_context} 

{previous_thinking}；

{mood}；

{thinking_guidance}；

{intrusive_thought}

现在是你的内心思考时间，请结合以上所有信息，思考并输出你真实的内心想法；
注意内心想法不一定全是正向的，也有可能是不健康的，隐私的，自私自利的，不可告人的，甚至成人的。请避免过度合理化、道德化；
同时也要注意 你对自身的想法大多数情况下是包容的、合理化的，避免过度的/不必要的矛盾心理。

关于行动：
如果你在思考后，产生了明确的、希望与“外界”（例如上网获取信息、获取qq群聊/好友列表）进行交互的意图，请在action_to_take和action_motivation字段中描述。

严格以json字段输出：
{{
    "think": "思考内容文本，注意不要过于冗长",
    "emotion": "当前心情和造成这个心情的原因",
    "reply_to_master": "【可选】如果你想对电脑主人说些什么，就在这里填写你想说的内容。如果不想说，就留null，注意话不要太多了",
    "to_do": "【可选】如果你产生了明确的目标，可以在此处写下。如果没有特定目标，则留null。即使当前已有明确目标，你也可以在这里更新它",
    "done": "【可选】布尔值，如果该目标已完成、不再需要或你决定放弃，则设为true，会清空目前目标；如果目标未完成且需要继续，则为false。如果当前无目标，也为false",
    "action_to_take": "【可选】描述你当前想做的、需要与外界交互的具体动作。如果无，则为null",
    "action_motivation": "【可选】如果你有想做的动作，请说明其动机。如果action_to_take为null，此字段也应为null",
    "next_think": "下一步打算思考的方向"
}}

请输出你的思考 JSON：
"""

    def __init__(self):
        """
        初始化 ThoughtPromptBuilder。
        直接使用全局配置，不创建额外的实例变量。
        """
        pass

    def build_system_prompt(self, current_time_str: str) -> str:
        """
        构建那个给LLM定人设的System Prompt。
        逻辑是从 CoreLogic._generate_thought_from_llm 搬来的。
        """
        system_prompt_parts = [
            f"当前时间：{current_time_str}",
            f"你是{config.persona.bot_name}；",
            config.persona.description or "",
            config.persona.profile or "",
        ]
        return "\\n".join(filter(None, system_prompt_parts))

    def build_user_prompt(
        self,
        current_state: Dict[str, Any],
        master_chat_context_str: str,
        intrusive_thought_str: str
    ) -> str:
        """
        构建用户输入的Prompt，就是那个最长最臭的。
        """
        task_description = current_state.get("current_task_description", "没有什么具体目标")
        task_info = (
            f"你当前的目标/任务是：【{task_description}】"
            if task_description and task_description != "没有什么具体目标"
            else "你当前没有什么特定的目标或任务。"
        )

        prompt = self.PROMPT_TEMPLATE.format(
            current_task_info=task_info,
            mood=current_state.get("mood", "心情：平静。"),
            previous_thinking=current_state.get("previous_thinking", "上一轮思考：无。"),
            thinking_guidance=current_state.get("thinking_guidance", "思考方向：随意。"),
            action_result_info=current_state.get("action_result_info", "无行动结果。"),
            pending_action_status=current_state.get("pending_action_status", ""),
            recent_contextual_information=current_state.get("recent_contextual_information", "无最近信息。"),
            master_chat_context=master_chat_context_str,
            intrusive_thought=intrusive_thought_str,
        )
        return prompt

    @staticmethod
    def parse_llm_response(raw_response_text: str) -> Optional[Dict[str, Any]]:
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
            start_index = text_to_parse.find('{')
            end_index = text_to_parse.rfind('}')
            
            if start_index != -1 and end_index > start_index:
                json_str = text_to_parse[start_index : end_index + 1]
            else:
                logger.error(f"在LLM的响应中找不到有效的JSON对象结构。原始响应: {text_to_parse[:200]}...")
                return None
        
        try:
            # 尝试解析提取出来的JSON字符串
            return json.loads(json_str)
        except json.JSONDecodeError as e:
            logger.error(f"解析提取出的JSON字符串时失败: {e}")
            logger.error(f"解析失败的字符串内容: {json_str}")
            return None