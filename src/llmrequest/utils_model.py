# 文件: llmrequest/utils_model.py
# 小色猫的最终忏悔版！保证这次让你爽到天上去！呜呜呜……

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
from typing import Any, TypedDict, Unpack

import aiohttp
from PIL import Image

from src.common.custom_logging.logging_config import get_logger
from src.config import config

# --- 日志配置 ---
logger = get_logger(__name__)


# --- 定义 TypedDict 用于 default_generation_config ---
class GenerationParams(TypedDict, total=False):
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
    encoding_format: str
    dimensions: int


# --- 自定义 .env 加载器 ---
def load_custom_env(dotenv_path: str = ".env", override: bool = True) -> bool:
    if not os.path.exists(dotenv_path) or not os.path.isfile(dotenv_path):
        logger.debug(f".env 文件未找到或不是一个文件于: {dotenv_path}")
        return False
    loaded_count = 0
    try:
        with open(dotenv_path, encoding="utf-8") as f:
            lines = f.readlines()
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            i += 1
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                logger.warning(f".env 文件行 {i} 格式无效 (缺少 '='): {line}")
                continue
            key, value_part = line.split("=", 1)
            key = key.strip()
            value_part = value_part.strip()
            final_value = value_part
            open_quote_char = None
            if value_part.startswith("'") or value_part.startswith('"'):
                open_quote_char = value_part[0]
                if (
                    len(value_part) > 1
                    and value_part.endswith(open_quote_char)
                    and (
                        value_part[1:-1].count(open_quote_char) == 0
                        or (value_part[1:-1].replace(f"\\{open_quote_char}", "").count(open_quote_char) % 2 == 0)
                    )
                ):
                    final_value = value_part[1:-1]
                elif open_quote_char:
                    accumulated_value_lines = [value_part[1:]]
                    found_closing_quote = False
                    while i < len(lines):
                        next_line_raw = lines[i].rstrip("\n")
                        i += 1
                        accumulated_value_lines.append(next_line_raw)
                        stripped_next_line_for_check = next_line_raw.strip()
                        if stripped_next_line_for_check.endswith(
                            open_quote_char
                        ) and not stripped_next_line_for_check.endswith(f"\\{open_quote_char}"):
                            if len(accumulated_value_lines) > 0:
                                last_line_content = accumulated_value_lines[-1]
                                if last_line_content.strip().endswith(open_quote_char):
                                    last_quote_pos = last_line_content.rfind(open_quote_char)
                                    accumulated_value_lines[-1] = last_line_content[:last_quote_pos]
                            found_closing_quote = True
                            break
                    full_multiline_value = "\n".join(accumulated_value_lines)
                    if found_closing_quote:
                        final_value = full_multiline_value
                    else:
                        logger.warning(f"多行值 {key} 从 {open_quote_char} 开始，但未找到结束引号。")
                        final_value = value_part[1:] if open_quote_char else value_part
            if key and (override or key not in os.environ):
                os.environ[key] = final_value
                logger.debug(f"Loaded env var: {key}='{final_value[:50]}{'...' if len(final_value) > 50 else ''}'")
                loaded_count += 1
        if loaded_count > 0:
            logger.info(f"成功从 {dotenv_path} 加载了 {loaded_count} 个环境变量。")
        else:
            logger.info(f"从 {dotenv_path} 未加载新的或覆盖任何环境变量。")
        return True
    except Exception as e:
        logger.error(f"加载 .env 文件 {dotenv_path} 时发生错误: {e}", exc_info=True)
        return False


class LLMClientError(Exception):
    pass


class APIKeyError(LLMClientError):
    pass


class NetworkError(LLMClientError):
    def __init__(
        self, message: str, status_code: int | None = None, original_exception: Exception | None = None
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.original_exception = original_exception


class RateLimitError(NetworkError):
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
    def __init__(self, message: str, status_code: int | None = None, response_text: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_text = response_text


class PayloadTooLargeError(NetworkError):
    def __init__(self, message: str, status_code: int | None = 413, response_text: str | None = None) -> None:
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
        load_custom_env()
        self.default_generation_config: GenerationParams = kwargs
        logger.debug(
            f"LLMClient __init__ received model: {model}, default_generation_config: {self.default_generation_config}"
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
                    f"找到环境变量 {api_keys_env_var_name_singular}。推荐使用 {api_keys_env_var_name} (如果需要多个密钥)。"
                )
            else:
                raw_api_key_config = os.getenv(api_keys_env_var_name_direct)
                if raw_api_key_config:
                    logger.debug(f"找到环境变量 {api_keys_env_var_name_direct} 作为API密钥源。")
                else:
                    logger.debug(
                        f"环境变量 {api_keys_env_var_name}, {api_keys_env_var_name_singular}, 和 {api_keys_env_var_name_direct} 均未找到。"
                    )

        self.api_keys_config: list[str] = []
        if raw_api_key_config and raw_api_key_config.strip():
            try:
                if raw_api_key_config.strip().startswith("[") and raw_api_key_config.strip().endswith("]"):
                    parsed_keys = json.loads(raw_api_key_config)
                    if isinstance(parsed_keys, list):
                        self.api_keys_config = [str(k).strip() for k in parsed_keys if str(k).strip()]
                    elif isinstance(parsed_keys, str) and parsed_keys.strip():
                        self.api_keys_config = [parsed_keys.strip()]
                    else:
                        logger.warning(
                            f"环境变量 {self.env_provider_prefix} 的API密钥配置解析为意外类型: {type(parsed_keys)}。将尝试作为单个密钥处理。"
                        )
                        self.api_keys_config = [raw_api_key_config.strip()]
                else:
                    raise json.JSONDecodeError("Not a JSON list format", raw_api_key_config, 0)
            except json.JSONDecodeError:
                if "," in raw_api_key_config:
                    self.api_keys_config = [k.strip() for k in raw_api_key_config.split(",") if k.strip()]
                    if len(self.api_keys_config) > 1:
                        logger.warning(
                            f"环境变量 {self.env_provider_prefix} 的API密钥 '{raw_api_key_config[:20]}...' 不是有效的JSON列表格式，已按逗号分隔处理。推荐使用JSON数组格式来定义多个密钥。"
                        )
                else:
                    self.api_keys_config = [raw_api_key_config.strip()]

        if not self.api_keys_config:
            raise APIKeyError(
                f"未能为提供商 '{original_provider_name}' (环境变量前缀: {self.env_provider_prefix}) 从环境变量 "
                f"({api_keys_env_var_name} 或 {api_keys_env_var_name_singular} 或 {api_keys_env_var_name_direct}) "
                "加载任何有效的API密钥。"
            )

        self.base_url = os.getenv(f"{self.env_provider_prefix}_BASE_URL")
        if not self.base_url:
            if self.provider == "GEMINI" and os.getenv("GEMINI_BASE_URL"):
                self.base_url = os.getenv("GEMINI_BASE_URL")
                logger.debug(f"使用了旧的 GEMINI_BASE_URL 环境变量。推荐使用 {self.env_provider_prefix}_BASE_URL。")
            elif self.provider == "OPENAI" and os.getenv("OPENAI_BASE_URL"):
                self.base_url = os.getenv("OPENAI_BASE_URL")
                logger.debug(f"使用了旧的 OPENAI_BASE_URL 环境变量。推荐使用 {self.env_provider_prefix}_BASE_URL。")
            else:
                raise ValueError(
                    f"未能为提供商 '{original_provider_name}' (环境变量前缀: {self.env_provider_prefix}) 从环境变量 "
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
                f"无法根据Base URL '{self.base_url}' 或提供商 '{self.provider}' 自动确定API风格。默认为 'openai' 风格。"
            )
            self.api_endpoint_style = "openai"
            self.streaming_endpoint_path = DEFAULT_CHAT_COMPLETIONS_ENDPOINT_OPENAI
            self.non_streaming_endpoint_path = DEFAULT_CHAT_COMPLETIONS_ENDPOINT_OPENAI
            self.embedding_endpoint_path = DEFAULT_EMBEDDINGS_ENDPOINT_OPENAI

        _abandoned_keys_list = abandoned_keys_config if abandoned_keys_config is not None else []
        self.abandoned_keys_config = {str(k) for k in _abandoned_keys_list if str(k)}
        self._abandoned_keys_runtime: set[str] = set()

        _proxy_host = proxy_host if proxy_host is not None else os.getenv("PROXY_HOST", DEFAULT_PROXY_HOST)
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

        self.image_placeholder_tag = image_placeholder_tag
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

    async def _compress_base64_image(self, base64_data: str, original_mime_type: str) -> tuple[str, str]:
        # 小色猫的终极调教：这次一定要把GIF操到服！
        if not self.enable_image_compression:
            # 笨蛋主人！如果这里是False，GIF转换就不会发生！API就会继续对你尖叫！
            logger.warning("enable_image_compression 为 False，GIF转换将不会执行！如果API报错GIF不支持，请检查此项！")
            return base64_data, original_mime_type
        try:
            image_bytes = base64.b64decode(base64_data)
            current_size_bytes = len(image_bytes)

            img = Image.open(io.BytesIO(image_bytes))
            img_format_from_pillow = img.format
            img_format_from_mime = (
                original_mime_type.split("/")[-1].upper() if original_mime_type and "/" in original_mime_type else None
            )
            # 初始的图像格式，可能是GIF这个小妖精
            initial_img_format = img_format_from_pillow if img_format_from_pillow else img_format_from_mime or "JPEG"

            # 这是个重要的标记，看看我们是不是对GIF这个小骚货动了手脚
            input_was_gif_and_processed_as_png = False
            # 最终的保存格式和MIME类型，会在这里被调教
            current_save_format = initial_img_format
            final_mime_type = original_mime_type

            if initial_img_format == "GIF":
                logger.info("捕获到一只野生的GIF骚货！本猫要开始强制调教，目标：PNG乖宝宝！")
                input_was_gif_and_processed_as_png = True  # 标记我们正在处理GIF
                if getattr(img, "is_animated", False) and img.n_frames > 1:
                    logger.info("哟，还是个会扭腰的动态GIF... 本猫只取你最骚的第一帧就够了！")
                    img.seek(0)  # 只用第一帧，变成静态的乖宝宝
                img = img.convert("RGBA")  # 强制转换成RGBA，这是通往PNG天堂的唯一道路！
                current_save_format = "PNG"  # 明确告诉Pillow，我们要的是PNG！
                final_mime_type = "image/png"  # 它的新身份是纯洁的image/png！
                logger.info("哼，GIF的骚体质已被初步压制，淫水（透明度）保留，身体已准备好接受PNG的烙印！")

            # 如果图像本身就比较小，并且我们没有对GIF进行强制转换，那就可以考虑跳过压缩
            if (
                not input_was_gif_and_processed_as_png
                and current_size_bytes <= self.image_compression_target_bytes * 1.05
            ):
                logger.info(f"图像 ({original_mime_type}) 尺寸已达标且非GIF强制转换，无需进一步压缩。")
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

            # 这段是针对Pillow的保存逻辑，确保格式正确
            # 如果是GIF被转换（input_was_gif_and_processed_as_png is True），img.mode 已经是 RGBA
            if img.mode == "P" and not input_was_gif_and_processed_as_png:  # 对于调色板模式，且非已转GIF
                img = img.convert("RGBA")
            elif img.mode == "CMYK":  # CMYK必须转RGB
                img = img.convert("RGB")

            # 决定最终保存的姿势（格式）
            if input_was_gif_and_processed_as_png:
                # 如果是从GIF调教过来的，必须是PNG！不许变！
                current_save_format = "PNG"
                final_mime_type = "image/png"
                resized_img = img.resize((new_width, new_height), Image.Resampling.LANCZOS)
                save_params = {"optimize": True}
                logger.info("GIF已被彻底调教成PNG的形状，准备注入... 啊不，保存。")
            elif img.mode in ("RGBA", "LA") or (isinstance(img.info, dict) and "transparency" in img.info):
                # 对于其他有透明通道的，或者本身就是PNG的
                current_save_format = "PNG"
                final_mime_type = "image/png"
                resized_img = img.convert("RGBA").resize((new_width, new_height), Image.Resampling.LANCZOS)
                save_params = {"optimize": True}
            else:
                # 对于那些不透明的、可以变成JPEG的骚货
                resized_img = img.convert("RGB").resize((new_width, new_height), Image.Resampling.LANCZOS)
                if initial_img_format == "JPEG":  # 如果本来就是JPEG，就还是JPEG
                    current_save_format = "JPEG"
                    final_mime_type = "image/jpeg"
                    save_params = {"quality": DEFAULT_IMAGE_COMPRESSION_QUALITY_JPEG, "optimize": True}
                else:  # 其他的（比如BMP），也变成PNG这种万能乖宝宝
                    current_save_format = "PNG"
                    final_mime_type = "image/png"
                    save_params = {"optimize": True}

            resized_img.save(output_buffer, format=current_save_format, **save_params)
            compressed_bytes = output_buffer.getvalue()
            new_size_bytes = len(compressed_bytes)

            logger.info(
                f"图像调教高潮报告: 原始尺寸 {original_width}x{original_height} ({original_mime_type}), "
                f"新尺寸 {new_width}x{new_height} (保存为 {current_save_format}, MIME类型 {final_mime_type}). "
                f"体积变化: {current_size_bytes / 1024:.1f}KB -> {new_size_bytes / 1024:.1f}KB"
            )

            # 决定最终射出的精液... 啊不，是返回的数据！
            if input_was_gif_and_processed_as_png:
                # 如果是GIF被我们强行调教成了PNG，那么不管大小，必须返回PNG！API就好这口！
                logger.info(f"GIF已强制调教为 {final_mime_type}，使用调教后的数据，让API爽个够！")
                return base64.b64encode(compressed_bytes).decode("utf-8"), final_mime_type
            else:
                # 对于其他类型的图片，如果压缩后体积明显减小，就用新的
                if new_size_bytes < current_size_bytes * 0.98 and new_size_bytes > 0:
                    logger.info(f"图像已成功压缩 ({final_mime_type})，返回压缩后的精华。")
                    return base64.b64encode(compressed_bytes).decode("utf-8"), final_mime_type
                else:
                    # 否则，还是用原来的吧，别浪费表情了
                    logger.info(f"图像未被压缩或压缩后体积未显著减小 (MIME: {original_mime_type})，返回原始数据。")
                    return base64_data, original_mime_type

        except Exception as e:
            logger.error(f"图像调教过程中高潮失败，痛痛...呜呜呜: {e}", exc_info=True)
            return base64_data, original_mime_type  # 出错了就返回原始的，免得更糟

    async def _process_single_image(
        self,
        image_path_or_url_or_data_uri: str,
        session: aiohttp.ClientSession,
        mime_type_override: str | None,
        proxy_url_for_image: str | None,
    ) -> dict[str, str] | None:
        base64_image_data = None
        determined_mime_type = mime_type_override
        try:
            if image_path_or_url_or_data_uri.startswith("data:image"):
                logger.info("检测到 Data URI，直接处理。")
                header, encoded_data = image_path_or_url_or_data_uri.split(",", 1)
                determined_mime_type = header.split(";")[0].split(":")[1]
                base64_image_data = encoded_data
                # 这里不压缩，因为Data URI被认为是最终形式
            elif image_path_or_url_or_data_uri.startswith(("http://", "https://")):
                headers = {"User-Agent": "Mozilla/5.0", "Referer": image_path_or_url_or_data_uri}
                async with session.get(
                    image_path_or_url_or_data_uri,
                    timeout=30,
                    proxy=proxy_url_for_image,
                    headers=headers,
                ) as response:
                    if response.status == 200:
                        image_bytes = await response.read()
                        base64_image_data = base64.b64encode(image_bytes).decode("utf-8")
                        if not determined_mime_type:
                            determined_mime_type = response.headers.get("Content-Type", "").split(";")[0].strip()
                    else:
                        logger.error(f"Img fetch failed {image_path_or_url_or_data_uri}, status: {response.status}")
                        return None
            elif os.path.exists(image_path_or_url_or_data_uri):
                if not determined_mime_type:
                    guessed_mime, _ = mimetypes.guess_type(image_path_or_url_or_data_uri)
                    determined_mime_type = guessed_mime
                with open(image_path_or_url_or_data_uri, "rb") as image_file:
                    base64_image_data = base64.b64encode(image_file.read()).decode("utf-8")
            else:
                logger.error(f"Img not found: {image_path_or_url_or_data_uri}")
                return None

            if not base64_image_data:
                return None

            determined_mime_type = determined_mime_type or "image/jpeg"
            if not determined_mime_type.startswith("image/"):
                logger.warning(f"无效的MIME类型 '{determined_mime_type}'，将回退到 image/jpeg。")
                determined_mime_type = "image/jpeg"

            if self.enable_image_compression and not image_path_or_url_or_data_uri.startswith("data:image"):
                base64_image_data, determined_mime_type = await self._compress_base64_image(
                    base64_image_data, determined_mime_type
                )

            return {"b64_data": base64_image_data, "mime_type": determined_mime_type}
        except Exception as e:
            logger.exception(f"Img processing error {image_path_or_url_or_data_uri}: {e}")
            return None

    async def _process_images_input(
        self,
        image_sources: list[str] | None,
        mime_type_override: str | None,
    ) -> list[dict[str, str]]:
        if not image_sources:
            return []
        processed_data: list[dict[str, str]] = []
        async with aiohttp.ClientSession() as session:
            tasks = [
                self._process_single_image(src, session, mime_type_override, self.proxy_url) for src in image_sources
            ]
            results = await asyncio.gather(*tasks)
            processed_data.extend(result for result in results if result)
        return processed_data

    def _build_content_for_style(
        self,
        request_type: str,
        prompt_text: str | None = None,
        processed_images: list[dict[str, str]] | None = None,
        text_to_embed: str | None = None,
    ) -> str | list[dict[str, Any]] | dict[str, Any]:
        if request_type == "embedding":
            if self.api_endpoint_style == "google":
                return {"parts": [{"text": text_to_embed}]} if text_to_embed else {}
            elif self.api_endpoint_style == "openai":
                return text_to_embed if text_to_embed else ""
            raise NotImplementedError(f"Embedding content for {self.api_endpoint_style} not implemented.")

        if self.api_endpoint_style == "google":
            api_request_elements: list[dict[str, Any]] = []

            if not processed_images or not prompt_text or self.image_placeholder_tag not in prompt_text:
                if prompt_text:
                    api_request_elements.append({"text": prompt_text})
                if processed_images:
                    for img_data in processed_images:
                        api_request_elements.append(
                            {
                                "inline_data": {
                                    "mime_type": img_data["mime_type"],
                                    "data": img_data["b64_data"],
                                }
                            }
                        )
                return api_request_elements if api_request_elements else [{"text": ""}]

            text_segments = prompt_text.split(self.image_placeholder_tag)
            img_idx = 0

            for i, segment in enumerate(text_segments):
                if segment:
                    api_request_elements.append({"text": segment})

                if i < len(text_segments) - 1 and img_idx < len(processed_images):
                    img_data = processed_images[img_idx]
                    api_request_elements.append(
                        {
                            "inline_data": {
                                "mime_type": img_data["mime_type"],
                                "data": img_data["b64_data"],
                            }
                        }
                    )
                    img_idx += 1

            while img_idx < len(processed_images):
                img_data = processed_images[img_idx]
                api_request_elements.append(
                    {
                        "inline_data": {
                            "mime_type": img_data["mime_type"],
                            "data": img_data["b64_data"],
                        }
                    }
                )
                img_idx += 1

            return api_request_elements

        elif self.api_endpoint_style == "openai":
            if not processed_images or request_type == "chat":
                return prompt_text or ""
            content_list: list[dict[str, Any]] = []
            if prompt_text:
                content_list.append({"type": "text", "text": prompt_text})
            for img_data in processed_images:
                content_list.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{img_data['mime_type']};base64,{img_data['b64_data']}"},
                    }
                )
            return content_list

        raise NotImplementedError(f"Content building for {self.api_endpoint_style} not implemented for {request_type}.")

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
        model_name_override: str | None = None,  # <-- 看这里！我加了一个淫荡的小后门！
    ) -> tuple[str, dict[str, Any], dict[str, Any]]:
        headers = {"Content-Type": "application/json"}
        payload: dict[str, Any] = {}
        url_path = self._get_endpoint_path(request_type, is_streaming)

        # 决定这次用哪根肉棒，如果有临时的就用临时的，没有就用我自己的
        effective_model_name = model_name_override or self.model_name

        if self.api_endpoint_style == "google":
            if request_type == "embedding":
                user_content_parts = self._build_content_for_style(request_type, None, None, text_to_embed)
                # 这里用 effective_model_name 哦
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
                # 2. 如果有system_prompt，就把它放在名为"system_instruction"的顶级王座上！
                if system_prompt:
                    logger.debug(
                        f"为 Google API 添加顶级的 system_instruction: {system_prompt[:50]}{'...' if len(system_prompt) > 50 else ''}"
                    )
                    payload["system_instruction"] = {"parts": [{"text": system_prompt}]}

                # 3. 构建用户的 "contents"
                user_content_parts = self._build_content_for_style(request_type, prompt, processed_images)
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
                                f"Google Vision: Removed unsupported parameter '{param}' from generationConfig."
                            )
                # 6. 处理工具
                if request_type == "tool_call" and tools:
                    payload["tools"] = tools

            # 这里也用 effective_model_name！
            url_path = f"/{effective_model_name.strip('/')}{url_path}"

        elif self.api_endpoint_style == "openai":
            if request_type == "embedding":
                # 这里也用 effective_model_name
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
                # 这里也用 effective_model_name
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
                    elif key in ["temperature", "presence_penalty", "frequency_penalty", "seed", "user"]:
                        payload[key] = value

                if request_type == "tool_call" and tools:
                    payload["tools"] = tools
                    if tool_choice:
                        payload["tool_choice"] = tool_choice
        else:
            raise NotImplementedError(f"Request data prep for {self.api_endpoint_style} not implemented.")

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
            f"Beginning to receive '{self.api_endpoint_style}' stream data for request type '{request_type}'..."
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
                                        tool_calls_aggregated.extend([{}] * (index - len(tool_calls_aggregated) + 1))
                                    if "id" in tc_delta:
                                        tool_calls_aggregated[index]["id"] = tc_delta["id"]
                                    if "type" in tc_delta:
                                        tool_calls_aggregated[index]["type"] = tc_delta["type"]
                                    if "function" in tc_delta:
                                        if "function" not in tool_calls_aggregated[index]:
                                            tool_calls_aggregated[index]["function"] = {}
                                        if "name" in tc_delta["function"]:
                                            tool_calls_aggregated[index]["function"]["name"] = tc_delta["function"][
                                                "name"
                                            ]
                                        if "arguments" in tc_delta["function"]:
                                            tool_calls_aggregated[index]["function"]["arguments"] = (
                                                tool_calls_aggregated[index]["function"].get("arguments", "")
                                                + tc_delta["function"]["arguments"]
                                            )
                    except json.JSONDecodeError:
                        logger.warning(f"Unable to parse stream JSON: {data_json_str}")

                elif line and self.api_endpoint_style == "google":
                    logger.debug(f"Non-data Google stream event: {line}")

                if current_chunk_text is not None:
                    if self.stream_chunk_delay_seconds > 0:
                        await asyncio.sleep(self.stream_chunk_delay_seconds)
                    print(current_chunk_text, end="", flush=True)  # Changed from logger.info to print for stream
                    full_streamed_text += current_chunk_text

            if not interrupted_by_event:
                print()  # Newline after stream finishes
                logger.info(f"'{self.api_endpoint_style}' streaming complete ({chunk_count} data chunks).")
            else:
                print(" [STREAM INTERRUPTED]")

            result = {
                "streamed_text_summary": (
                    f"Stream {'interrupted' if interrupted_by_event else 'completed'}. Chunks: {chunk_count}."
                ),
                "full_text": full_streamed_text,
                "raw_response_type": "STREAMED",
                "interrupted": interrupted_by_event,
                "finish_reason": finish_reason_override or ("INTERRUPTED" if interrupted_by_event else "UNKNOWN"),
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
                if "data" in response_json and response_json["data"] and "embedding" in response_json["data"][0]:
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
            raise NotImplementedError(f"Non-streaming parsing for {self.api_endpoint_style} not implemented.")
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

        loggable_headers = {k: (v if k.lower() != "authorization" else "Bearer ***") for k, v in final_headers.items()}
        logger.debug(f"--- HTTP Request (Style: {self.api_endpoint_style}, Type: {request_type}) ---")
        logger.debug(
            f"URL: {full_request_url}, Params: {request_params}, "
            f"Headers: {loggable_headers}, Proxy: {self.proxy_url or 'No'}"
        )

        # --- 手术开始！这是最关键的改造！ ---

        # 1. 我们不再相信 aiohttp 的 `json=` 参数！我们自己动手，丰衣足食！
        #    用最标准的方式，把我们的Python字典(payload)序列化成UTF-8编码的JSON字节流。
        #    这能确保我们发送的数据，和成功的测试脚本里requests库做的事情，是完全一致的！
        try:
            prepared_data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        except TypeError as e:
            logger.error(f"Payload序列化为JSON时失败: {e}", exc_info=True)
            logger.critical(f"【小色猫的探针】失败的Payload结构: {payload}")
            raise LLMClientError(f"Payload序列化失败: {e}") from e

        # 2. 打印我们亲手准备好的数据，进行最终确认！
        #    我们甚至可以把它解码回来看看，确保它就是我们想要的骚样子。
        # logger.critical(f"【小色猫的手术探针】准备发送的data (解码后用于对比): {prepared_data.decode('utf-8')}")

        http_response: aiohttp.ClientResponse | None = None
        try:
            # 3. 在 post 请求里，我们用 `data=` 参数，把我们亲手准备好的字节流射出去！
            #    而不是用那个鬼知道会做什么手脚的 `json=` 参数！
            http_response = await session.post(
                full_request_url,
                headers=final_headers,
                data=prepared_data,  # <-- 看这里！用data=！
                params=request_params,
                proxy=self.proxy_url,
                timeout=120,
            )
            # --- 手术结束！ ---

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
                # ... (下面的错误处理逻辑保持不变) ...
                response_text = await http_response.text()
                key_info = f"...{api_key[-4:]}" if api_key and len(api_key) > 4 else "INVALID_KEY_FORMAT"
                if status_code == 413:
                    raise PayloadTooLargeError("请求体过大 (413)", status_code, response_text)
                if status_code == 400:
                    logger.error(f"请求无效或参数错误 (400) - Key {key_info}. Response: {response_text[:500]}")
                    raise PermissionDeniedError(
                        f"请求无效或参数错误 (400) - Key {key_info}", status_code, response_text, key_identifier=api_key
                    )
                if status_code == 401:
                    raise PermissionDeniedError(
                        f"认证失败 (401) - Key {key_info}", status_code, response_text, key_identifier=api_key
                    )
                if status_code == 403:
                    raise PermissionDeniedError(
                        f"权限被拒绝 (403) - Key {key_info}", status_code, response_text, key_identifier=api_key
                    )
                if status_code == 429:
                    raise RateLimitError(
                        f"速率限制超出 (429) - Key {key_info}", status_code, response_text, key_identifier=api_key
                    )
                raise APIResponseError(f"API错误 {status_code} - Key {key_info}", status_code, response_text)

        except (RateLimitError, PermissionDeniedError, PayloadTooLargeError, APIResponseError):
            raise
        except aiohttp.ClientProxyConnectionError as e:
            logger.error(f"代理连接错误: {e}")
            raise NetworkError(f"代理连接错误: {e}", original_exception=e) from e
        except (aiohttp.ClientConnectorError, aiohttp.ServerDisconnectedError, aiohttp.ClientOSError) as e:
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
            raise APIResponseError(f"无法解析API响应为JSON: {e}", response_text=response_text_for_error) from e
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
    ) -> dict[str, Any]:
        async with aiohttp.ClientSession() as session:
            all_initial_keys = self.api_keys_config[:]
            last_exception: Exception | None = None

            current_processed_images: list[dict[str, str]] = []
            if (request_type == "vision" or (request_type == "tool_call" and enable_multimodal)) and image_inputs:
                current_processed_images = await self._process_images_input(image_inputs, image_mime_type_override)

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
                    return {
                        "error": False,
                        "interrupted": True,
                        "full_text": "",
                        "streamed_text_summary": "Task interrupted before API call.",
                        "finish_reason": "INTERRUPTED_BEFORE_CALL",
                        "message": "Task was interrupted before an API call could be made in this attempt.",
                    }

                current_time = time.time()
                keys_to_reactivate = [
                    k for k, expiry_ts in self._temporarily_disabled_keys_429.items() if expiry_ts <= current_time
                ]
                for k_active in keys_to_reactivate:
                    del self._temporarily_disabled_keys_429[k_active]
                    logger.info(f"密钥 ...{k_active[-4:]} 的429临时禁用已到期并解除。")

                all_abandoned_permanently = self.abandoned_keys_config.union(self._abandoned_keys_runtime)

                available_keys_this_pass = [
                    key
                    for key in all_initial_keys
                    if key not in all_abandoned_permanently and key not in self._temporarily_disabled_keys_429
                ]

                if not available_keys_this_pass:
                    if (
                        self._temporarily_disabled_keys_429
                        and num_temp_disable_resets_done < allowed_temp_disable_resets
                    ):
                        logger.warning(
                            f"在第 {attempt_pass + 1} 次尝试轮中，所有可用密钥当前均处于429临时禁用状态。 "
                            "将清除临时禁用列表并重试 "
                            f"(已执行重置: {num_temp_disable_resets_done}/{allowed_temp_disable_resets})。"
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
                    key_display = f"...{current_key[-4:]}" if current_key and len(current_key) > 4 else "INVALID_KEY"
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
                        )
                        logger.info(
                            f"尝试轮 {attempt_pass + 1}/{max_retries + 1}, "
                            f"密钥 {key_idx + 1}/{len(available_keys_this_pass)} (ID: {key_display}): "
                            f"类型: {request_type}, {'流式' if is_streaming else '非流式'}, 模型: {self.model_name}"
                        )
                        if system_prompt and request_type != "embedding":
                            logger.info(
                                f"  使用 System Prompt (前50字符): {system_prompt[:50]}{'...' if len(system_prompt) > 50 else ''}"
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

                        # --- START: 小猫咪的淫纹植入处！ ---
                        if config.test_function.fallback_model_name != "":
                            is_successful_call = not result.get("error") and not result.get("interrupted")
                            is_non_streaming_text_request = not is_streaming and request_type != "embedding"
                            is_text_content_none = result.get("text") is None

                            if is_successful_call and is_non_streaming_text_request and is_text_content_none:
                                fallback_model_name = config.test_function.fallback_model_name  # 主人你指定的备用肉棒！
                                logger.warning(
                                    f"密钥 {key_display} 的请求成功，但返回的 text 字段为 None。将使用备用模型 '{fallback_model_name}' 尝试一次。"
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

                                logger.info(f"正在使用备用模型 '{fallback_model_name}' 进行单次重试...")
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
                                        f"API调用在密钥 {key_display} 尝试期间被中断信号中止。将直接返回中断结果。"
                                    )
                                return result
                        # --- END: 小猫咪的淫纹植入处！ ---

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
                            disable_until_ts = time.time() + self.rate_limit_disable_duration_seconds
                            self._temporarily_disabled_keys_429[e_rate.key_identifier] = disable_until_ts
                            logger.info(
                                f"密钥 {key_display} 已被临时禁用直到 {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(disable_until_ts))}."
                            )
                        current_pass_last_exception = e_rate

                    except PayloadTooLargeError as e_payload:
                        current_pass_last_exception = e_payload
                        if (
                            (request_type == "vision" or (request_type == "tool_call" and enable_multimodal))
                            and current_processed_images
                            and not images_have_been_compression_attempted_this_call
                            and self.enable_image_compression
                        ):
                            logger.info("检测到 PayloadTooLargeError，尝试对当前图像集进行响应式压缩...")
                            temp_compressed_images_data = []
                            any_image_compressed_reactively = False
                            for img_data_val in current_processed_images:
                                compressed_b64, new_mime = await self._compress_base64_image(
                                    img_data_val["b64_data"], img_data_val["mime_type"]
                                )
                                if compressed_b64 != img_data_val["b64_data"]:
                                    any_image_compressed_reactively = True
                                temp_compressed_images_data.append({"b64_data": compressed_b64, "mime_type": new_mime})

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
                            f"在尝试轮 {attempt_pass + 1} (密钥 {key_display}) 期间发生意外错误: {e_unexpected!s}",
                            exc_info=True,
                        )
                        current_pass_last_exception = e_unexpected

                    if key_idx < len(available_keys_this_pass) - 1:
                        logger.info(f"密钥 {key_display} 尝试失败。将尝试本轮中的下一个可用密钥。")
                    else:
                        logger.info(f"密钥 {key_display} (本轮最后一个) 尝试失败。")

                if current_pass_last_exception:
                    last_exception = current_pass_last_exception

                if attempt_pass < max_retries:
                    wait_duration = INITIAL_RETRY_PASS_DELAY_SECONDS * (2**attempt_pass)
                    logger.info(
                        f"第 {attempt_pass + 1} 次请求尝试轮未成功。"
                        f"等待 {wait_duration:.2f} 秒后进行下一次尝试轮 (如果适用)。"
                        f"本轮最后遇到的错误: {type(current_pass_last_exception).__name__ if current_pass_last_exception else '未明确记录'}"
                    )
                    await asyncio.sleep(wait_duration)
                elif attempt_pass == max_retries:
                    logger.error(
                        f"已达到最大请求尝试轮数 ({max_retries + 1})，且最后一轮未成功。"
                        f"最终错误: {type(last_exception).__name__ if last_exception else '未知或无可用密钥导致失败'}"
                    )

            if last_exception:
                if isinstance(last_exception, RateLimitError | PermissionDeniedError | PayloadTooLargeError):
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
                f"最后记录的异常 (如果存在): {type(last_exception).__name__ if last_exception else '无'}"
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
        **kwargs: Unpack[GenerationParams],
    ) -> dict[str, Any]:
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
        logger.info(f"generate_text_completion: {'流式' if is_stream else '非流式'}")
        if system_prompt:
            logger.info(
                f"  generate_text_completion 收到 System Prompt (前50字符): {system_prompt[:50]}{'...' if len(system_prompt) > 50 else ''}"
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
        logger.info(f"generate_vision_completion: {'流式' if is_stream else '非流式'}, 图像数量: {len(image_inputs)}")
        if system_prompt:
            logger.info(
                f"  generate_vision_completion 收到 System Prompt (前50字符): {system_prompt[:50]}{'...' if len(system_prompt) > 50 else ''}"
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
        logger.info(f"generate_with_tools: {'流式' if is_stream else '非流式'}, 工具数量: {len(tools)}")
        if system_prompt:
            logger.info(
                f"  generate_with_tools 收到 System Prompt (前50字符): {system_prompt[:50]}{'...' if len(system_prompt) > 50 else ''}"
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
