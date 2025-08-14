# src/database/services/image_analysis_cache_service.py
import asyncio
import json
import time
from typing import Any

from src.common.custom_logging.logging_config import get_logger
from typedb.driver import TransactionType
from ..core.connection_manager import TypeDBConnectionManager
from ..models import ImageCacheDocument

logger = get_logger(__name__)


class ImageAnalysisCacheService:
    """服务类，负责处理图片分析结果的缓存存储和检索 (TypeDB 版本)."""

    def __init__(self, conn_manager: TypeDBConnectionManager) -> None:
        self.conn_manager = conn_manager
        logger.info("ImageAnalysisCacheService (TypeDB) 初始化完成。")

    async def get_analysis_by_hash(
        self, image_hash: str, version: str, ttl_seconds: int
    ) -> dict[str, Any] | None:
        """根据哈希值从缓存检索分析结果，并校验版本和TTL."""
        if not image_hash:
            return None

        query = f"""
        match $ic isa image-cache, has image-hash "{image_hash}";
        $ic has version $v;
        $ic has timestamp $ts;
        $ic has analysis-result-json $res;
        get $v, $ts, $res;
        """
        driver = self.conn_manager.get_driver()
        db_name = self.conn_manager.database_name

        def db_read() -> dict[str, Any] | None:
            with driver.transaction(db_name, TransactionType.READ) as tx:
                # [修正] tx.query 是方法
                answers = list(tx.query(query).resolve())
                if not answers:
                    return None

                cached_version = answers[0].get("v").as_attribute().get_value().get_string()
                if cached_version != version:
                    logger.info(
                        f"缓存版本不匹配 (需要: {version}, 现有: {cached_version})，缓存无效。"
                    )
                    return None

                stored_timestamp_ms = answers[0].get("ts").as_attribute().get_value().get_integer()
                if time.time() * 1000 > stored_timestamp_ms + (ttl_seconds * 1000):
                    logger.info(f"缓存已过期，缓存无效。哈希: {image_hash[:10]}...")
                    return None

                result_json = answers[0].get("res").as_attribute().get_value().get_string()
                return json.loads(result_json)

        try:
            result = await asyncio.to_thread(db_read)
            if result:
                logger.info(f"图片分析缓存命中！哈希: {image_hash[:10]}...")
            return result
        except Exception as e:
            logger.error(f"从缓存检索图片分析时失败 (哈希: {image_hash}): {e}", exc_info=True)
            return None

    async def save_analysis(self, image_hash: str, analysis_result: dict, version: str) -> bool:
        """将新的图片分析结果存入缓存 (UPSERT)."""
        cache_doc = ImageCacheDocument(
            _key=image_hash,
            analysis_result=analysis_result,
            version=version,
            timestamp=int(time.time() * 1000),
        )

        driver = self.conn_manager.get_driver()
        db_name = self.conn_manager.database_name

        def db_write() -> bool:
            with driver.transaction(db_name, TransactionType.WRITE) as tx:
                delete_query = f'match $ic isa image-cache, has image-hash "{cache_doc._key}"; delete $ic isa image-cache;'
                # [修正] tx.query 是方法
                tx.query(delete_query).resolve()

                result_json_safe = json.dumps(
                    cache_doc.analysis_result, ensure_ascii=False
                ).replace('"', '\\"')
                insert_query = f"""
                insert $ic isa image-cache,
                    has image-hash "{cache_doc._key}",
                    has analysis-result-json "{result_json_safe}",
                    has version "{cache_doc.version}",
                    has timestamp {cache_doc.timestamp};
                """
                # [修正] tx.query 是方法
                tx.query(insert_query).resolve()
                tx.commit()
                return True

        try:
            success = await asyncio.to_thread(db_write)
            if success:
                logger.info(
                    f"新的图片分析结果已存入缓存。哈希: {cache_doc._key[:10]}..., "
                    f"版本: {cache_doc.version}"
                )
            return success
        except Exception as e:
            logger.error(f"保存图片分析到缓存时失败 (哈希: {cache_doc._key}): {e}", exc_info=True)
            return False