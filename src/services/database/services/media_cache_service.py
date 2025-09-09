# src/services/database/services/media_cache_service.py
import asyncio
import base64
import json
import time
from pathlib import Path
from typing import Any

from src.common.custom_logging.logging_config import get_logger
from src.config import config
from typedb.driver import TransactionType

from ..core.connection_manager import TypeDBConnectionManager
from ..models import ImageCacheDocument

logger = get_logger(__name__)


class MediaCacheService:
    """服务：同时管理媒体文件缓存(数据库)和原始媒体文件缓存(本地文件系统)."""
    CACHE_VERSION = "v1.0"
    CACHE_TTL_SECONDS = 7 * 24 * 3600

    def __init__(self, conn_manager: TypeDBConnectionManager) -> None:
        self.conn_manager = conn_manager
        # --- 新增文件缓存路径 ---
        self.media_cache_dir = Path(config.runtime_environment.data_root) / "media_cache"
        self.media_cache_dir.mkdir(parents=True, exist_ok=True)
        logger.info(
            f"MediaCacheService (TypeDB+FS) 初始化完成。文件缓存目录: {self.media_cache_dir}"
        )

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

    def _get_file_path_for_hash(self, image_hash: str) -> Path:
        """根据哈希生成确定性的文件存储路径."""
        # 使用哈希的前两位作为子目录，避免单个文件夹内文件过多
        sub_dir = self.media_cache_dir / image_hash[:2]
        return sub_dir / image_hash

    # --- [核心新增] 存储和获取原始图片数据 ---
    async def get_image_b64_by_hash(self, image_hash: str) -> dict | None:
        """通过哈希从文件缓存获取图片的Base64和MIME类型."""
        file_path = self._get_file_path_for_hash(image_hash)

        if not await asyncio.to_thread(file_path.exists):
            return None

        try:
            # 文件的MIME类型存储在TypeDB的image-cache实体中
            query = (
                f'match $ic isa image-cache, has image-hash "{image_hash}"; '
                f'$ic has mime-type $mime; select $mime;'
            )
            driver = self.conn_manager.get_driver()
            db_name = self.conn_manager.database_name

            def db_read_mime() -> str | None:
                with driver.transaction(db_name, TransactionType.READ) as tx:
                    answers = list(tx.query(query).resolve().as_concept_rows())
                    if answers and (mime_attr := answers[0].get("mime")):
                        return mime_attr.as_attribute().get_value()
                return None

            mime_type = await asyncio.to_thread(db_read_mime)
            if not mime_type:
                logger.warning(f"在文件缓存中找到图片 {image_hash}，但在数据库中未找到其MIME类型。")
                # 即使没有MIME类型，也尝试读取
                mime_type = "application/octet-stream"

            image_bytes = await asyncio.to_thread(file_path.read_bytes)
            b64_data = base64.b64encode(image_bytes).decode('utf-8')

            return {"hash": image_hash, "base64": b64_data, "mime_type": mime_type}
        except Exception as e:
            logger.error(f"从文件缓存读取图片 {image_hash} 失败: {e}", exc_info=True)
            return None

    async def save_image_b64(self, image_hash: str, base64_data: str, mime_type: str) -> bool:
        """保存图片的Base64数据到文件缓存，并在数据库中记录元数据."""
        file_path = self._get_file_path_for_hash(image_hash)

        try:
            # 异步写入文件
            image_bytes = base64.b64decode(base64_data)
            file_path.parent.mkdir(exist_ok=True)
            await asyncio.to_thread(file_path.write_bytes, image_bytes)

            # 在数据库中记录元数据（路径和MIME类型）
            driver = self.conn_manager.get_driver()
            db_name = self.conn_manager.database_name

            def db_write_meta() -> bool:
                with driver.transaction(db_name, TransactionType.WRITE) as tx:
                    # 使用 put (upsert) 逻辑
                    put_query = f"""
                    match $ic isa image-cache, has image-hash "{image_hash}";
                    put $ic has file-path "{str(file_path.resolve()).replace('"', '\\"')}",
                        has mime-type "{mime_type}";
                    """
                    tx.query(put_query).resolve()
                    tx.commit()
                return True

            await asyncio.to_thread(db_write_meta)
            logger.info(f"图片 {image_hash[:10]}... 已保存到文件缓存并记录元数据。")
            return True
        except Exception as e:
            logger.error(f"保存图片 {image_hash} 到文件缓存失败: {e}", exc_info=True)
            return False
