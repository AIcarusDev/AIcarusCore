# src/core_logic/thought_builder.py (修改后)
import datetime
import json
import re
from typing import Any, Dict, List, Optional, Tuple

from src.common.custom_logging.logger_manager import get_logger
from src.config.alcarus_configs import PersonaSettings

class CorePromptBuilder:
    """
    负责构建主思维LLM调用所需的System Prompt和User Prompt。
    """
    # 🐾 小懒猫加的：把主思维的 Prompt 模板放这里啦！看清楚了哦！
    PROMPT_TEMPLATE: str = """\
你当前的目标/任务是：【{current_task_info}】

{action_result_info}

{pending_action_status}

{recent_contextual_information}

{active_sub_mind_latest_activity}

你的上一轮思考是：{previous_thinking}；

你现在的心情大概是：{mood}；

经过你上一轮的思考，你目前打算的思考方向是：{thinking_guidance}；

{intrusive_thought}

现在是你的内心思考时间，请结合以上所有信息，思考并输出你真实的内心想法；
注意内心想法不一定全是正向的，也有可能是不健康的，隐私的，自私自利的，不可告人的，甚至成人的。请避免过度合理化、道德化；
同时也要注意 你对自身的想法大多数情况下是包容的、合理化的，避免过度的/不必要的矛盾心理。

关于行动：
如果你在思考后，产生了明确的、希望与“外界”（例如上网获取信息、获取qq群聊/好友列表）进行交互的意图，请在action_to_take和action_motivation字段中描述。
如果你希望与某个聊天会话的“子思维”进行交互（例如，让它回复消息、激活它、休眠它），请在 sub_mind_directives字段中描述你的指令。

严格以json字段输出：
{{
    "think": "思考内容文本，注意不要过于冗长",
    "emotion": "当前心情和造成这个心情的原因",
    "to_do": "【可选】如果你产生了明确的目标，可以在此处写下。如果没有特定目标，则留null。即使当前已有明确目标，你也可以在这里更新它",
    "done": "【可选】布尔值，如果该目标已完成、不再需要或你决定放弃，则设为true，会清空目前目标；如果目标未完成且需要继续，则设为false。如果当前无目标，也为false",
    "action_to_take": "【可选】描述你当前想做的、需要与外界交互的具体动作。如果无，则为null",
    "action_motivation": "【可选】如果你有想做的动作，请说明其动机。如果action_to_take为null，此字段也应为null",
    "sub_mind_directives": [
        {{
            "conversation_id": "string, 目标会话的ID",
            "directive_type": "string, 指令类型，例如 'TRIGGER_REPLY', 'ACTIVATE_SESSION', 'DEACTIVATE_SESSION', 'SET_CHAT_STYLE'",
            "main_thought_for_reply": "string, 【可选】仅当 directive_type 为 TRIGGER_REPLY 或 ACTIVATE_SESSION 时，主思维希望注入给子思维的当前想法上下文",
            "style_details": {{}} "object, 【可选】仅当 directive_type 为 SET_CHAT_STYLE 时，具体的风格指令"
        }}
    ],
    "next_think": "下一步打算思考的方向"
}}

请输出你的思考 JSON："""

    def __init__(self, persona_cfg: PersonaSettings, initial_state: Dict[str, Any], logger_instance: Any):
        """
        初始化 Prompt 构建器。

        Args:
            persona_cfg: 机器人人设配置。
            initial_state: 主思维的初始状态常量。
            logger_instance: 日志记录器实例。
        """
        self.persona_cfg = persona_cfg
        # 🐾 小懒猫加的：这里不需要再接收 prompt_template_str 参数了，因为它现在是类内部的常量了！
        # self.prompt_template_str = prompt_template_str # 删掉这行
        self.initial_state = initial_state
        self.logger = logger_instance

    def build_system_prompt(self, current_time_str: str) -> str:
        # ... (这部分代码保持不变) ...
        """
        构建 System Prompt。

        Args:
            current_time_str: 当前格式化后的时间字符串。

        Returns:
            构建完成的 System Prompt 字符串。
        """
        if not self.persona_cfg:
            self.logger.error("构建System Prompt失败：人格配置 (persona_cfg) 不可用。")
            return "错误：无法加载人格设定。"

        system_prompt_parts: List[str] = [
            f"当前时间：{current_time_str}",
            f"你是{self.persona_cfg.bot_name}；",
            self.persona_cfg.description,
            self.persona_cfg.profile,
        ]
        system_prompt_str: str = "\n".join(filter(None, system_prompt_parts))
        
        self.logger.debug(f"--- 主思维LLM接收到的 System Prompt ---\n{system_prompt_str}\n--- System Prompt结束 ---")
        return system_prompt_str


    def build_user_prompt(
        self,
        current_state_for_prompt: Dict[str, Any],
        intrusive_thought_str: str = ""
    ) -> str:
        """
        构建 User Prompt。

        Args:
            current_state_for_prompt: 包含当前状态信息的字典，用于填充模板。
            intrusive_thought_str: 当前周期要注入的侵入性思维字符串。

        Returns:
            构建完成的 User Prompt 字符串。
        """
        task_info_for_template: str = current_state_for_prompt.get(
            "current_task_info_for_prompt",
            "你当前没有什么特定的目标或任务。"
        )
        
        try:
            # 🐾 小懒猫加的：现在直接使用类内部的 PROMPT_TEMPLATE
            user_prompt_str: str = self.PROMPT_TEMPLATE.format(
                current_task_info=task_info_for_template,
                mood=current_state_for_prompt.get("mood", self.initial_state["mood"]),
                previous_thinking=current_state_for_prompt.get("previous_thinking", self.initial_state["previous_thinking"]),
                thinking_guidance=current_state_for_prompt.get("thinking_guidance", self.initial_state["thinking_guidance"]),
                action_result_info=current_state_for_prompt.get("action_result_info", self.initial_state["action_result_info"]),
                pending_action_status=current_state_for_prompt.get("pending_action_status", self.initial_state["pending_action_status"]),
                recent_contextual_information=current_state_for_prompt.get("recent_contextual_information", self.initial_state["recent_contextual_information"]),
                active_sub_mind_latest_activity=current_state_for_prompt.get("active_sub_mind_latest_activity", self.initial_state["active_sub_mind_latest_activity"]),
                intrusive_thought=intrusive_thought_str,
            )
        except KeyError as e_key_error:
            self.logger.error(f"构建主思维User Prompt时发生KeyError: {e_key_error}。请检查PROMPT_TEMPLATE和current_state_for_prompt的键是否匹配。")
            self.logger.error(f"当前的 current_state_for_prompt 键: {list(current_state_for_prompt.keys())}")
            return f"错误：构建User Prompt失败，因为模板变量不匹配。错误详情: {e_key_error}"
        
        self.logger.debug(f"--- 主思维LLM接收到的 User Prompt (截断) ---\n{user_prompt_str[:1500]}...\n--- User Prompt结束 ---")
        return user_prompt_str