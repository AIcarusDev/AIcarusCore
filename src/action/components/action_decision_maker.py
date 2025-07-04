# src/action/components/action_decision_maker.py
import json
from dataclasses import dataclass
from typing import Any

from src.common.custom_logging.logging_config import get_logger
from src.common.json_parser.json_parser import parse_llm_json_response
from src.llmrequest.llm_processor import Client as ProcessorClient

logger = get_logger(__name__)


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

    def __init__(self, llm_client: ProcessorClient) -> None:
        if not llm_client:
            raise ValueError("LLM客户端实例 'llm_client' 不能为空。")
        self.llm_client = llm_client
        logger.info(f"{self.__class__.__name__} instance created.")

    def _build_decision_prompt(
        self,
        tools_schema: list[dict[str, Any]],
        current_thought_context: str,
        action_description: str,
        action_motivation: str,
        relevant_adapter_messages_context: str,
    ) -> str:
        # 把工具说明书（schema）转换成好看的JSON字符串
        # 这里的 tools_schema 是一个包含了所有工具（平台+内部）的列表
        tools_json_string = json.dumps(tools_schema, indent=2, ensure_ascii=False)

        # 这是新的Prompt模板，它会把工具说明书塞进去
        prompt_template = f"""你是我的智能行动决策系统，你的任务是分析我的的思考和行动意图，然后从下方提供的<目前可用工具列表>中选择一个最合适的工具，并以指定的JSON格式输出你的决策。

<目前可用工具列表>
{tools_json_string}
</目前可用工具列表>

输入信息:
我的当前的思考上下文: "{current_thought_context}"
我的明确想做的动作（原始意图描述）: "{action_description}"
我的的动机（原始行动动机）: "{action_motivation}"
最近可能相关的外部消息或请求 (如果适用): {relevant_adapter_messages_context}

你的决策应遵循以下步骤：
1. 仔细理解我的想要完成的动作、我为什么想做这个动作，以及我此刻正在思考什么。
2. 然后，查看提供的工具列表，判断是否有某个工具的功能与我的行动意图或响应外部请求的需求相匹配。
3. 如果找到了能够满足我意图的工具，请选择它，并为其准备好准确的调用参数。
4. 如果经过分析，认为我的意图不适合使用上述任何具体工具，或者动作无法完成，请选择"internal.report_action_failure"工具，并提供原因。
5. 你的最终输出**必须严格**是一个JSON对象字符串，结构如下。不要包含任何额外的解释、注释或 "```json" 标记。

**输出格式:**
{{
    "tool_to_use": "你选择的工具的唯一标识符 (例如 'platform.napcat_adapter_default_instance.send_message', 'internal.web_search')",
    "arguments": {{
        "参数1名称": "参数1的值",
        "参数2名称": "参数2的值"
    }}
}}

输出json：
"""
        return prompt_template

    async def make_decision(
        self,
        action_description: str,
        action_motivation: str,
        current_thought_context: str,
        relevant_adapter_messages_context: str,
        tools_schema: list[dict[str, Any]],  # 接收“功能说明书”
    ) -> ActionDecision:
        logger.info(f"开始为动作 '{action_description[:50]}...' 进行LLM决策。")

        prompt = self._build_decision_prompt(
            tools_schema,
            current_thought_context,
            action_description,
            action_motivation,
            relevant_adapter_messages_context,
        )

        # 把工具说明书也传给LLM，这样它才能正确地进行 tool_call
        response = await self.llm_client.make_llm_request(prompt=prompt, is_stream=False, tools=tools_schema)
        raw_text = response.get("text", "").strip()

        # 检查LLM是否直接返回了tool_calls
        tool_calls = response.get("tool_calls")
        if tool_calls and isinstance(tool_calls, list) and len(tool_calls) > 0:
            logger.info("LLM直接返回了 tool_calls 结构，将优先使用。")
            chosen_tool_call = tool_calls[0]  # 只取第一个
            tool_name = chosen_tool_call.get("function", {}).get("name")
            try:
                # LLM返回的arguments可能是字符串，需要解析
                arguments_str = chosen_tool_call.get("function", {}).get("arguments", "{}")
                arguments = json.loads(arguments_str) if isinstance(arguments_str, str) else arguments_str
            except json.JSONDecodeError:
                logger.error(f"解析 tool_calls 中的 arguments 失败: {arguments_str}")
                arguments = {}
            return ActionDecision(tool_name, arguments, raw_text)

        # 如果没有tool_calls，再尝试从文本里解析
        if not raw_text:
            error_msg = "行动决策失败，LLM的响应中不包含任何文本内容或 tool_calls。"
            logger.warning(error_msg)
            return ActionDecision(None, {}, "", error=error_msg)

        if parsed_decision := parse_llm_json_response(raw_text):
            tool_name = parsed_decision.get("tool_to_use")
            arguments = parsed_decision.get("arguments", {})
            if not isinstance(arguments, dict):
                arguments = {}
            logger.info(f"LLM决策完成（通过解析文本）。选择的工具: '{tool_name}'")
            return ActionDecision(tool_name, arguments, raw_text)
        else:
            error_msg = f"解析LLM决策JSON失败。 原始文本: {raw_text[:200]}..."
            logger.error(error_msg)
            return ActionDecision(None, {}, raw_text, error=error_msg)
