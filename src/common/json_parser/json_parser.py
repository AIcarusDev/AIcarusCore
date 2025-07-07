# src/common/json_parser.py
import json
import re
from typing import Any

from src.common.custom_logging.logging_config import get_logger

logger = get_logger(__name__)


def parse_llm_json_response(raw_response_text: str | None) -> dict[str, Any] | None:
    """
    一个超级健壮的LLM JSON响应解析器。
    哼，现在它学会了“暴力破解”，就算LLM不听话，也能把JSON挖出来。

    Args:
        raw_response_text: 来自LLM的原始文本响应。

    Returns:
        解析成功后的字典，或者在任何失败情况下返回 None。
    """
    if not raw_response_text or not raw_response_text.strip():
        logger.debug("输入文本为空，没什么好解析的。")
        return None

    text_to_parse = raw_response_text.strip()
    json_str = ""

    if match := re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", text_to_parse, re.DOTALL):
        json_str = match[1]
        logger.debug("策略一命中：通过正则表达式找到了JSON代码块。")
    else:
        # 策略二（后备）：如果没找到代码块，就粗暴地找到第一个 '{' 和最后一个 '}'
        # 这能处理那些LLM直接输出JSON，前后可能还带点废话的情况
        start_index = text_to_parse.find("{")
        end_index = text_to_parse.rfind("}")

        if start_index != -1 and end_index > start_index:
            json_str = text_to_parse[start_index : end_index + 1]
            logger.debug("策略二命中：通过查找第一个 '{' 和最后一个 '}' 来提取潜在的JSON。")
        else:
            # 如果连花括号都找不到，那就真的没救了
            logger.warning(f"响应中找不到有效的JSON对象结构。原始响应: {text_to_parse}")
            return None

    # 开始解析前，先对提取出来的字符串做个小手术，处理掉讨厌的末尾逗号
    try:
        # 这个正则表达式会找到紧跟在 '}' 或 ']' 前面的逗号（以及可能存在的空格），并把它删掉
        # 比如 "key": "value",} -> "key": "value"}
        json_str_cleaned = re.sub(r",\s*(?=[}\]])", "", json_str)

        parsed_dict = json.loads(json_str_cleaned)

        # 确保解析出来的确实是个字典，而不是列表或者其他什么鬼东西
        if isinstance(parsed_dict, dict):
            logger.debug("JSON成功解析为字典。")
            return parsed_dict
        else:
            logger.warning(f"成功解析了JSON，但结果不是字典类型，而是 {type(parsed_dict)}。这可能不是我们想要的。")
            return None

    except json.JSONDecodeError as e:
        logger.error(f"解析LLM的JSON响应时最终失败: {e}")
        logger.debug(f"解析失败的JSON字符串 (清洗后): {json_str_cleaned}")
        return None
