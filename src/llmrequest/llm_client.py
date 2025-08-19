# src/llmrequest/llm_client.py

import asyncio
import json
import os
import re
from typing import Any, Unpack

from src.common.custom_logging.logging_config import get_logger

from .core.media_processor import MediaProcessor
from .core.models import APIKeyError, APIKeyManager, GenerationParams
from .core.provider_handler import ApiProviderHandler, GoogleApiHandler, OpenAIApiHandler
from .core.request_executor import RequestExecutor

logger = get_logger(__name__)

class LLMClient:
    """新的、苗条的LLM客户端，作为所有LLM请求的统一入口。
    它负责组合各个核心组件来完成请求。
    """

    def __init__(
        self,
        model: dict,
        abandoned_keys_config: list[str] | None = None,
        proxy_host: str | None = None,
        proxy_port: int | None = None,
        image_placeholder_tag: str = "[IMAGE_HERE]",
        stream_chunk_delay_seconds: float = 0.05,
        enable_image_compression: bool = True,
        image_compression_target_bytes: int = 1 * 1024 * 1024,
        rate_limit_disable_duration_seconds: int = 30 * 60,
        **kwargs: Unpack[GenerationParams],
    ) -> None:
        if not isinstance(model, dict) or "provider" not in model or "name" not in model:
            raise ValueError("`model` 参数必须是一个包含 'provider' 和 'name' 键的字典。")

        self.model_name: str = model["name"]
        self.provider: str = model["provider"].upper()
        self.default_generation_config: GenerationParams = kwargs
        self.stream_chunk_delay_seconds = stream_chunk_delay_seconds
        
        self.image_placeholder_pattern = re.compile(r"\[(图片|动画表情|GIF)_(\d+)]")
        self.proxy_url = f"http://{proxy_host}:{proxy_port}" if proxy_host and proxy_port else None
        
        # 1. 初始化专家组件
        api_keys, self.base_url = self._load_provider_config()
        key_manager = APIKeyManager(
            initial_keys=api_keys,
            abandoned_keys_config=set(abandoned_keys_config or []),
            rate_limit_disable_seconds=rate_limit_disable_duration_seconds,
        )
        self.media_processor = MediaProcessor(enable_image_compression, image_compression_target_bytes)
        
        # --- [FIX START] ---
        # 将 self.base_url 传递给 RequestExecutor
        self.request_executor = RequestExecutor(key_manager, self.base_url, self.proxy_url)
        # --- [FIX END] ---

        self.handler = self._get_provider_handler()
        
        logger.info(f"LLMClient (Refactored) for '{self.provider}' initialized. Model: {self.model_name}")

    def _load_provider_config(self) -> tuple[list[str], str]:
        """从环境变量加载API密钥和Base URL。"""
        env_prefix = re.sub(r"[^A-Z0-9_]", "_", self.provider.upper())
        keys_env_var = f"{env_prefix}_API_KEYS"
        base_url_env_var = f"{env_prefix}_BASE_URL"

        raw_keys = os.getenv(keys_env_var) or os.getenv(f"{env_prefix}_KEY")
        if not raw_keys:
            raise APIKeyError(f"环境变量 {keys_env_var} 或 {env_prefix}_KEY 未设置。")
        
        try:
            keys = json.loads(raw_keys) if raw_keys.startswith("[") else raw_keys.split(",")
        except json.JSONDecodeError:
            keys = [k.strip() for k in raw_keys.split(",") if k.strip()]

        base_url = os.getenv(base_url_env_var)
        if not base_url:
            raise ValueError(f"环境变量 {base_url_env_var} 未设置。")
            
        return [str(k).strip() for k in keys if str(k).strip()], base_url.rstrip("/")

    def _get_provider_handler(self) -> ApiProviderHandler:
        """根据 provider 和 base_url 选择正确的 API Handler。"""
        if self.provider == "GEMINI":
            return GoogleApiHandler(self.image_placeholder_pattern)
        if self.provider in ["OPENAI", "SILICONFLOW", "DEEPSEEK", "CHATANYWHERE"]:
            return OpenAIApiHandler(self.image_placeholder_pattern)
        if "googleapis.com" in self.base_url:
            return GoogleApiHandler(self.image_placeholder_pattern)
        if "openai" in self.base_url:
            return OpenAIApiHandler(self.image_placeholder_pattern)
        
        logger.warning(f"无法为 provider '{self.provider}' 自动确定API风格，默认为 OpenAI。")
        return OpenAIApiHandler(self.image_placeholder_pattern)

    async def make_request(
        self,
        prompt: str,
        system_prompt: str | None,
        is_stream: bool,
        is_multimodal: bool = False,
        image_inputs: list[str] | None = None,
        temp: float | None = None,
        max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict | None = None,
        image_mime_type_override: str | None = None,
        max_retries: int = 3,
        interruption_event: asyncio.Event | None = None,
        use_google_search: bool = False,
        **kwargs: Unpack[GenerationParams],
    ) -> dict[str, Any]:
        """Orchestrates a request to the LLM API."""
        request_type = "chat"
        if tools: request_type = "tool_call"
        elif is_multimodal and image_inputs: request_type = "vision"

        generation_params = self.default_generation_config.copy()
        generation_params.update(kwargs)
        if temp is not None: generation_params["temperature"] = temp
        if max_tokens is not None: generation_params["maxOutputTokens"] = max_tokens

        # --- [FIX START] ---
        # 3. 在调用 media_processor 时，使用实例属性 self.proxy_url
        processed_images = await self.media_processor.process_media_inputs(
            image_inputs, image_mime_type_override, self.proxy_url
        ) if is_multimodal and image_inputs else None
        # --- [FIX END] ---

        return await self.request_executor.execute_request(
            handler=self.handler,
            request_type=request_type,
            is_streaming=is_stream,
            prompt=prompt,
            system_prompt=system_prompt,
            processed_images=processed_images,
            generation_params=generation_params,
            tools=tools,
            tool_choice=tool_choice,
            text_to_embed=None,
            model_name=self.model_name,
            max_retries=max_retries,
            interruption_event=interruption_event,
            enable_google_search=use_google_search,
            stream_chunk_delay=self.stream_chunk_delay_seconds
        )
    
    async def get_embedding(
        self,
        text_to_embed: str,
        max_retries: int = 3,
        **kwargs: Unpack[GenerationParams]
    ) -> dict[str, Any]:
        """Generates embeddings for the given text."""
        generation_params = self.default_generation_config.copy()
        generation_params.update(kwargs)

        return await self.request_executor.execute_request(
            handler=self.handler,
            request_type="embedding",
            is_streaming=False,
            prompt=None, system_prompt=None, processed_images=None,
            generation_params=generation_params,
            tools=None, tool_choice=None,
            text_to_embed=text_to_embed,
            model_name=self.model_name,
            max_retries=max_retries,
            interruption_event=None,
            enable_google_search=False,
            stream_chunk_delay=0.0
        )
    
    async def close(self) -> None:
        # In this refactored version, aiohttp sessions are created and closed per-request
        # so this method is a no-op but kept for interface compatibility.
        pass