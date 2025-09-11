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

    async def get_images_b64_by_hashes(self, image_hashes: list[str]) -> dict[str, dict]:
        """批量通过哈希从文件缓存获取图片的Base64和MIME类型.

        Args:
            image_hashes: 一个包含多个图片哈希的列表。

        Returns:
            一个字典，键是图片哈希，值是包含 "base64" 和 "mime_type" 的字典。
            只包含成功找到的图片。
        """
        if not image_hashes:
            return {}

        # 并发执行所有单个图片的获取任务
        tasks = [self.get_image_b64_by_hash(h) for h in image_hashes]
        results = await asyncio.gather(*tasks)

        # 将结果重新组织成以哈希为键的字典
        # 过滤掉返回 None 的失败结果
        return {res["hash"]: res for res in results if res and "hash" in res}

    async def save_images_b64(self, images_data: list[dict]) -> None:
        """批量保存从Adapter获取的图片数据到缓存.

        Args:
            images_data: 一个字典列表，每个字典包含 "hash", "base64", "mime_type"。
        """
        if not images_data:
            return

        tasks = [
            self.save_image_b64(
                img.get("hash", ""), img.get("base64", ""), img.get("mime_type", "")
            )
            for img in images_data
            if isinstance(img, dict)
        ]
        await asyncio.gather(*tasks)
        logger.info(f"已批量缓存 {len(tasks)} 张从Adapter获取的图片。")

    # 存储和获取原始图片数据 ---
    async def get_image_b64_by_hash(self, image_hash: str) -> dict | None:
        """通过哈希从文件缓存获取图片的Base64和MIME类型."""
        file_path = self._get_file_path_for_hash(image_hash)

        if not await asyncio.to_thread(file_path.exists):
            return None

        try:
            # 文件的MIME类型存储在TypeDB的image-cache实体中
            query = (
                f'match $ic isa image-cache, has image-hash "{image_hash}"; '
                f"$ic has mime-type $mime; select $mime;"
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
            b64_data = base64.b64encode(image_bytes).decode("utf-8")

            return {"hash": image_hash, "base64": b64_data, "mime_type": mime_type}
        except Exception as e:
            logger.error(f"从文件缓存读取图片 {image_hash} 失败: {e}", exc_info=True)
            return None

    async def save_image_b64(self, image_hash: str, base64_data: str, mime_type: str) -> bool:
        """保存图片的Base64数据到文件缓存，并在数据库中记录元数据."""
        if not all([image_hash, base64_data, mime_type]):
            logger.warning("尝试保存空的图片数据到缓存，已跳过。")
            return False
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
                    # 对 Windows 路径中的反斜杠进行转义
                    safe_file_path = str(file_path.resolve()).replace("\\", "\\\\")
                    safe_file_path = safe_file_path.replace('"', '\\"')

                    # 1. 检查实体是否存在
                    match_query = (
                        f'match $ic isa image-cache, has image-hash "{image_hash}"; select $ic;'
                    )
                    existing = list(tx.query(match_query).resolve())

                    if existing:
                        # 2a. 如果存在，使用 update 语句确保属性被添加或覆盖
                        update_query = f"""
                        match $ic isa image-cache, has image-hash "{image_hash}";
                        delete $ic has file-path $fp if present;
                        delete $ic has mime-type $mt if present;
                        insert $ic has file-path "{safe_file_path}",
                                has mime-type "{mime_type}";
                        """
                        tx.query(update_query).resolve()
                    else:
                        # 2b. 如果不存在，使用 insert 语句创建实体并添加属性
                        insert_query = f"""
                        insert $ic isa image-cache,
                            has image-hash "{image_hash}",
                            has file-path "{safe_file_path}",
                            has mime-type "{mime_type}";
                        """
                        tx.query(insert_query).resolve()

                    tx.commit()
                return True

            await asyncio.to_thread(db_write_meta)
            logger.info(f"图片 {image_hash[:10]}... 已保存到文件缓存并记录元数据。")
            return True
        except Exception as e:
            logger.error(f"保存图片 {image_hash} 到文件缓存失败: {e}", exc_info=True)
            return False
