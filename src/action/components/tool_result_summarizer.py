# src/action/components/tool_result_summarizer.py
import json

from src.common.custom_logging.logging_config import get_logger
from src.llmrequest.llm_processor import Client as ProcessorClient

from ..prompts import INFORMATION_SUMMARY_PROMPT_TEMPLATE

logger = get_logger(__name__)


class ToolResultSummarizer:
    """
    负责将工具执行的原始结果总结为对AI更有用的自然语言。
    """

    def __init__(self, llm_client: ProcessorClient) -> None:
        logger = get_logger(f"AIcarusCore.{self.__class__.__name__}")
        if not llm_client:
            raise ValueError("LLM客户端实例 'llm_client' 不能为空。")
        self.llm_client = llm_client
        logger.info(f"{self.__class__.__name__} instance created.")

    async def summarize(self, original_query: str, original_motivation: str, tool_output: str | list | dict) -> str:
        """
        调用LLM对工具的原始输出进行总结。

        Args:
            original_query: 触发工具调用的原始查询或动作。
            original_motivation: 执行该动作的动机。
            tool_output: 工具返回的原始数据。

        Returns:
            总结后的文本，或在出错时返回错误信息。
        """
        logger.info(f"正在对工具结果进行信息总结。原始意图: '{original_query[:50]}...'")
        try:
            raw_tool_output_str = (
                json.dumps(tool_output, indent=2, ensure_ascii=False, default=str)
                if isinstance(tool_output, list | dict)
                else str(tool_output)
            )
        except TypeError:
            raw_tool_output_str = str(tool_output)

        summary_prompt = INFORMATION_SUMMARY_PROMPT_TEMPLATE.format(
            original_query_or_action=original_query,
            original_motivation=original_motivation,
            raw_tool_output=raw_tool_output_str,
        )
        response = await self.llm_client.make_llm_request(prompt=summary_prompt, is_stream=False)

        if response.get("error"):
            error_message = f"总结信息时LLM调用失败: {response.get('message', '未知API错误')}"
            logger.error(error_message)
            return error_message

        summary_text = response.get("text")
        if not summary_text or not summary_text.strip():
            logger.warning("信息总结LLM调用成功，但未返回有效的文本内容。")
            return "未能从工具结果中总结出有效信息。"

        logger.info(f"信息总结完成。摘要 (前50字符): {summary_text[:50]}...")
        return summary_text.strip()
