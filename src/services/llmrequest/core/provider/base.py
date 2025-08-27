# src/llmrequest/core/provider/base.py
import re
from abc import ABC, abstractmethod
from typing import Any

from src.services.llmrequest.core.models import GenerationParams


class ApiProviderHandler(ABC):
    """所有 API Provider Handler 的抽象基类.

    它定义了请求准备、响应解析和流式处理的统一接口。
    """

    def __init__(self, image_placeholder_pattern: re.Pattern) -> None:
        self.image_placeholder_pattern = image_placeholder_pattern

    @abstractmethod
    def prepare_request_data(
        self,
        model_name: str,
        request_type: str,
        is_streaming: bool,
        prompt: str | None,
        system_prompt: str | None,
        processed_images: list[dict[str, str]] | None,
        final_generation_config: GenerationParams,
        tools: list[dict[str, Any]] | None,
        tool_choice: str | dict | None,
        text_to_embed: str | None,
        enable_google_search: bool,
    ) -> tuple[str, dict, dict, dict]:
        """准备 API 请求所需的所有数据。.

        返回:
            一个元组 (path, params, headers, payload)。
        """
        pass

    @abstractmethod
    def parse_non_streaming_response(
        self, response_json: dict[str, Any], request_type: str
    ) -> dict[str, Any]:
        """解析非流式的 API 响应."""
        pass

    @abstractmethod
    async def handle_streaming_response(
        self, response: Any, stream_chunk_delay: float
    ) -> dict[str, Any]:
        """处理并解析流式的 API 响应."""
        pass

    def _interleave_text_and_images(
        self, prompt_text: str, processed_images: list[dict[str, str]]
    ) -> list[dict[str, Any]]:
        """一个通用的辅助函数，用于将文本和图片交错组合成 OpenAI Vision API 的格式."""
        if not processed_images:
            return [{"type": "text", "text": prompt_text or ""}]
        if not prompt_text:
            return [
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{img['mime_type']};base64,{img['b64_data']}"},
                }
                for img in processed_images
            ]

        elements: list[dict[str, Any]] = []
        last_end = 0
        for match in self.image_placeholder_pattern.finditer(prompt_text):
            if match.start() > last_end:
                elements.append({"type": "text", "text": prompt_text[last_end : match.start()]})
            try:
                image_index = int(match.group(2)) - 1
                if 0 <= image_index < len(processed_images):
                    img = processed_images[image_index]
                    elements.append(
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{img['mime_type']};base64,{img['b64_data']}"
                            },
                        }
                    )
            except (ValueError, IndexError):
                pass
            last_end = match.end()
        if last_end < len(prompt_text):
            elements.append({"type": "text", "text": prompt_text[last_end:]})
        return elements
