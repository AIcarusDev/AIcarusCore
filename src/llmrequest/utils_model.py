# 文件: llmrequest/utils_model.py
# 工具模型模块，提供与语言模型交互的辅助功能。

import asyncio
import base64
import contextlib
import io
import json
import mimetypes
import os
import random
import re
import time
from collections.abc import Callable
from typing import Any, TypedDict, Unpack

import aiohttp
from PIL import Image
from src.common.custom_logging.logging_config import get_logger
from src.config import config

# --- 日志配置 ---
logger = get_logger(__name__)


# --- 定义 TypedDict 用于 default_generation_config ---
class GenerationParams(TypedDict, total=False):
    """定义生成请求的参数类型.

    Attributes:
        model: str - 使用的模型名称。
        prompt: str - 输入的提示文本。
        systemPrompt: str - 系统提示文本，用于设置上下文。
        temperature: float - 生成文本的温度，控制随机性。
        maxOutputTokens: int - 最大输出令牌数。
        topP: float - nucleus sampling 的概率阈值。
        topK: int - top-k 采样的 k 值。
        stopSequences: list[str] - 停止生成的序列列表。
        candidateCount: int - 生成候选答案的数量。
        presence_penalty: float - 对新话题的惩罚系数。
        frequency_penalty: float - 对频繁出现的词语的惩罚系数。
        seed: int - 随机种子，用于结果的可重复性。
        user: str - 用户标识符，用于跟踪请求。
        response_mime_type: str - 响应的 MIME 类型，默认为 "application/json"。
        responseSchema: dict[str, Any] - 响应的 JSON Schema，用于验证响应格式。
        encoding_format: str - 输入文本的编码格式，默认为 "utf-8"。
        dimensions: int - 图像生成的维度，默认为 512。
    """

    temperature: float
    maxOutputTokens: int
    topP: float
    topK: int
    stopSequences: list[str]
    candidateCount: int
    presence_penalty: float
    frequency_penalty: float
    seed: int
    user: str
    response_mime_type: str
    responseSchema: dict[str, Any]
    encoding_format: str
    dimensions: int


class LLMClientError(Exception):
    """表示与语言模型客户端相关的通用错误."""

    pass


class APIKeyError(LLMClientError):
    """表示API密钥错误，可能是由于无效或缺失的API密钥引起的."""

    pass


class NetworkError(LLMClientError):
    """表示网络错误，可能是由于无法连接到API服务器或请求超时.

    Attributes:
        message: 错误消息。
        status_code: HTTP 状态码，默认为 None。
        original_exception: 原始异常对象，可能包含更多错误信息。
    """

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        original_exception: Exception | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.original_exception = original_exception


class RateLimitError(NetworkError):
    """表示API请求被速率限制，通常是由于超过了API密钥的使用限制.

    Attributes:
        message: 错误消息。
        status_code: HTTP 状态码，默认为 429。
        response_text: 服务器返回的响应文本，可能包含更多错误信息。
        key_identifier: API密钥标识符，可能用于识别哪个密钥被限制。
    """

    def __init__(
        self,
        message: str,
        status_code: int | None = 429,
        response_text: str | None = None,
        key_identifier: str | None = None,
    ) -> None:
        super().__init__(message, status_code=status_code, original_exception=None)
        self.response_text = response_text
        self.key_identifier = key_identifier


class PermissionDeniedError(NetworkError):
    """表示权限被拒绝，可能是由于API密钥无效或没有足够的权限访问资源.

    Attributes:
        message: 错误消息。
        status_code: HTTP 状态码，默认为 403。
        response_text: 服务器返回的响应文本，可能包含更多错误信息。
    """

    def __init__(
        self,
        message: str,
        status_code: int | None = 403,
        response_text: str | None = None,
        key_identifier: str | None = None,
    ) -> None:
        super().__init__(message, status_code=status_code, original_exception=None)
        self.response_text = response_text
        self.key_identifier = key_identifier


class APIResponseError(LLMClientError):
    """表示API响应错误，可能是由于请求格式不正确或服务器无法处理请求.

    Attributes:
        message: 错误消息。
        status_code: HTTP 状态码，默认为 None。
        response_text: 服务器返回的响应文本，可能包含更多错误信息。
    """

    def __init__(
        self, message: str, status_code: int | None = None, response_text: str | None = None
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_text = response_text


class PayloadTooLargeError(NetworkError):
    """表示请求的负载过大，无法被服务器处理.

    Attributes:
        message: 错误消息。
        status_code: HTTP 状态码，默认为 413。
        response_text: 服务器返回的响应文本，可能包含更多错误信息。
    """

    def __init__(
        self, message: str, status_code: int | None = 413, response_text: str | None = None
    ) -> None:
        super().__init__(message, status_code=status_code)
        self.response_text = response_text


DEFAULT_STREAMING_API_ENDPOINT_GOOGLE: str = ":streamGenerateContent?alt=sse"
DEFAULT_NON_STREAMING_API_ENDPOINT_GOOGLE: str = ":generateContent"
DEFAULT_EMBEDDING_ENDPOINT_GOOGLE: str = ":embedContent"
DEFAULT_CHAT_COMPLETIONS_ENDPOINT_OPENAI: str = "/chat/completions"
DEFAULT_EMBEDDINGS_ENDPOINT_OPENAI: str = "/embeddings"
DEFAULT_IMAGE_PLACEHOLDER_TAG: str = "[IMAGE_HERE]"
DEFAULT_STREAM_CHUNK_DELAY_SECONDS: float = 0.05
DEFAULT_PROXY_HOST: str | None = None
DEFAULT_PROXY_PORT: int | None = None
DEFAULT_IMAGE_COMPRESSION_TARGET_BYTES: int = 1 * 1024 * 1024
DEFAULT_IMAGE_COMPRESSION_QUALITY_JPEG: int = 85
DEFAULT_IMAGE_COMPRESSION_SCALE_MIN: float = 0.2
DEFAULT_RATE_LIMIT_DISABLE_SECONDS: int = 30 * 60
INITIAL_RETRY_PASS_DELAY_SECONDS: float = 10.0


class LLMClient:
    """LLMClient 是一个用于与语言模型API交互的客户端类.

    它支持多种语言模型提供商，并提供了配置和请求生成的功能。

    Attributes:
        model: dict - 包含模型提供商和名称的字典。
        abandoned_keys_config: list[str] | None - 可选的废弃密钥配置列表。
        proxy_host: str | None - 可选的代理主机地址。
        proxy_port: int | None - 可选的代理端口号。
        image_placeholder_tag: str - 用于替换图像的占位符标签，默认为 "[IMAGE_HERE]"。
        stream_chunk_delay_seconds: float - 流式响应的分块延迟时间，默认为 0.05 秒。
        enable_image_compression: bool - 是否启用图像压缩，默认为 True。
        image_compression_target_bytes: int - 图像压缩的目标字节数，默认为 1MB。
        rate_limit_disable_duration_seconds: int - 速率限制禁用持续时间，默认为 30 分钟。
    """

    def __init__(
        self,
        model: dict,
        abandoned_keys_config: list[str] | None = None,
        proxy_host: str | None = None,
        proxy_port: int | None = None,
        image_placeholder_tag: str = DEFAULT_IMAGE_PLACEHOLDER_TAG,
        stream_chunk_delay_seconds: float = DEFAULT_STREAM_CHUNK_DELAY_SECONDS,
        enable_image_compression: bool = True,
        image_compression_target_bytes: int = DEFAULT_IMAGE_COMPRESSION_TARGET_BYTES,
        rate_limit_disable_duration_seconds: int = DEFAULT_RATE_LIMIT_DISABLE_SECONDS,
        **kwargs: Unpack[GenerationParams],
    ) -> None:
        self.image_placeholder_pattern_regex = re.compile(r"\[(图片|动画表情)_(\d+)]")
        self.default_generation_config: GenerationParams = kwargs
        logger.debug(
            f"LLMClient __init__ received model: {model}, "
            f"default_generation_config: {self.default_generation_config}"
        )

        if not isinstance(model, dict) or "provider" not in model or "name" not in model:
            raise ValueError("`model` 参数必须是一个包含 'provider' 和 'name' 键的字典。")

        original_provider_name: str = model["provider"]
        self.env_provider_prefix = re.sub(r"[^A-Z0-9_]", "_", original_provider_name.upper())
        self.provider = original_provider_name.upper()
        self.model_name: str = model["name"]
        self.initial_stream_setting = model.get("stream", False)
        self.pri_in = model.get("pri_in", 0)
        self.pri_out = model.get("pri_out", 0)
        self.image_placeholder_tag = image_placeholder_tag
        self.stream_chunk_delay_seconds = stream_chunk_delay_seconds
        self.enable_image_compression = enable_image_compression
        self.image_compression_target_bytes = image_compression_target_bytes
        self.rate_limit_disable_duration_seconds = rate_limit_disable_duration_seconds
        self._temporarily_disabled_keys_429: dict[str, float] = {}

        api_keys_env_var_name = f"{self.env_provider_prefix}_API_KEYS"
        api_keys_env_var_name_singular = f"{self.env_provider_prefix}_KEY"
        api_keys_env_var_name_direct = self.env_provider_prefix

        raw_api_key_config = os.getenv(api_keys_env_var_name)
        if not raw_api_key_config:
            raw_api_key_config = os.getenv(api_keys_env_var_name_singular)
            if raw_api_key_config:
                logger.debug(
                    f"找到环境变量 {api_keys_env_var_name_singular}。"
                    f"推荐使用 {api_keys_env_var_name} (如果需要多个密钥)。"
                )
            else:
                raw_api_key_config = os.getenv(api_keys_env_var_name_direct)
                if raw_api_key_config:
                    logger.debug(f"找到环境变量 {api_keys_env_var_name_direct} 作为API密钥源。")
                else:
                    logger.debug(
                        f"环境变量 {api_keys_env_var_name}, {api_keys_env_var_name_singular}, "
                        f"和 {api_keys_env_var_name_direct} 均未找到。"
                    )

        self.api_keys_config: list[str] = []
        if raw_api_key_config and raw_api_key_config.strip():
            try:
                if raw_api_key_config.strip().startswith(
                    "["
                ) and raw_api_key_config.strip().endswith("]"):
                    parsed_keys = json.loads(raw_api_key_config)
                    if isinstance(parsed_keys, list):
                        self.api_keys_config = [
                            str(k).strip() for k in parsed_keys if str(k).strip()
                        ]
                    elif isinstance(parsed_keys, str) and parsed_keys.strip():
                        self.api_keys_config = [parsed_keys.strip()]
                    else:
                        logger.warning(
                            f"环境变量 {self.env_provider_prefix} 的API密钥配置解析为意外类型: "
                            f"{type(parsed_keys)}。将尝试作为单个密钥处理。"
                        )
                        self.api_keys_config = [raw_api_key_config.strip()]
                else:
                    raise json.JSONDecodeError("Not a JSON list format", raw_api_key_config, 0)
            except json.JSONDecodeError:
                if "," in raw_api_key_config:
                    self.api_keys_config = [
                        k.strip() for k in raw_api_key_config.split(",") if k.strip()
                    ]
                    if len(self.api_keys_config) > 1:
                        logger.warning(
                            f"环境变量 {self.env_provider_prefix} 的API密钥 "
                            f"'{raw_api_key_config[:20]}...' 不是有效的JSON列表格式，"
                            f"已按逗号分隔处理。推荐使用JSON数组格式来定义多个密钥。"
                        )
                else:
                    self.api_keys_config = [raw_api_key_config.strip()]

        if not self.api_keys_config:
            raise APIKeyError(
                f"未能为提供商 '{original_provider_name}' "
                f"(环境变量前缀: {self.env_provider_prefix}) 从环境变量 "
                f"({api_keys_env_var_name} 或 {api_keys_env_var_name_singular} "
                f"或 {api_keys_env_var_name_direct}) "
                f"加载任何有效的API密钥。"
            )

        self.base_url = os.getenv(f"{self.env_provider_prefix}_BASE_URL")
        if not self.base_url:
            if self.provider == "GEMINI" and os.getenv("GEMINI_BASE_URL"):
                self.base_url = os.getenv("GEMINI_BASE_URL")
                logger.debug(
                    f"使用了旧的 GEMINI_BASE_URL 环境变量。"
                    f"推荐使用 {self.env_provider_prefix}_BASE_URL。"
                )
            elif self.provider == "OPENAI" and os.getenv("OPENAI_BASE_URL"):
                self.base_url = os.getenv("OPENAI_BASE_URL")
                logger.debug(
                    f"使用了旧的 OPENAI_BASE_URL 环境变量。"
                    f"推荐使用 {self.env_provider_prefix}_BASE_URL。"
                )
            else:
                raise ValueError(
                    f"未能为提供商 '{original_provider_name}' "
                    f"(环境变量前缀: {self.env_provider_prefix}) 从环境变量 "
                    f"({self.env_provider_prefix}_BASE_URL) 加载Base URL。"
                )
        self.base_url = self.base_url.rstrip("/")

        if self.provider == "GEMINI" or ("googleapis.com" in self.base_url.lower()):
            self.api_endpoint_style = "google"
            self.streaming_endpoint_path = DEFAULT_STREAMING_API_ENDPOINT_GOOGLE
            self.non_streaming_endpoint_path = DEFAULT_NON_STREAMING_API_ENDPOINT_GOOGLE
            self.embedding_endpoint_path = DEFAULT_EMBEDDING_ENDPOINT_GOOGLE
        elif self.provider in ["OPENAI", "SILICONFLOW", "DEEPSEEK", "CHATANYWHERE"] or (
            "openai" in self.base_url.lower()
        ):
            self.api_endpoint_style = "openai"
            self.streaming_endpoint_path = DEFAULT_CHAT_COMPLETIONS_ENDPOINT_OPENAI
            self.non_streaming_endpoint_path = DEFAULT_CHAT_COMPLETIONS_ENDPOINT_OPENAI
            self.embedding_endpoint_path = DEFAULT_EMBEDDINGS_ENDPOINT_OPENAI
        else:
            logger.warning(
                f"无法根据Base URL '{self.base_url}' 或提供商 '{self.provider}' 自动确定API风格。"
                f"默认为 'openai' 风格。"
            )
            self.api_endpoint_style = "openai"
            self.streaming_endpoint_path = DEFAULT_CHAT_COMPLETIONS_ENDPOINT_OPENAI
            self.non_streaming_endpoint_path = DEFAULT_CHAT_COMPLETIONS_ENDPOINT_OPENAI
            self.embedding_endpoint_path = DEFAULT_EMBEDDINGS_ENDPOINT_OPENAI

        _abandoned_keys_list = abandoned_keys_config if abandoned_keys_config is not None else []
        self.abandoned_keys_config = {str(k) for k in _abandoned_keys_list if str(k)}
        self._abandoned_keys_runtime: set[str] = set()

        _proxy_host = (
            proxy_host if proxy_host is not None else os.getenv("PROXY_HOST", DEFAULT_PROXY_HOST)
        )
        _proxy_port_str = os.getenv("PROXY_PORT")
        _proxy_port = None
        if proxy_port is not None:
            _proxy_port = proxy_port
        elif _proxy_port_str:
            try:
                _proxy_port = int(_proxy_port_str)
            except ValueError:
                logger.warning(f"无效的 PROXY_PORT 环境变量: {_proxy_port_str}")
        elif DEFAULT_PROXY_PORT is not None:
            _proxy_port = DEFAULT_PROXY_PORT

        self.proxy_url: str | None = None
        if _proxy_host and _proxy_port is not None:
            try:
                self.proxy_url = f"http://{_proxy_host}:{int(_proxy_port)}"
            except ValueError:
                logger.warning(f"无效的代理端口号: {_proxy_port}.")

        self.stream_chunk_delay_seconds = stream_chunk_delay_seconds
        self.enable_image_compression = enable_image_compression
        self.image_compression_target_bytes = image_compression_target_bytes

        logger.info(
            f"LLMClient 为提供商 '{self.provider}' 初始化完成。"
            f"模型: {self.model_name}, API密钥数: {len(self.api_keys_config)}, "
            f"默认生成参数: {self.default_generation_config}"
            f"429临时禁用时长: {self.rate_limit_disable_duration_seconds // 60} 分钟."
        )
        if self.proxy_url:
            logger.info(f"代理: {self.proxy_url}")
        else:
            logger.info("代理未配置。")
        logger.info(
            f"图像压缩: {'启用' if self.enable_image_compression else '禁用'}, "
            f"目标大小: {self.image_compression_target_bytes / (1024 * 1024):.2f} MB"
        )
        self._session: aiohttp.ClientSession | None = None

    def _interleave_text_and_images(
        self, prompt_text: str, processed_images: list[dict[str, str]]
    ) -> list[dict[str, Any]]:
        """[Helper] 将文本和图片交错组合成一个标准化的中间列表."""
        # 如果没有图片或没有文本，则快速处理
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

        # 核心图文混排逻辑
        elements: list[dict[str, Any]] = []
        last_end = 0
        for match in self.image_placeholder_pattern_regex.finditer(prompt_text):
            # 添加占位符之前的文本
            if match.start() > last_end:
                elements.append({"type": "text", "text": prompt_text[last_end : match.start()]})

            # 添加图片
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
                else:
                    logger.warning(f"聊天记录中的图片索引 '{match.group(0)}' 超出范围，已忽略。")
            except (ValueError, IndexError) as e:
                logger.error(f"解析或使用图片索引 '{match.group(0)}' 时出错: {e}")

            last_end = match.end()

        # 添加最后一个占位符之后的文本
        if last_end < len(prompt_text):
            elements.append({"type": "text", "text": prompt_text[last_end:]})

        return elements

    def _format_element_for_google(self, element: dict[str, Any]) -> dict[str, Any]:
        """[Helper] 将标准中间元素格式化为 Google API 的格式."""
        if element["type"] == "text":
            return {"text": element["text"]}
        elif element["type"] == "image_url":
            header, encoded_data = element["image_url"]["url"].split(",", 1)
            mime_type = header.split(";")[0].split(":")[1]
            return {"inline_data": {"mime_type": mime_type, "data": encoded_data}}
        return {}

    def _format_content_for_google(
        self, intermediate_list: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """[Formatter] 将标准中间列表转换为 Google API 的 `parts` 格式."""
        return [self._format_element_for_google(elem) for elem in intermediate_list]

    def _format_content_for_openai(
        self, intermediate_list: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """[Formatter] 将标准中间列表转换为 OpenAI API 的 `content` 格式."""
        # OpenAI 的格式与我们的标准中间格式恰好一致
        return intermediate_list

    def _build_content_for_style(
        self,
        request_type: str,
        prompt_text: str | None = None,
        processed_images: list[dict[str, str]] | None = None,
        text_to_embed: str | None = None,
    ) -> Any:
        """[Orchestrator] 根据API风格组装请求内容 (重构后)."""
        # 1. 处理 embedding 的特殊情况
        if request_type == "embedding":
            if self.api_endpoint_style == "google":
                return {"parts": [{"text": text_to_embed}]} if text_to_embed else {}
            elif self.api_endpoint_style == "openai":
                return text_to_embed or ""
            raise NotImplementedError(f"Embedding for {self.api_endpoint_style} not implemented.")

        # 2. 构建标准化的中间内容列表
        intermediate_content = self._interleave_text_and_images(
            prompt_text or "", processed_images or []
        )

        # 3. 使用分发字典 (Switch Pattern) 选择正确的格式化器
        formatters: dict[str, Callable[[list[dict]], Any]] = {
            "google": self._format_content_for_google,
            "openai": self._format_content_for_openai,
        }
        formatter = formatters.get(self.api_endpoint_style)

        if not formatter:
            raise NotImplementedError(
                f"Content building for {self.api_endpoint_style} not implemented."
            )

        # 4. 调用选定的格式化器并返回结果
        return formatter(intermediate_content)

    async def _get_session(self) -> aiohttp.ClientSession:
        """获取或创建aiohttp会话."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
            logger.debug("创建新的 aiohttp.ClientSession。")
        return self._session

    async def _close_session_if_any(self) -> None:
        """如果存在活动的aiohttp会话，则关闭它."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
            logger.info("aiohttp.ClientSession 已关闭。")
        else:
            logger.debug("没有活动的 aiohttp.ClientSession 需要关闭。")

    async def _compress_base64_image(
        self, base64_data: str, original_mime_type: str
    ) -> tuple[str, str]:
        try:
            image_bytes = base64.b64decode(base64_data)
            current_size_bytes = len(image_bytes)

            img = Image.open(io.BytesIO(image_bytes))
            img_format_from_pillow = img.format
            img_format_from_mime = (
                original_mime_type.split("/")[-1].upper()
                if original_mime_type and "/" in original_mime_type
                else None
            )
            initial_img_format = img_format_from_pillow or (img_format_from_mime or "JPEG")

            current_save_format = initial_img_format
            final_mime_type = original_mime_type

            if current_size_bytes <= self.image_compression_target_bytes * 1.05:
                return base64_data, original_mime_type

            original_width, original_height = img.size
            scale_factor = max(
                DEFAULT_IMAGE_COMPRESSION_SCALE_MIN,
                min(1.0, (self.image_compression_target_bytes / current_size_bytes) ** 0.5),
            )
            new_width = max(1, int(original_width * scale_factor))
            new_height = max(1, int(original_height * scale_factor))

            output_buffer = io.BytesIO()
            save_params = {}

            if img.mode == "P":
                img = img.convert("RGBA")
            elif img.mode == "CMYK":  # CMYK必须转RGB
                img = img.convert("RGB")

            if img.mode in ("RGBA", "LA") or (
                isinstance(img.info, dict) and "transparency" in img.info
            ):
                # 对于其他有透明通道的，或者本身就是PNG的
                current_save_format = "PNG"
                final_mime_type = "image/png"
                resized_img = img.convert("RGBA").resize(
                    (new_width, new_height), Image.Resampling.LANCZOS
                )
                save_params = {"optimize": True}
            else:
                resized_img = img.convert("RGB").resize(
                    (new_width, new_height), Image.Resampling.LANCZOS
                )
                if initial_img_format == "JPEG":
                    current_save_format = "JPEG"
                    final_mime_type = "image/jpeg"
                    save_params = {
                        "quality": DEFAULT_IMAGE_COMPRESSION_QUALITY_JPEG,
                        "optimize": True,
                    }
                else:
                    current_save_format = "PNG"
                    final_mime_type = "image/png"
                    save_params = {"optimize": True}

            resized_img.save(output_buffer, format=current_save_format, **save_params)
            compressed_bytes = output_buffer.getvalue()
            new_size_bytes = len(compressed_bytes)

            logger.info(
                f"图像报告: 原始尺寸 {original_width}x{original_height} "
                f"({original_mime_type}), "
                f"新尺寸 {new_width}x{new_height} (保存为 {current_save_format}, "
                f"MIME类型 {final_mime_type}). "
                f"体积变化: {current_size_bytes / 1024:.1f}KB -> {new_size_bytes / 1024:.1f}KB"
            )

            # 对于其他类型的图片，如果压缩后体积明显减小，就用新的
            if new_size_bytes < current_size_bytes * 0.98 and new_size_bytes > 0:
                logger.info(f"图像已成功压缩 ({final_mime_type})，返回压缩后的精华。")
                return base64.b64encode(compressed_bytes).decode("utf-8"), final_mime_type
            else:
                logger.info(
                    f"图像未被压缩或压缩后体积未显著减小 (MIME: {original_mime_type})"
                    f"，返回原始数据。"
                )
                return base64_data, original_mime_type

        except Exception as e:
            logger.error(f"图像处理过程中失败，痛痛...呜呜呜: {e}", exc_info=True)
            return base64_data, original_mime_type

    async def _process_single_media_input(
        self,
        media_path_or_url_or_data_uri: str,
        session: aiohttp.ClientSession,
        mime_type_override: str | None,
        proxy_url_for_media: str | None,
    ) -> dict[str, str] | None:
        base64_media_data = None
        determined_mime_type = mime_type_override
        try:
            # 将 "data:image" 泛化为 "data:"，以同时支持图片和视频
            if media_path_or_url_or_data_uri.startswith("data:"):
                header, encoded_data = media_path_or_url_or_data_uri.split(",", 1)
                # 从 header 中解析出真实的 MIME 类型
                determined_mime_type = header.split(";")[0].split(":")[1]
                base64_media_data = encoded_data
                # [探针] 添加日志探针，明确打印出解析到的媒体类型
                logger.debug(f"已从 Data URI 中解析到媒体，类型: {determined_mime_type}")

            elif media_path_or_url_or_data_uri.startswith(("http://", "https://")):
                headers = {"User-Agent": "Mozilla/5.0", "Referer": media_path_or_url_or_data_uri}
                async with session.get(
                    media_path_or_url_or_data_uri,
                    timeout=30,
                    proxy=proxy_url_for_media,
                    headers=headers,
                ) as response:
                    if response.status == 200:
                        media_bytes = await response.read()
                        base64_media_data = base64.b64encode(media_bytes).decode("utf-8")
                        if not determined_mime_type:
                            determined_mime_type = (
                                response.headers.get("Content-Type", "").split(";")[0].strip()
                            )
                    else:
                        logger.error(
                            f"媒体文件获取失败 {media_path_or_url_or_data_uri}, "
                            f"状态码: {response.status}"
                        )
                        return None
            elif os.path.exists(media_path_or_url_or_data_uri):
                if not determined_mime_type:
                    guessed_mime, _ = mimetypes.guess_type(media_path_or_url_or_data_uri)
                    determined_mime_type = guessed_mime
                with open(media_path_or_url_or_data_uri, "rb") as media_file:
                    base64_media_data = base64.b64encode(media_file.read()).decode("utf-8")
            else:
                # 优化日志信息
                logger.error(f"媒体源未找到或无效: {media_path_or_url_or_data_uri[100:]}...")
                return None

            if not base64_media_data:
                return None

            # 统一处理MIME类型，确保其有效性
            determined_mime_type = determined_mime_type or "application/octet-stream"
            if "/" not in determined_mime_type:
                logger.warning(
                    f"无效的MIME类型 '{determined_mime_type}'，将回退到 application/octet-stream。"
                )
                determined_mime_type = "application/octet-stream"

            # 图片压缩逻辑只对图片生效
            if self.enable_image_compression and determined_mime_type.startswith("image/"):
                base64_media_data, determined_mime_type = await self._compress_base64_image(
                    base64_media_data, determined_mime_type
                )

            return {"b64_data": base64_media_data, "mime_type": determined_mime_type}
        except Exception as e:
            logger.exception(f"媒体处理过程中出错 {media_path_or_url_or_data_uri}: {e}")
            return None

    async def _process_media_inputs(
        self,
        media_sources: list[str] | None,
        mime_type_override: str | None,
    ) -> list[dict[str, str]]:
        if not media_sources:
            return []
        session = await self._get_session()
        tasks = [
            self._process_single_media_input(src, session, mime_type_override, self.proxy_url)
            for src in media_sources
        ]
        results = await asyncio.gather(*tasks)
        return [result for result in results if result]

    def _get_endpoint_path(self, request_type: str, is_streaming: bool) -> str:
        if request_type == "embedding":
            return self.embedding_endpoint_path
        return self.streaming_endpoint_path if is_streaming else self.non_streaming_endpoint_path

    def _prepare_request_data_for_style(
        self,
        request_type: str,
        prompt: str | None,
        system_prompt: str | None,
        processed_images: list[dict[str, str]] | None,
        is_streaming: bool,
        final_generation_config: GenerationParams,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict | None = None,
        text_to_embed: str | None = None,
        model_name_override: str | None = None,
        enable_google_search: bool = False,
        enable_url_context: bool = False,
    ) -> tuple[str, dict[str, Any], dict[str, Any]]:
        headers = {"Content-Type": "application/json"}
        payload: dict[str, Any] = {}
        url_path = self._get_endpoint_path(request_type, is_streaming)

        effective_model_name = model_name_override or self.model_name

        if self.api_endpoint_style == "google":
            if request_type == "embedding":
                user_content_parts = self._build_content_for_style(
                    request_type, None, None, text_to_embed
                )
                payload = {"model": f"models/{effective_model_name}", "content": user_content_parts}
            else:
                # 1. 构建最终的Payload骨架
                payload = {
                    "safetySettings": [
                        {"category": c, "threshold": "BLOCK_NONE"}
                        for c in [
                            "HARM_CATEGORY_HARASSMENT",
                            "HARM_CATEGORY_HATE_SPEECH",
                            "HARM_CATEGORY_SEXUALLY_EXPLICIT",
                            "HARM_CATEGORY_DANGEROUS_CONTENT",
                        ]
                    ],
                    "generationConfig": final_generation_config.copy(),
                }
                # 检查是否需要结构化输出 (JSON Schema)
                if "responseSchema" in payload["generationConfig"]:
                    if is_streaming:
                        logger.warning(
                            "Gemini 的结构化输出不支持流式请求。'responseSchema' 将被忽略。"
                        )
                        # 从配置中移除，避免API报错
                        del payload["generationConfig"]["responseSchema"]
                    else:
                        logger.debug("检测到 responseSchema，为 Gemini API 启用 JSON 模式。")
                        # 启用 JSON 模式时，必须指定 MIME 类型为 application/json
                        payload["generationConfig"]["response_mime_type"] = "application/json"

                # 2. 如果有system_prompt，就把它放在名为"system_instruction"的顶级王座上！
                if system_prompt:
                    logger.debug(
                        f"为 Google API 添加顶级的 system_instruction: {system_prompt[:50]}"
                        f"{'...' if len(system_prompt) > 50 else ''}"
                    )
                    payload["system_instruction"] = {"parts": [{"text": system_prompt}]}

                # 3. 构建用户的 "contents"
                user_content_parts = self._build_content_for_style(
                    request_type, prompt, processed_images
                )
                # 4. 把用户的 contents 也放进Payload里
                payload["contents"] = [{"role": "user", "parts": user_content_parts}]

                # 5. 处理Vision不支持的参数
                is_vision_request_for_google = request_type == "vision" or (
                    processed_images and len(processed_images) > 0
                )
                if is_vision_request_for_google:
                    params_to_remove = ["topP", "topK", "candidateCount", "stopSequences"]
                    gen_config = payload["generationConfig"]
                    for param in params_to_remove:
                        if param in gen_config:
                            del gen_config[param]
                            logger.debug(
                                f"Google Vision: Removed unsupported parameter '{param}' "
                                f"from generationConfig."
                            )
                active_tools = []
                if enable_google_search:
                    logger.debug("为 Google API 请求添加 Google 搜索工具。")
                    active_tools.append({"google_search": {}})

                if enable_url_context:
                    logger.debug("为 Google API 请求添加 URL 上下文工具。")
                    # 根据文档，`url_context` 的值应该是一个空的 message/object，
                    # 在Python SDK中是 types.UrlContext，在REST API中是 {}
                    active_tools.append({"url_context": {}})

                if request_type == "tool_call" and tools:
                    logger.debug(f"为 Google API 请求添加 {len(tools)} 个自定义函数调用工具。")
                    active_tools.extend(tools)

                if active_tools:
                    payload["tools"] = active_tools

            url_path = f"/{effective_model_name.strip('/')}{url_path}"

        elif self.api_endpoint_style == "openai":
            if request_type == "embedding":
                payload = {"input": text_to_embed, "model": effective_model_name}
                if "encoding_format" in final_generation_config:
                    payload["encoding_format"] = final_generation_config["encoding_format"]
                if "dimensions" in final_generation_config:
                    payload["dimensions"] = final_generation_config["dimensions"]
            else:
                messages_list: list[dict[str, Any]] = []
                if system_prompt:
                    messages_list.append({"role": "system", "content": system_prompt})

                content = self._build_content_for_style(request_type, prompt, processed_images)
                messages_list.append({"role": "user", "content": content})
                payload = {"model": effective_model_name, "messages": messages_list}

                if is_streaming:
                    payload["stream"] = True

                for key, value in final_generation_config.items():
                    if key == "maxOutputTokens":
                        payload["max_tokens"] = value
                    elif key == "stopSequences":
                        payload["stop"] = value
                    elif key == "candidateCount":
                        payload["n"] = value
                    elif key == "topP":
                        payload["top_p"] = value
                    elif key == "topK":
                        pass
                    elif key in [
                        "temperature",
                        "presence_penalty",
                        "frequency_penalty",
                        "seed",
                        "user",
                    ]:
                        payload[key] = value

                if request_type == "tool_call" and tools:
                    payload["tools"] = tools
                    if tool_choice:
                        payload["tool_choice"] = tool_choice
        else:
            raise NotImplementedError(
                f"Request data prep for {self.api_endpoint_style} not implemented."
            )

        return url_path, headers, payload

    async def _handle_streaming_response_for_style(
        self,
        response: aiohttp.ClientResponse,
        request_type: str,
        interruption_event: asyncio.Event | None = None,
    ) -> dict[str, Any]:
        full_streamed_text = ""
        chunk_count = 0
        tool_calls_aggregated = []
        interrupted_by_event = False
        finish_reason_override = None

        logger.info(
            f"Beginning to receive '{self.api_endpoint_style}' stream data for"
            f" request type '{request_type}'..."
        )
        try:
            async for line_bytes in response.content:
                if interruption_event and interruption_event.is_set():
                    logger.info(f"'{self.api_endpoint_style}' streaming interrupted by event.")
                    interrupted_by_event = True
                    finish_reason_override = "INTERRUPTED"
                    break

                line = line_bytes.decode("utf-8").strip()
                if not line:
                    continue

                current_chunk_text: str | None = None
                if line.startswith("data:"):
                    data_json_str = line[len("data:") :].strip()
                    if not data_json_str:
                        continue

                    if self.api_endpoint_style == "openai" and data_json_str == "[DONE]":
                        logger.info("OpenAI-style stream [DONE] signal.")
                        break

                    try:
                        data_chunk = json.loads(data_json_str)
                        chunk_count += 1

                        if self.api_endpoint_style == "google":
                            candidate = data_chunk.get("candidates", [{}])[0]
                            if candidate:
                                content = candidate.get("content", {})
                                if content:
                                    parts = content.get("parts", [])
                                    if parts:
                                        for part in parts:
                                            if "text" in part:
                                                current_chunk_text = part["text"]
                                                if current_chunk_text is None:
                                                    current_chunk_text = ""
                                if candidate.get("finishReason") and not finish_reason_override:
                                    finish_reason_override = candidate.get("finishReason")

                        elif self.api_endpoint_style == "openai" and data_chunk.get("choices"):
                            choice = data_chunk["choices"][0]
                            delta = choice.get("delta", {})
                            if "content" in delta:
                                current_chunk_text = delta["content"]
                                if current_chunk_text is None:
                                    current_chunk_text = ""

                            if choice.get("finish_reason") and not finish_reason_override:
                                finish_reason_override = choice.get("finish_reason")

                            if "tool_calls" in delta:
                                for tc_delta in delta["tool_calls"]:
                                    index = tc_delta.get("index", 0)
                                    if index >= len(tool_calls_aggregated):
                                        tool_calls_aggregated.extend(
                                            [{}] * (index - len(tool_calls_aggregated) + 1)
                                        )
                                    if "id" in tc_delta:
                                        tool_calls_aggregated[index]["id"] = tc_delta["id"]
                                    if "type" in tc_delta:
                                        tool_calls_aggregated[index]["type"] = tc_delta["type"]
                                    if "function" in tc_delta:
                                        if "function" not in tool_calls_aggregated[index]:
                                            tool_calls_aggregated[index]["function"] = {}
                                        if "name" in tc_delta["function"]:
                                            tool_calls_aggregated[index]["function"]["name"] = (
                                                tc_delta["function"]["name"]
                                            )
                                        if "arguments" in tc_delta["function"]:
                                            tool_calls_aggregated[index]["function"][
                                                "arguments"
                                            ] = (
                                                tool_calls_aggregated[index]["function"].get(
                                                    "arguments", ""
                                                )
                                                + tc_delta["function"]["arguments"]
                                            )
                    except json.JSONDecodeError:
                        logger.warning(f"Unable to parse stream JSON: {data_json_str}")

                elif line and self.api_endpoint_style == "google":
                    logger.debug(f"Non-data Google stream event: {line}")

                if current_chunk_text is not None:
                    if self.stream_chunk_delay_seconds > 0:
                        await asyncio.sleep(self.stream_chunk_delay_seconds)
                    print(current_chunk_text, end="", flush=True)
                    full_streamed_text += current_chunk_text

            if not interrupted_by_event:
                print()
                logger.info(
                    f"'{self.api_endpoint_style}' streaming complete ({chunk_count} data chunks)."
                )
            else:
                print(" [STREAM INTERRUPTED]")

            result = {
                "streamed_text_summary": (
                    f"Stream {'interrupted' if interrupted_by_event else 'completed'}. ",
                    f"Chunks: {chunk_count}.",
                ),
                "full_text": full_streamed_text,
                "raw_response_type": "STREAMED",
                "interrupted": interrupted_by_event,
                "finish_reason": finish_reason_override
                or ("INTERRUPTED" if interrupted_by_event else "UNKNOWN"),
            }
            if tool_calls_aggregated:
                for tc in tool_calls_aggregated:
                    if (
                        "function" in tc
                        and "arguments" in tc["function"]
                        and isinstance(tc["function"]["arguments"], str)
                        and not tc["function"]["arguments"].strip()
                    ):
                        tc["function"]["arguments"] = "{}"
                result["tool_calls"] = tool_calls_aggregated
            return result

        except aiohttp.ClientPayloadError as e:
            logger.error(f"Stream ClientPayloadError: {e}")
            raise NetworkError(f"Stream payload error: {e}", original_exception=e) from e
        except aiohttp.ClientConnectionError as e:
            logger.error(f"Stream ClientConnectionError: {e}")
            raise NetworkError(f"Stream connection error: {e}", original_exception=e) from e
        except Exception as e_stream:
            logger.exception(f"Unknown stream processing error: {e_stream}")
            raise APIResponseError(f"Unknown stream processing error: {e_stream}") from e_stream

    def _parse_non_streaming_response_for_style(
        self,
        response_json: dict[str, Any],
        request_type: str,
    ) -> dict[str, Any]:
        parsed_result = {
            "text": None,
            "tool_calls": None,
            "function_call": None,
            "embedding": None,
            "raw_response": response_json,
            "usage": None,
            "interrupted": False,
            "finish_reason": None,
            "blocked_by_safety": False,
        }
        if self.api_endpoint_style == "google":
            if request_type == "embedding":
                if "embedding" in response_json and "value" in response_json["embedding"]:
                    parsed_result["embedding"] = response_json["embedding"]["value"]
            else:
                candidate = response_json.get("candidates", [{}])[0]
                if candidate:
                    content_parts = candidate.get("content", {}).get("parts", [])
                    text_parts = [part["text"] for part in content_parts if "text" in part]
                    if text_parts:
                        parsed_result["text"] = "".join(text_parts)

                    for part in content_parts:
                        if "functionCall" in part and request_type == "tool_call":
                            parsed_result["function_call"] = part["functionCall"]
                            break

                    parsed_result["finish_reason"] = candidate.get("finishReason")
                    if (
                        not parsed_result["text"]
                        and not parsed_result["function_call"]
                        and candidate.get("finishReason") == "SAFETY"
                    ):
                        parsed_result["text"] = "[内容因安全原因被过滤]"
                        parsed_result["blocked_by_safety"] = True
                if "usageMetadata" in response_json:
                    parsed_result["usage"] = response_json["usageMetadata"]

        elif self.api_endpoint_style == "openai":
            if request_type == "embedding":
                if (
                    "data" in response_json
                    and response_json["data"]
                    and "embedding" in response_json["data"][0]
                ):
                    parsed_result["embedding"] = response_json["data"][0]["embedding"]
            else:
                choice = response_json.get("choices", [{}])[0]
                if choice:
                    message = choice.get("message", {})
                    parsed_result["text"] = message.get("content")
                    if message.get("tool_calls") and request_type == "tool_call":
                        parsed_result["tool_calls"] = message["tool_calls"]
                    parsed_result["finish_reason"] = choice.get("finish_reason")
            if "usage" in response_json:
                parsed_result["usage"] = response_json["usage"]
        else:
            raise NotImplementedError(
                f"Non-streaming parsing for {self.api_endpoint_style} not implemented."
            )
        return parsed_result

    async def _make_api_call_attempt(
        self,
        session: aiohttp.ClientSession,
        url_path: str,
        api_key: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        is_streaming: bool,
        request_type: str,
        interruption_event: asyncio.Event | None = None,
    ) -> dict[str, Any]:
        full_request_url = f"{self.base_url}{url_path}"
        request_params = {}
        final_headers = headers.copy()

        if self.api_endpoint_style == "google":
            request_params["key"] = api_key
        elif self.api_endpoint_style == "openai":
            final_headers["Authorization"] = f"Bearer {api_key}"

        loggable_headers = {
            k: (v if k.lower() != "authorization" else "Bearer ***")
            for k, v in final_headers.items()
        }
        logger.debug(
            f"--- HTTP Request (Style: {self.api_endpoint_style}, Type: {request_type}) ---"
        )
        logger.debug(
            f"URL: {full_request_url}, Params: {request_params}, "
            f"Headers: {loggable_headers}, Proxy: {self.proxy_url or 'No'}"
        )

        try:
            prepared_data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        except TypeError as e:
            logger.error(f"Payload序列化为JSON时失败: {e}", exc_info=True)
            logger.critical(f"失败的Payload结构: {payload}")
            raise LLMClientError(f"Payload序列化失败: {e}") from e

        http_response: aiohttp.ClientResponse | None = None
        try:
            http_response = await session.post(
                full_request_url,
                headers=final_headers,
                data=prepared_data,
                params=request_params,
                proxy=self.proxy_url,
                timeout=120,
            )

            status_code = http_response.status
            logger.debug(f"Request sent. Actual URL: {http_response.url}. Status: {status_code}")

            if 200 <= status_code < 300:
                if is_streaming:
                    return await self._handle_streaming_response_for_style(
                        http_response,
                        request_type,
                        interruption_event,
                    )
                else:
                    response_json = await http_response.json()
                    return self._parse_non_streaming_response_for_style(response_json, request_type)
            else:
                response_text = await http_response.text()
                key_info = (
                    f"...{api_key[-4:]}" if api_key and len(api_key) > 4 else "INVALID_KEY_FORMAT"
                )
                if status_code == 413:
                    raise PayloadTooLargeError("请求体过大 (413)", status_code, response_text)
                if status_code == 400:
                    logger.error(
                        f"请求无效或参数错误 (400) - Key {key_info}. "
                        f"Response: {response_text[:500]}"
                    )
                    # 抛出 APIResponseError，这样就不会触发外层逻辑将密钥禁用。
                    raise APIResponseError(
                        f"请求无效或参数错误 (400) - Key {key_info}",
                        status_code,
                        response_text,
                    )
                if status_code == 401:
                    raise PermissionDeniedError(
                        f"认证失败 (401) - Key {key_info}",
                        status_code,
                        response_text,
                        key_identifier=api_key,
                    )
                if status_code == 403:
                    raise PermissionDeniedError(
                        f"权限被拒绝 (403) - Key {key_info}",
                        status_code,
                        response_text,
                        key_identifier=api_key,
                    )
                if status_code == 429:
                    raise RateLimitError(
                        f"速率限制超出 (429) - Key {key_info}",
                        status_code,
                        response_text,
                        key_identifier=api_key,
                    )
                raise APIResponseError(
                    f"API错误 {status_code} - Key {key_info}", status_code, response_text
                )

        except (RateLimitError, PermissionDeniedError, PayloadTooLargeError, APIResponseError):
            raise
        except aiohttp.ClientProxyConnectionError as e:
            logger.error(f"代理连接错误: {e}")
            raise NetworkError(f"代理连接错误: {e}", original_exception=e) from e
        except (
            aiohttp.ClientConnectorError,
            aiohttp.ServerDisconnectedError,
            aiohttp.ClientOSError,
        ) as e:
            logger.error(f"网络连接错误: {e}")
            raise NetworkError(f"网络连接错误: {e}", original_exception=e) from e
        except TimeoutError as e:
            logger.error("请求超时")
            raise NetworkError("请求超时", original_exception=e) from e
        except json.JSONDecodeError as e:
            response_text_for_error = "N/A"
            if http_response:
                with contextlib.suppress(Exception):
                    response_text_for_error = await http_response.text(errors="ignore")
                    pass
            logger.error(f"JSON解码错误: {e}. Response text: {response_text_for_error[:200]}")
            raise APIResponseError(
                f"无法解析API响应为JSON: {e}", response_text=response_text_for_error
            ) from e
        except aiohttp.ClientError as e:
            logger.exception(f"AIOHTTP客户端调用时发生意外错误: {e}")
            raise NetworkError(f"AIOHTTP客户端调用时发生意外错误: {e}", original_exception=e) from e
        except Exception as e:
            logger.exception("API调用时发生完全未预料的错误")
            raise LLMClientError(f"API调用时发生完全未预料的错误: {e}") from e
        finally:
            if http_response:
                http_response.release()

    async def _execute_request_with_retries(
        self,
        request_type: str,
        is_streaming: bool,
        prompt: str | None = None,
        system_prompt: str | None = None,
        enable_multimodal: bool = False,
        image_inputs: list[str] | None = None,
        image_mime_type_override: str | None = None,
        generation_params_override: GenerationParams | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict | None = None,
        text_to_embed: str | None = None,
        max_retries: int = 3,
        interruption_event: asyncio.Event | None = None,
        enable_google_search: bool = False,
        enable_url_context: bool = False,
    ) -> dict[str, Any]:
        async with aiohttp.ClientSession() as session:
            all_initial_keys = self.api_keys_config[:]
            last_exception: Exception | None = None

            current_processed_images: list[dict[str, str]] = []
            if (
                request_type == "vision" or (request_type == "tool_call" and enable_multimodal)
            ) and image_inputs:
                current_processed_images = await self._process_media_inputs(
                    image_inputs, image_mime_type_override
                )

            if (
                request_type != "embedding"
                and not prompt
                and not current_processed_images
                and not (isinstance(prompt, str) and not prompt.strip())
            ):
                raise ValueError("提示和图像不能都为空 (对于非嵌入请求)。")
            if request_type == "embedding" and not text_to_embed:
                raise ValueError("text_to_embed 不能为空 (对于嵌入请求)。")

            current_generation_config: GenerationParams = self.default_generation_config.copy()
            if generation_params_override:
                current_generation_config.update(generation_params_override)

            images_have_been_compression_attempted_this_call = False
            allowed_temp_disable_resets = max(0, max_retries - 1)
            num_temp_disable_resets_done = 0

            for attempt_pass in range(max_retries + 1):
                if interruption_event and interruption_event.is_set():
                    logger.info(f"请求执行在第 {attempt_pass + 1} 轮尝试前被中断信号中止。")
                    message = (
                        "Task was interrupted before an API call could be made in this attempt."
                    )
                    return {
                        "error": False,
                        "interrupted": True,
                        "full_text": "",
                        "streamed_text_summary": "Task interrupted before API call.",
                        "finish_reason": "INTERRUPTED_BEFORE_CALL",
                        "message": message,
                    }

                current_time = time.time()
                keys_to_reactivate = [
                    k
                    for k, expiry_ts in self._temporarily_disabled_keys_429.items()
                    if expiry_ts <= current_time
                ]
                for k_active in keys_to_reactivate:
                    del self._temporarily_disabled_keys_429[k_active]
                    logger.info(f"密钥 ...{k_active[-4:]} 的429临时禁用已到期并解除。")

                all_abandoned_permanently = self.abandoned_keys_config.union(
                    self._abandoned_keys_runtime
                )

                available_keys_this_pass = [
                    key
                    for key in all_initial_keys
                    if key not in all_abandoned_permanently
                    and key not in self._temporarily_disabled_keys_429
                ]

                if not available_keys_this_pass:
                    if (
                        self._temporarily_disabled_keys_429
                        and num_temp_disable_resets_done < allowed_temp_disable_resets
                    ):
                        logger.warning(
                            f"在第 {attempt_pass + 1} 次尝试轮中， 所有可用密钥"
                            "当前均处于429临时禁用状态。将清除临时禁用列表并重试 (已执行重置: "
                            f"{num_temp_disable_resets_done}/{allowed_temp_disable_resets})。"
                        )
                        self._temporarily_disabled_keys_429.clear()
                        num_temp_disable_resets_done += 1
                        available_keys_this_pass = [
                            key for key in all_initial_keys if key not in all_abandoned_permanently
                        ]
                        if not available_keys_this_pass:
                            logger.error("清除临时禁用列表后，仍无任何可用API密钥。")
                            break
                    else:
                        logger.error(
                            f"在第 {attempt_pass + 1} 次尝试轮中，已无任何可用API密钥"
                            f"（包括永久禁用和无法再重置的临时禁用）。"
                        )
                        break

                random.shuffle(available_keys_this_pass)
                logger.info(
                    f"开始第 {attempt_pass + 1}/{max_retries + 1} 次请求尝试轮。 "
                    f"本轮可用密钥数 (排除永久和临时禁用): {len(available_keys_this_pass)}"
                )

                current_pass_last_exception: Exception | None = None

                for key_idx, current_key in enumerate(available_keys_this_pass):
                    key_display = (
                        f"...{current_key[-4:]}"
                        if current_key and len(current_key) > 4
                        else "INVALID_KEY"
                    )
                    try:
                        url_path, headers, payload = self._prepare_request_data_for_style(
                            request_type=request_type,
                            prompt=prompt,
                            system_prompt=system_prompt,
                            processed_images=current_processed_images,
                            is_streaming=is_streaming,
                            final_generation_config=current_generation_config,
                            tools=tools,
                            tool_choice=tool_choice,
                            text_to_embed=text_to_embed,
                            enable_google_search=enable_google_search,
                            enable_url_context=enable_url_context,
                        )
                        logger.info(
                            f"尝试轮 {attempt_pass + 1}/{max_retries + 1}, "
                            f"密钥 {key_idx + 1}/{len(available_keys_this_pass)} "
                            f"(ID: {key_display}): "
                            f"类型: {request_type}, {'流式' if is_streaming else '非流式'}, "
                            f"模型: {self.model_name}"
                        )
                        if system_prompt and request_type != "embedding":
                            logger.info(
                                f"  使用 System Prompt (前50字符): {system_prompt[:50]}"
                                f"{'...' if len(system_prompt) > 50 else ''}"
                            )

                        result = await self._make_api_call_attempt(
                            session,
                            url_path,
                            current_key,
                            headers,
                            payload,
                            is_streaming,
                            request_type,
                            interruption_event,
                        )

                        # 处理非流式请求的结果
                        if config.test_function.fallback_model_name != "":
                            is_successful_call = not result.get("error") and not result.get(
                                "interrupted"
                            )
                            is_non_streaming_text_request = (
                                not is_streaming and request_type != "embedding"
                            )
                            is_text_content_none = result.get("text") is None

                            if (
                                is_successful_call
                                and is_non_streaming_text_request
                                and is_text_content_none
                            ):
                                fallback_model_name = (
                                    config.test_function.fallback_model_name
                                )  # 从配置中获取备用模型名称
                                logger.warning(
                                    f"密钥 {key_display} 的请求成功，但返回的 text 字段为 None。"
                                    f"将使用备用模型 '{fallback_model_name}' 尝试一次。"
                                )

                                if self.model_name == fallback_model_name:
                                    logger.error(
                                        "当前模型已经是备用模型，但仍然返回空文本。为避免无限循环，将不再尝试。"
                                    )
                                    return result

                                url_path_fallback, headers_fallback, payload_fallback = (
                                    self._prepare_request_data_for_style(
                                        request_type=request_type,
                                        prompt=prompt,
                                        system_prompt=system_prompt,
                                        processed_images=current_processed_images,
                                        is_streaming=is_streaming,
                                        final_generation_config=current_generation_config,
                                        tools=tools,
                                        tool_choice=tool_choice,
                                        text_to_embed=text_to_embed,
                                        model_name_override=fallback_model_name,
                                    )
                                )

                                logger.info(
                                    f"正在使用备用模型 '{fallback_model_name}' 进行单次重试..."
                                )
                                try:
                                    fallback_result = await self._make_api_call_attempt(
                                        session,
                                        url_path_fallback,
                                        current_key,
                                        headers_fallback,
                                        payload_fallback,
                                        is_streaming,
                                        request_type,
                                        interruption_event,
                                    )
                                    logger.info("备用模型调用完成。")
                                    return fallback_result
                                except Exception as e_fallback:
                                    logger.error(f"备用模型调用失败: {e_fallback}", exc_info=True)
                                    return result
                            else:
                                if result.get("interrupted"):
                                    logger.info(
                                        f"API调用在密钥 {key_display} 尝试期间被中断信号中止。"
                                        f"将直接返回中断结果。"
                                    )
                                return result

                        # 尝试修复无返回导致响应无处理状况
                        if result.get("interrupted"):
                            logger.info(
                                f"API调用在密钥 {key_display} 尝试期间被中断信号中止。"
                                f"将直接返回中断结果。"
                            )
                            return result
                        return result

                    except PermissionDeniedError as e_perm:
                        logger.error(
                            f"密钥 {key_display} 遇到权限拒绝 ({e_perm.status_code}): "
                            f"{e_perm!s}. 将被永久标记为已弃用。"
                        )
                        if e_perm.key_identifier:
                            self._abandoned_keys_runtime.add(e_perm.key_identifier)
                            if e_perm.key_identifier in self._temporarily_disabled_keys_429:
                                del self._temporarily_disabled_keys_429[e_perm.key_identifier]
                        current_pass_last_exception = e_perm

                    except RateLimitError as e_rate:
                        logger.warning(
                            f"密钥 {key_display} 达到速率限制 ({e_rate.status_code}). "
                            f"将被临时禁用 {self.rate_limit_disable_duration_seconds // 60} 分钟。"
                        )
                        if e_rate.key_identifier and self.rate_limit_disable_duration_seconds > 0:
                            disable_until_ts = (
                                time.time() + self.rate_limit_disable_duration_seconds
                            )
                            self._temporarily_disabled_keys_429[e_rate.key_identifier] = (
                                disable_until_ts
                            )
                            ban_time = time.strftime(
                                "%Y-%m-%d %H:%M:%S", time.localtime(disable_until_ts)
                            )
                            logger.info(f"密钥 {key_display} 已被临时禁用直到 {ban_time}.")
                        current_pass_last_exception = e_rate

                    except PayloadTooLargeError as e_payload:
                        current_pass_last_exception = e_payload
                        if (
                            (
                                request_type == "vision"
                                or (request_type == "tool_call" and enable_multimodal)
                            )
                            and current_processed_images
                            and not images_have_been_compression_attempted_this_call
                            and self.enable_image_compression
                        ):
                            logger.info(
                                "检测到 PayloadTooLargeError，尝试对当前图像集进行响应式压缩..."
                            )
                            temp_compressed_images_data = []
                            any_image_compressed_reactively = False
                            for img_data_val in current_processed_images:
                                compressed_b64, new_mime = await self._compress_base64_image(
                                    img_data_val["b64_data"], img_data_val["mime_type"]
                                )
                                if compressed_b64 != img_data_val["b64_data"]:
                                    any_image_compressed_reactively = True
                                temp_compressed_images_data.append(
                                    {"b64_data": compressed_b64, "mime_type": new_mime}
                                )

                            if any_image_compressed_reactively:
                                current_processed_images = temp_compressed_images_data
                                images_have_been_compression_attempted_this_call = True
                                logger.info(
                                    "响应式图像压缩已应用。将继续使用（可能）压缩后的图像尝试下一个（或相同的，如果适用）密钥。"
                                )
                            else:
                                logger.info("响应式图像压缩未改变图像数据或未启用。")
                        else:
                            logger.warning("遇到PayloadTooLargeError，但无法或不再尝试图像压缩。")

                    except (NetworkError, APIResponseError, LLMClientError) as e_general:
                        logger.warning(
                            f"尝试轮 {attempt_pass + 1} (密钥 {key_display}) 失败，"
                            f"错误类型 {type(e_general).__name__}: {e_general!s}"
                        )
                        current_pass_last_exception = e_general

                    except Exception as e_unexpected:
                        logger.error(
                            f"在尝试轮 {attempt_pass + 1} (密钥 {key_display}) "
                            f"期间发生意外错误: {e_unexpected!s}",
                            exc_info=True,
                        )
                        current_pass_last_exception = e_unexpected

                    if key_idx < len(available_keys_this_pass) - 1:
                        logger.warning(
                            f"密钥 {key_display} 尝试失败。将尝试本轮中的下一个可用密钥。"
                        )
                    else:
                        logger.warning(f"密钥 {key_display} (本轮最后一个) 尝试失败。")

                if current_pass_last_exception:
                    last_exception = current_pass_last_exception

                if attempt_pass < max_retries:
                    wait_duration = INITIAL_RETRY_PASS_DELAY_SECONDS * (2**attempt_pass)
                    last_error = (
                        type(current_pass_last_exception).__name__
                        if current_pass_last_exception
                        else "未知或无可用密钥导致失败"
                    )
                    logger.warning(
                        f"第 {attempt_pass + 1} 次请求尝试轮未成功。"
                        f"等待 {wait_duration:.2f} 秒后进行下一次尝试轮 (如果适用)。"
                        f"本轮最后遇到的错误: {last_error}"
                    )
                    await asyncio.sleep(wait_duration)
                elif attempt_pass == max_retries:
                    last_error = (
                        type(last_exception).__name__
                        if last_exception
                        else "未知或无可用密钥导致失败"
                    )
                    logger.error(
                        f"已达到最大请求尝试轮数 ({max_retries + 1})，且最后一轮未成功。"
                        f"最终错误: {last_error}"
                    )

            if last_exception:
                if isinstance(
                    last_exception, RateLimitError | PermissionDeniedError | PayloadTooLargeError
                ):
                    return {
                        "error": True,
                        "type": type(last_exception).__name__,
                        "status_code": getattr(last_exception, "status_code", None),
                        "message": f"所有API请求尝试轮均失败。最终错误: {last_exception!s}",
                        "details": getattr(last_exception, "response_text", str(last_exception)),
                    }
                raise last_exception

            raise LLMClientError(
                "所有API请求尝试轮均失败，或未能找到可用API密钥执行请求。"
                "最后记录的异常 (如果存在): "
                f"{type(last_exception).__name__ if last_exception else '无'}"
            )

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
        use_url_context: bool = False,
        **kwargs: Unpack[GenerationParams],
    ) -> dict[str, Any]:
        """发送请求到 LLM API 并返回响应.

        Args:
            prompt (str): 用户输入的提示文本.
            system_prompt (str | None): 系统提示文本.
            is_stream (bool): 是否使用流式响应.
            is_multimodal (bool): 是否启用多模态支持（默认为 False）.
            image_inputs (list[str] | None): 图像输入列表（base64编码的字符串）.
            temp (float | None): 温度参数（可选）.
            max_tokens (int | None): 最大输出令牌数（可选）.
            tools (list[dict[str, Any]] | None): 工具列表（如果适用）.
            tool_choice (str | dict[str, Any] | None): 工具选择（如果适用）.
            image_mime_type_override (str | None): 图像 MIME 类型覆盖（如果适用）.
            max_retries (int): 最大重试次数（默认为3）.
            interruption_event (asyncio.Event | None): 中断事件（如果需要中断支持）.
            use_google_search (bool): 是否启用 Google 搜索功能（默认为 False）.
            use_url_context (bool): 是否启用 URL 上下文功能（默认为 False）.
            **kwargs: 其他生成参数.

        Returns:
            dict[str, Any]: 生成的响应结果.
        """
        request_type = "chat"
        if tools:
            request_type = "tool_call"
        elif is_multimodal and image_inputs:
            request_type = "vision"

        generation_params_override: GenerationParams = kwargs.copy()
        if temp is not None:
            generation_params_override["temperature"] = temp
        if max_tokens is not None:
            generation_params_override["maxOutputTokens"] = max_tokens

        actual_enable_multimodal = is_multimodal and bool(image_inputs)

        return await self._execute_request_with_retries(
            request_type=request_type,
            is_streaming=is_stream,
            prompt=prompt,
            system_prompt=system_prompt,
            enable_multimodal=actual_enable_multimodal,
            image_inputs=image_inputs,
            image_mime_type_override=image_mime_type_override,
            generation_params_override=generation_params_override,
            tools=tools,
            tool_choice=tool_choice,
            max_retries=max_retries,
            interruption_event=interruption_event,
            enable_google_search=use_google_search,
            enable_url_context=use_url_context,
        )

    async def generate_text_completion(
        self,
        prompt: str,
        is_stream: bool,
        system_prompt: str | None = None,
        temp: float | None = None,
        max_tokens: int | None = None,
        max_retries: int = 3,
        interruption_event: asyncio.Event | None = None,
        **kwargs: Unpack[GenerationParams],
    ) -> dict[str, Any]:
        """生成文本补全响应.

        Args:
            prompt (str): 用户输入的提示文本.
            is_stream (bool): 是否使用流式响应.
            system_prompt (str | None): 系统提示文本.
            temp (float | None): 温度参数（可选）.
            max_tokens (int | None): 最大输出令牌数（可选）.
            max_retries (int): 最大重试次数（默认为3）.
            interruption_event (asyncio.Event | None): 中断事件（如果需要中断支持）.
            **kwargs: 其他生成参数.

        Returns:
            dict[str, Any]: 生成的响应结果.
        """
        logger.info(f"generate_text_completion: {'流式' if is_stream else '非流式'}")
        if system_prompt:
            logger.info(
                f"  generate_text_completion 收到 System Prompt (前50字符): {system_prompt[:50]}"
                f"{'...' if len(system_prompt) > 50 else ''}"
            )
        gen_params = kwargs.copy()
        return await self.make_request(
            prompt=prompt,
            system_prompt=system_prompt,
            is_stream=is_stream,
            is_multimodal=False,
            temp=temp,
            max_tokens=max_tokens,
            max_retries=max_retries,
            interruption_event=interruption_event,
            **gen_params,
        )

    async def generate_vision_completion(
        self,
        prompt: str,
        image_inputs: list[str],
        is_stream: bool,
        system_prompt: str | None = None,
        image_mime_type_override: str | None = None,
        temp: float | None = None,
        max_tokens: int | None = None,
        max_retries: int = 3,
        interruption_event: asyncio.Event | None = None,
        **kwargs: Unpack[GenerationParams],
    ) -> dict[str, Any]:
        """生成视觉补全响应.

        Args:
            prompt (str): 用户输入的提示文本.
            image_inputs (list[str]): 图像输入列表（base64编码的字符串）.
            is_stream (bool): 是否使用流式响应.
            system_prompt (str | None): 系统提示文本.
            image_mime_type_override (str | None): 图像 MIME 类型覆盖（如果适用）.
            temp (float | None): 温度参数（可选）.
            max_tokens (int | None): 最大输出令牌数（可选）.
            max_retries (int): 最大重试次数（默认为3）.
            interruption_event (asyncio.Event | None): 中断事件（如果需要中断支持）.
            **kwargs: 其他生成参数.

        Returns:
            dict[str, Any]: 生成的响应结果.
        """
        logger.info(
            f"generate_vision_completion: {'流式' if is_stream else '非流式'}, "
            f"图像数量: {len(image_inputs)}"
        )
        if system_prompt:
            logger.info(
                f"  generate_vision_completion 收到 System Prompt (前50字符): {system_prompt[:50]}"
                f"{'...' if len(system_prompt) > 50 else ''}"
            )

        if not image_inputs:
            raise ValueError("视觉补全请求必须包含图像输入。")
        gen_params = kwargs.copy()
        return await self.make_request(
            prompt=prompt,
            system_prompt=system_prompt,
            is_stream=is_stream,
            is_multimodal=True,
            image_inputs=image_inputs,
            image_mime_type_override=image_mime_type_override,
            temp=temp,
            max_tokens=max_tokens,
            max_retries=max_retries,
            interruption_event=interruption_event,
            **gen_params,
        )

    async def generate_with_tools(
        self,
        prompt: str,
        tools: list[dict[str, Any]],
        is_stream: bool,
        system_prompt: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        image_inputs: list[str] | None = None,
        image_mime_type_override: str | None = None,
        temp: float | None = None,
        max_tokens: int | None = None,
        max_retries: int = 3,
        interruption_event: asyncio.Event | None = None,
        **kwargs: Unpack[GenerationParams],
    ) -> dict[str, Any]:
        """使用工具生成文本或调用工具.

        Args:
            prompt (str): 用户输入的提示文本.
            tools (list[dict[str, Any]]): 工具定义列表.
            is_stream (bool): 是否使用流式响应.
            system_prompt (str | None): 系统提示文本.
            tool_choice (str | dict[str, Any] | None): 工具选择配置.
            image_inputs (list[str] | None): 图像输入列表（如果适用）.
            image_mime_type_override (str | None): 图像 MIME 类型覆盖（如果适用）.
            temp (float | None): 温度参数（可选）.
            max_tokens (int | None): 最大输出令牌数（可选）.
            max_retries (int): 最大重试次数（默认为3）.
            interruption_event (asyncio.Event | None): 中断事件（如果需要中断支持）.
            **kwargs: 其他生成参数.

        Returns:
            dict[str, Any]: 生成的响应结果.
        """
        logger.info(
            f"generate_with_tools: {'流式' if is_stream else '非流式'}, 工具数量: {len(tools)}"
        )
        if system_prompt:
            logger.info(
                f"  generate_with_tools 收到 System Prompt (前50字符): {system_prompt[:50]}"
                f"{'...' if len(system_prompt) > 50 else ''}"
            )

        if not tools:
            raise ValueError("工具调用请求必须包含工具定义。")
        gen_params = kwargs.copy()
        return await self.make_request(
            prompt=prompt,
            system_prompt=system_prompt,
            is_stream=is_stream,
            is_multimodal=bool(image_inputs),
            image_inputs=image_inputs,
            tools=tools,
            tool_choice=tool_choice,
            image_mime_type_override=image_mime_type_override,
            temp=temp,
            max_tokens=max_tokens,
            max_retries=max_retries,
            interruption_event=interruption_event,
            **gen_params,
        )

    async def get_embedding(
        self,
        text_to_embed: str,
        generation_params_override: GenerationParams | None = None,
        max_retries: int = 3,
    ) -> dict[str, Any]:
        """获取文本嵌入表示.

        Args:
            text_to_embed (str): 要嵌入的文本.
            generation_params_override (GenerationParams | None): 可选的生成参数覆盖.
            max_retries (int): 最大重试次数（默认为3）.

        Returns:
            dict[str, Any]: 包含嵌入结果的字典.
        """
        logger.info(f"嵌入请求: 文本长度 {len(text_to_embed)}")
        if not text_to_embed:
            raise ValueError("用于嵌入的文本不能为空。")
        return await self._execute_request_with_retries(
            request_type="embedding",
            is_streaming=False,
            text_to_embed=text_to_embed,
            system_prompt=None,
            generation_params_override=generation_params_override,
            max_retries=max_retries,
        )

    async def close(self) -> None:
        """优雅地关闭内部持有的 aiohttp.ClientSession."""
        if self._session and not self._session.closed:
            await self._session.close()
            logger.info(f"LLMClient for provider '{self.provider}' session closed.")
            self._session = None
