# src/core_logic/thought_generator.py
from typing import Any, Dict, List, Optional, TYPE_CHECKING
import json # 确保导入 json

from src.common.custom_logging.logger_manager import get_logger

if TYPE_CHECKING:
    from src.llmrequest.llm_processor import Client as ProcessorClient
    from src.core_logic.prompt_builder import ThoughtPromptBuilder

logger = get_logger("AIcarusCore.CoreLogic.ThoughtGenerator")

class ThoughtGenerator:
    def __init__(
        self,
        llm_client: 'ProcessorClient'
    ):
        self.llm_client = llm_client
        # self.prompt_builder = prompt_builder # 不再需要保存实例
        self.logger = logger
        self.logger.info("ThoughtGenerator 已初始化。")

    async def generate_thought(
        self, system_prompt: str, user_prompt: str, image_inputs: List[str]
    ) -> Optional[Dict[str, Any]]:
        """
        调用LLM生成思考结果，并解析响应。
        """
        try:
            response_data = await self.llm_client.make_llm_request(
                prompt=user_prompt,
                system_prompt=system_prompt,
                is_stream=False,
                image_inputs=image_inputs or None,
                is_multimodal=bool(image_inputs),
            )

            if response_data.get("error"):
                self.logger.error(f"LLM调用失败: {response_data.get('message', '未知错误')}")
                return None

            raw_text = response_data.get("text", "")
            if not raw_text:
                self.logger.error("LLM响应中缺少文本内容。")
                return None

            # 使用 ThoughtPromptBuilder 的静态方法来解析响应
            # 需要从 .prompt_builder 导入 ThoughtPromptBuilder 类本身
            from .prompt_builder import ThoughtPromptBuilder # 局部导入或在文件顶部导入
            parsed_json = ThoughtPromptBuilder.parse_llm_response(raw_text)

            if parsed_json is None:
                self.logger.error("解析LLM的JSON响应失败，它返回了None。")
                return None

            if response_data.get("usage"):
                parsed_json["_llm_usage_info"] = response_data.get("usage")

            self.logger.info("LLM API 的回应已成功解析为JSON。")
            return parsed_json

        except Exception as e:
            self.logger.error(f"调用LLM或解析响应时发生意外错误: {e}", exc_info=True)
            return None
