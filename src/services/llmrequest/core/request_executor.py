# src/llmrequest/core/request_executor.py

import asyncio
import json
from typing import Any

import aiohttp
from src.common.custom_logging.logging_config import get_logger

from .models import (
    APIKeyManager,
    APIResponseError,
    GenerationParams,
    LLMClientError,
    NetworkError,
    PayloadTooLargeError,
    PermissionDeniedError,
    RateLimitError,
)
from .provider.base import ApiProviderHandler

logger = get_logger(__name__)

INITIAL_RETRY_PASS_DELAY_SECONDS: float = 10.0


class RequestExecutor:
    """Handles the execution of API requests with retries, key management, and error handling."""

    def __init__(self, key_manager: APIKeyManager, base_url: str, proxy_url: str | None) -> None:
        self.key_manager = key_manager
        self.base_url = base_url
        self.proxy_url = proxy_url

    async def execute_request(
        self,
        handler: ApiProviderHandler,
        model_name: str,
        request_type: str,
        is_streaming: bool,
        prompt_parts: list[dict],
        system_prompt: str | None,
        processed_images: list[dict[str, str]] | None,
        generation_params: GenerationParams,
        tools: list[dict[str, Any]] | None,
        tool_choice: str | dict | None,
        text_to_embed: str | None,
        max_retries: int,
        interruption_event: asyncio.Event | None,
        enable_google_search: bool,
        stream_chunk_delay: float,
    ) -> dict[str, Any]:
        """Execute an API request with retries, key management, and error handling.

        Parameters
        ----------
        handler : ApiProviderHandler
            The API provider handler to use for request preparation and response parsing.
        model_name : str
            The name of the model to use for the request.
        request_type : str
            The type of request (e.g., 'generate', 'embed').
        is_streaming : bool
            Whether the request should use streaming response.
        prompt : str | None
            The main prompt text for the request.
        system_prompt : str | None
            The system prompt text for the request.
        processed_images : list[dict[str, str]] | None
            List of processed image data for multimodal requests.
        generation_params : GenerationParams
            Parameters for text generation.
        tools : list[dict[str, Any]] | None
            List of tools available for the model.
        tool_choice : str | dict | None
            Tool selection configuration.
        text_to_embed : str | None
            Text to embed for embedding requests.
        max_retries : int
            Maximum number of retry attempts.
        interruption_event : asyncio.Event | None
            Event to signal request interruption.
        enable_google_search : bool
            Whether to enable Google search functionality.
        stream_chunk_delay : float
            Delay between streaming chunks in seconds.

        Returns:
        -------
        dict[str, Any]
            Response dictionary containing the API response or error information.

        Raises:
        ------
        LLMClientError
            If all API request attempts fail or no available API keys are found.
        """
        last_exception: Exception | None = None

        for attempt_pass in range(max_retries + 1):
            if interruption_event and interruption_event.is_set():
                return {
                    "error": False,
                    "interrupted": True,
                    "message": "Task interrupted before API call.",
                }

            available_keys = self.key_manager.get_available_keys()
            if not available_keys:
                logger.error(f"在第 {attempt_pass + 1} 次尝试轮中，已无任何可用API密钥。")
                break

            logger.info(
                f"开始第 {attempt_pass + 1}/{max_retries + 1} 次请求尝试轮。"
                f"本轮可用密钥数: {len(available_keys)}"
            )

            for _key_idx, current_key in enumerate(available_keys):
                payload = {}
                try:
                    path, params, headers, payload = handler.prepare_request_data(
                        model_name,
                        request_type,
                        is_streaming,
                        prompt_parts,
                        system_prompt,
                        generation_params,
                        tools,
                        tool_choice,
                        text_to_embed,
                        enable_google_search,
                    )

                    result = await self._make_api_call(
                        handler,
                        current_key,
                        path,
                        params,
                        headers,
                        payload,
                        is_streaming,
                        request_type,
                        stream_chunk_delay,
                    )
                    return result

                except PermissionDeniedError as e:
                    logger.error(f"密钥 ...{current_key[-4:]} 遇到权限拒绝: {e!s}. 将被永久弃用。")
                    self.key_manager.permanently_abandon_key(e.key_identifier or current_key)
                    last_exception = e
                except RateLimitError as e:
                    logger.warning(f"密钥 ...{current_key[-4:]} 达到速率限制。将被临时禁用。")
                    self.key_manager.temporarily_disable_key(e.key_identifier or current_key)
                    last_exception = e
                except (NetworkError, APIResponseError, LLMClientError) as e:
                    # 增强此处的日志记录
                    error_details = (
                        f"尝试密钥 ...{current_key[-4:]} 失败: {type(e).__name__} - {e!s}"
                    )
                    # 检查异常对象是否有 response_text 属性
                    if hasattr(e, "response_text") and e.response_text:
                        error_details += f"\n--> API 响应体: {e.response_text}"

                    # 尝试记录发送的 payload
                    try:
                        payload_str = json.dumps(payload, ensure_ascii=False, indent=2)
                        error_details += f"\n--> 发送的 Payload: \n{payload_str}"
                    except Exception:
                        error_details += f"\n--> 发送的 Payload (无法序列化): {payload}"

                    logger.warning(error_details)
                    last_exception = e
                except Exception as e:
                    logger.error(
                        f"尝试密钥 ...{current_key[-4:]} 时发生未知严重错误: {e!s}", exc_info=True
                    )
                    last_exception = e

            if attempt_pass < max_retries:
                wait_duration = INITIAL_RETRY_PASS_DELAY_SECONDS * (2**attempt_pass)
                logger.warning(
                    f"第 {attempt_pass + 1} 次请求尝试轮未成功。等待 {wait_duration:.2f} 秒后重试。"
                )
                await asyncio.sleep(wait_duration)

        if last_exception:
            error_type = type(last_exception).__name__
            message = f"所有API请求尝试均失败。最终错误: {last_exception!s}"

            # --- [BUG FIX] ---
            # 从捕获到的异常对象中提取 status_code
            status_code = getattr(last_exception, "status_code", None)
            # --- [BUG FIX END] ---

            return {
                "error": True,
                "type": error_type,
                "message": message,
                "details": str(last_exception),
                "status_code": status_code,  # <-- 将 status_code 加入返回的字典
            }

        raise LLMClientError("所有API请求尝试轮均失败，或未能找到可用API密钥。")

    async def _make_api_call(
        self,
        handler: ApiProviderHandler,
        api_key: str,
        path: str,
        params: dict,
        headers: dict,
        payload: dict,
        is_streaming: bool,
        request_type: str,
        stream_chunk_delay: float,
    ) -> dict[str, Any]:
        # 正确的 URL 构建逻辑
        full_request_url = f"{self.base_url}{path}"
        final_headers = headers.copy()
        final_params = params.copy()
        # Inject API key based on provider style
        if "Authorization" in final_headers:
            final_headers["Authorization"] = final_headers["Authorization"].format(api_key=api_key)
        else:
            final_params["key"] = api_key

        try:
            prepared_data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        except TypeError as e:
            raise LLMClientError(f"Payload序列化失败: {e}") from e

        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(
                    full_request_url,
                    headers=final_headers,
                    data=prepared_data,
                    params=final_params,
                    proxy=self.proxy_url,
                    timeout=120,
                ) as http_response:
                    status_code = http_response.status
                    if 200 <= status_code < 300:
                        if is_streaming:
                            return await handler.handle_streaming_response(
                                http_response, stream_chunk_delay
                            )
                        else:
                            response_json = await http_response.json()
                            return handler.parse_non_streaming_response(response_json, request_type)
                    else:
                        response_text = await http_response.text()
                        if status_code == 413:
                            raise PayloadTooLargeError("请求体过大", status_code, response_text)
                        if status_code in [401, 403]:
                            raise PermissionDeniedError(
                                "权限被拒绝", status_code, response_text, key_identifier=api_key
                            )
                        if status_code == 429:
                            raise RateLimitError(
                                "速率限制", status_code, response_text, key_identifier=api_key
                            )
                        raise APIResponseError(f"API错误 {status_code}", status_code, response_text)
            except aiohttp.ClientError as e:
                raise NetworkError(f"AIOHTTP客户端错误: {e}", original_exception=e) from e
