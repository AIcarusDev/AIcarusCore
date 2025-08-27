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
    """Service for managing image analysis cache operations in TypeDB.

    This service provides methods for caching and retrieving image analysis results
    to avoid redundant processing. It stores analysis results with version information
    and timestamp-based TTL for cache validity.

    Attributes:
    ----------
    conn_manager : TypeDBConnectionManager
        The connection manager for TypeDB database operations.

    Methods:
    -------
    get_analysis_by_hash(image_hash, version, ttl_seconds)
        Get cached image analysis result by hash if valid and matches version.
    save_analysis(image_hash, analysis_result, version)
        Save image analysis result to cache with version and timestamp.
    """

    def __init__(self, conn_manager: TypeDBConnectionManager) -> None:
        self.conn_manager = conn_manager
        logger.info("ImageAnalysisCacheService (TypeDB) 初始化完成。")

    async def get_analysis_by_hash(
        self, image_hash: str, version: str, ttl_seconds: int
    ) -> dict[str, Any] | None:
        """Get cached image analysis result by hash.

        Parameters
        ----------
        image_hash : str
            The hash of the image to retrieve analysis for.
        version : str
            The version of the analysis algorithm to match.
        ttl_seconds : int
            The time-to-live in seconds for cache validity.

        Returns:
        -------
        dict[str, Any] | None
            The cached analysis result if found and valid, None otherwise.
        """
        if not image_hash:
            return None
        query = f"""
        match $ic isa image-cache, has image-hash "{image_hash}";
        $ic has version $v;
        $ic has timestamp $ts;
        $ic has analysis-result-json $res;
        select $v, $ts, $res;
        """
        driver = self.conn_manager.get_driver()
        db_name = self.conn_manager.database_name

        def db_read() -> dict[str, Any] | None:
            with driver.transaction(db_name, TransactionType.READ) as tx:
                answers = list(tx.query(query).resolve().as_concept_rows())
                if not answers:
                    return None
                cached_version = answers[0].get("v").as_attribute().get_value()
                if cached_version != version:
                    return None
                stored_timestamp_ms = answers[0].get("ts").as_attribute().get_value()
                if time.time() * 1000 > stored_timestamp_ms + (ttl_seconds * 1000):
                    return None
                result_json = answers[0].get("res").as_attribute().get_value()
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
        """Save image analysis result to cache.

        Parameters
        ----------
        image_hash : str
            The hash of the image for which analysis was performed.
        analysis_result : dict
            The analysis result data to be cached.
        version : str
            The version of the analysis algorithm used.

        Returns:
        -------
        bool
            True if the analysis was successfully saved to cache, False otherwise.
        """
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
                delete_query = (
                    f'match $ic isa image-cache, has image-hash "{cache_doc._key}"; delete $ic;'
                )
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
                tx.query(insert_query).resolve()
                tx.commit()
                return True

        try:
            success = await asyncio.to_thread(db_write)
            if success:
                logger.info(
                    f"新的图片分析结果已存入缓存。"
                    f"哈希: {cache_doc._key[:10]}..., 版本: {cache_doc.version}"
                )
            return success
        except Exception as e:
            logger.error(f"保存图片分析到缓存时失败 (哈希: {cache_doc._key}): {e}", exc_info=True)
            return False
