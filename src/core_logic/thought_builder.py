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
    def __init__(self, persona_cfg: PersonaSettings, prompt_template_str: str, initial_state: Dict[str, Any], logger_instance: Any):
        """
        初始化 Prompt 构建器。

        Args:
            persona_cfg: 机器人人设配置。
            prompt_template_str: 用户Prompt模板字符串。
            initial_state: 主思维的初始状态常量。
            logger_instance: 日志记录器实例。
        """
        self.persona_cfg = persona_cfg
        self.prompt_template_str = prompt_template_str
        self.initial_state = initial_state
        self.logger = logger_instance # 使用传入的 logger 实例

    def build_system_prompt(self, current_time_str: str) -> str:
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
            user_prompt_str: str = self.prompt_template_str.format(
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