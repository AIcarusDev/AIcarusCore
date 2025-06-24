# src/action/components/action_decision_maker.py
import json
from dataclasses import dataclass

from src.common.custom_logging.logger_manager import get_logger
from src.llmrequest.llm_processor import Client as ProcessorClient

from ..prompts import ACTION_DECISION_PROMPT_TEMPLATE


@dataclass
class ActionDecision:
    """
    封装LLM的行动决策结果。
    """

    tool_to_use: str | None
    arguments: dict
    raw_llm_output: str
    error: str | None = None


class ActionDecisionMaker:
    """
    负责调用LLM进行工具选择决策。
    它构建prompt，调用LLM，并解析返回的JSON决策。
    """

    def __init__(self, llm_client: ProcessorClient):
        self.logger = get_logger(f"AIcarusCore.{self.__class__.__name__}")
        if not llm_client:
            raise ValueError("LLM客户端实例 'llm_client' 不能为空。")
        self.llm_client = llm_client
        self.logger.info(f"{self.__class__.__name__} instance created.")

    async def make_decision(
        self,
        action_description: str,
        action_motivation: str,
        current_thought_context: str,
        relevant_adapter_messages_context: str,
    ) -> ActionDecision:
        """
        调用LLM来决定应该使用哪个工具以及使用什么参数。

        Args:
            action_description: 当前计划执行的动作描述。
            action_motivation: 执行该动作的动机。
            current_thought_context: 当前的思维链上下文。
            relevant_adapter_messages_context: 相关的外部消息上下文。

        Returns:
            一个 ActionDecision 对象，包含了决策结果或错误信息。
        """
        self.logger.info(f"开始为动作 '{action_description[:50]}...' 进行LLM决策。")
        prompt = ACTION_DECISION_PROMPT_TEMPLATE.format(
            current_thought_context=current_thought_context,
            action_description=action_description,
            action_motivation=action_motivation,
            relevant_adapter_messages_context=relevant_adapter_messages_context,
        )

        response = await self.llm_client.make_llm_request(prompt=prompt, is_stream=False)
        raw_text = response.get("text", "").strip()

        if response.get("error"):
            error_msg = f"行动决策LLM调用失败: {response.get('message', '未知API错误')}"
            self.logger.error(error_msg)
            return ActionDecision(None, {}, raw_text, error=error_msg)

        if not raw_text:
            error_msg = "行动决策失败，LLM的响应中不包含任何文本内容。"
            self.logger.warning(error_msg)
            return ActionDecision(None, {}, raw_text, error=error_msg)

        try:
            json_string = raw_text
            if json_string.startswith("```json"):
                json_string = json_string[7:]
            if json_string.startswith("```"):
                json_string = json_string[3:]
            if json_string.endswith("```"):
                json_string = json_string[:-3]
            json_string = json_string.strip()

            parsed_decision = json.loads(json_string)
            tool_name = parsed_decision.get("tool_to_use")
            arguments = parsed_decision.get("arguments", {})

            self.logger.info(f"LLM决策完成。选择的工具: '{tool_name}'")
            return ActionDecision(tool_name, arguments, raw_text)

        except (json.JSONDecodeError, TypeError) as e:
            error_msg = f"解析LLM决策JSON失败: {e}. 原始文本: {raw_text[:200]}..."
            self.logger.error(error_msg)
            return ActionDecision(None, {}, raw_text, error=error_msg)
