# src/database/services/image_analysis_cache_service.py
from typing import Any

from src.common.custom_logging.logging_config import get_logger
from src.database import ArangoDBConnectionManager, CoreDBCollections
from src.database.models import ImageAnalysisCacheDocument

logger = get_logger(__name__)


class ImageAnalysisCacheService:
    """服务类，负责处理图片分析结果的缓存存储和检索."""

    def __init__(self, conn_manager: ArangoDBConnectionManager) -> None:
        self.conn_manager = conn_manager
        self.collection_name = CoreDBCollections.IMAGE_ANALYSIS_CACHE
        logger.info(f"ImageAnalysisCacheService 初始化完毕，将操作集合 '{self.collection_name}'。")

    async def get_analysis_by_hash(self, image_hash: str) -> dict[str, Any] | None:
        """根据图片内容的哈希值从缓存中检索分析结果."""
        if not image_hash:
            return None
        try:
            collection = await self.conn_manager.get_collection(self.collection_name)
            cached_doc = await collection.get(image_hash)
            if cached_doc:
                logger.info(f"图片分析缓存命中！哈希: {image_hash[:10]}...")
                return cached_doc.get("analysis_result")
            return None
        except Exception as e:
            logger.error(f"从缓存检索图片分析时失败 (哈希: {image_hash}): {e}", exc_info=True)
            return None

    async def save_analysis(self, image_hash: str, analysis_result: dict[str, Any]) -> bool:
        """将新的图片分析结果存入缓存."""
        if not image_hash or not analysis_result:
            return False
        try:
            collection = await self.conn_manager.get_collection(self.collection_name)
            cache_doc = ImageAnalysisCacheDocument(
                _key=image_hash,
                analysis_result=analysis_result
            )
            await collection.insert(cache_doc.to_dict(), overwrite_mode="replace")
            logger.info(f"新的图片分析结果已存入缓存。哈希: {image_hash[:10]}...")
            return True
        except Exception as e:
            logger.error(f"保存图片分析到缓存时失败 (哈希: {image_hash}): {e}", exc_info=True)
            return False
