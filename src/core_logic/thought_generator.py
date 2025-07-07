# src/core_logic/thought_generator.py
import uuid
from typing import TYPE_CHECKING, Any

from src.common.custom_logging.logging_config import get_logger
from src.common.json_parser.json_parser import parse_llm_json_response

if TYPE_CHECKING:
    from src.llmrequest.llm_processor import Client as ProcessorClient

logger = get_logger(__name__)


class ThoughtGenerator:
    def __init__(self, llm_client: "ProcessorClient") -> None:
        self.llm_client = llm_client
        logger.info("ThoughtGenerator 已初始化。")

    async def generate_thought(
        self,
        system_prompt: str,
        user_prompt: str,
        image_inputs: list[str],
        response_schema: dict[str, Any] | None = None,  # <--- 看这里！我给它加上了！
    ) -> dict[str, Any] | None:
        """调用LLM生成思考结果，并解析响应。
        确保 response_schema 被正确传递。
        """
        try:
            response_data = await self.llm_client.make_llm_request(
                prompt=user_prompt,
                system_prompt=system_prompt,
                is_stream=False,
                image_inputs=image_inputs or None,
                is_multimodal=bool(image_inputs),
                use_google_search=False,  # 主意识不开启接地搜索
                response_schema=response_schema,  # <--- 在这里把它传下去！
            )

            if response_data.get("error"):
                logger.error(f"LLM调用失败: {response_data.get('message', '未知错误')}")
                return None

            raw_text = response_data.get("text", "")
            if not raw_text:
                logger.error("LLM响应中缺少文本内容。")
                return None

            parsed_json = parse_llm_json_response(raw_text)

            if parsed_json is None:
                logger.error("解析LLM的JSON响应失败，它返回了None。")
                return None

            if response_data.get("usage"):
                parsed_json["_llm_usage_info"] = response_data.get("usage")

            # 在这里，我们给生成的思考结果也加上唯一的ID，方便追踪
            if "action" in parsed_json and isinstance(parsed_json["action"], dict):
                # 如果有动作，就用 action_id 作为整个思考的ID
                parsed_json["thought_id"] = parsed_json.get("action", {}).get(
                    "action_id", str(uuid.uuid4())
                )
            else:
                parsed_json["thought_id"] = str(uuid.uuid4())

            logger.info(f"LLM API 的回应已成功解析为JSON。Thought ID: {parsed_json['thought_id']}")
            return parsed_json

        except Exception as e:
            logger.error(f"调用LLM或解析响应时发生意外错误: {e}", exc_info=True)
            return None
