# src/llmrequest/core/media_processor.py

import asyncio
import base64
import io
import mimetypes
import os
from typing import Any

import aiohttp
from PIL import Image
from src.common.custom_logging.logging_config import get_logger

logger = get_logger(__name__)

DEFAULT_IMAGE_COMPRESSION_TARGET_BYTES: int = 1 * 1024 * 1024
DEFAULT_IMAGE_COMPRESSION_QUALITY_JPEG: int = 85
DEFAULT_IMAGE_COMPRESSION_SCALE_MIN: float = 0.2

class MediaProcessor:
    """Handles all media-related processing like downloading, encoding, and compression."""
    
    def __init__(
        self,
        enable_compression: bool = True,
        compression_target_bytes: int = DEFAULT_IMAGE_COMPRESSION_TARGET_BYTES,
    ):
        self.enable_compression = enable_compression
        self.compression_target_bytes = compression_target_bytes

    async def process_media_inputs(
        self,
        media_sources: list[str] | None,
        mime_type_override: str | None,
        proxy_url: str | None,
    ) -> list[dict[str, str]]:
        if not media_sources:
            return []
        
        async with aiohttp.ClientSession() as session:
            tasks = [
                self._process_single_media_input(src, session, mime_type_override, proxy_url)
                for src in media_sources
            ]
            results = await asyncio.gather(*tasks)
        return [result for result in results if result]

    async def _process_single_media_input(
        self,
        media_path_or_url_or_data_uri: str,
        session: aiohttp.ClientSession,
        mime_type_override: str | None,
        proxy_url_for_media: str | None,
    ) -> dict[str, str] | None:
        # This logic is extracted directly from the old LLMClient
        base64_media_data = None
        determined_mime_type = mime_type_override
        try:
            if media_path_or_url_or_data_uri.startswith("data:"):
                header, encoded_data = media_path_or_url_or_data_uri.split(",", 1)
                determined_mime_type = header.split(";")[0].split(":")[1]
                base64_media_data = encoded_data
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
                            determined_mime_type = response.headers.get("Content-Type", "").split(";")[0].strip()
                    else:
                        logger.error(f"媒体文件获取失败 {media_path_or_url_or_data_uri}, 状态码: {response.status}")
                        return None
            elif os.path.exists(media_path_or_url_or_data_uri):
                if not determined_mime_type:
                    guessed_mime, _ = mimetypes.guess_type(media_path_or_url_or_data_uri)
                    determined_mime_type = guessed_mime
                with open(media_path_or_url_or_data_uri, "rb") as media_file:
                    base64_media_data = base64.b64encode(media_file.read()).decode("utf-8")
            else:
                logger.error(f"媒体源未找到或无效: {media_path_or_url_or_data_uri[100:]}...")
                return None

            if not base64_media_data:
                return None

            determined_mime_type = determined_mime_type or "application/octet-stream"
            if "/" not in determined_mime_type:
                logger.warning(f"无效的MIME类型 '{determined_mime_type}'，将回退到 application/octet-stream。")
                determined_mime_type = "application/octet-stream"

            return {"b64_data": base64_media_data, "mime_type": determined_mime_type}
        except Exception as e:
            logger.exception(f"媒体处理过程中出错 {media_path_or_url_or_data_uri}: {e}")
            return None

    async def compress_base64_image(self, base64_data: str, original_mime_type: str) -> tuple[str, str]:
        if not self.enable_compression:
            return base64_data, original_mime_type
            
        try:
            image_bytes = base64.b64decode(base64_data)
            current_size_bytes = len(image_bytes)

            if current_size_bytes <= self.compression_target_bytes:
                return base64_data, original_mime_type

            img = Image.open(io.BytesIO(image_bytes))
            
            original_width, original_height = img.size
            scale_factor = max(
                DEFAULT_IMAGE_COMPRESSION_SCALE_MIN,
                min(1.0, (self.compression_target_bytes / current_size_bytes) ** 0.5),
            )
            new_width = max(1, int(original_width * scale_factor))
            new_height = max(1, int(original_height * scale_factor))

            output_buffer = io.BytesIO()
            save_params = {}

            if img.mode in ("RGBA", "LA", "P") or (isinstance(img.info, dict) and "transparency" in img.info):
                final_mime_type = "image/png"
                resized_img = img.convert("RGBA").resize((new_width, new_height), Image.Resampling.LANCZOS)
                save_params = {"optimize": True}
                resized_img.save(output_buffer, format="PNG", **save_params)
            else:
                final_mime_type = "image/jpeg"
                resized_img = img.convert("RGB").resize((new_width, new_height), Image.Resampling.LANCZOS)
                save_params = {"quality": DEFAULT_IMAGE_COMPRESSION_QUALITY_JPEG, "optimize": True}
                resized_img.save(output_buffer, format="JPEG", **save_params)
                
            compressed_bytes = output_buffer.getvalue()
            new_size_bytes = len(compressed_bytes)

            logger.info(f"图像响应式压缩: {current_size_bytes / 1024:.1f}KB -> {new_size_bytes / 1024:.1f}KB")
            return base64.b64encode(compressed_bytes).decode("utf-8"), final_mime_type
        except Exception as e:
            logger.error(f"响应式图像压缩失败: {e}", exc_info=True)
            return base64_data, original_mime_type