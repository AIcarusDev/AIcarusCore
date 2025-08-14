# src/database/services/sticker_storage_service.py
import asyncio
from typing import Any

from src.common.custom_logging.logging_config import get_logger
from src.common.image_utils import compare_phashes
from src.database.core.typedb_connection_manager import TypeDBConnectionManager
from typedb.driver import TransactionType

logger = get_logger(__name__)

class StickerStorageService:
    """服务类，负责管理表情包元数据的存储和检索 (TypeDB gRPC 版本)."""

    def __init__(self, conn_manager: TypeDBConnectionManager) -> None:
        """初始化表情包存储服务."""
        self.conn_manager = conn_manager
        logger.info("StickerStorageService (TypeDB gRPC) 初始化完成。")

    async def _get_next_sticker_id(self, platform_id: str, tx) -> str:
        """在事务内原子性地获取下一个可用的表情包ID (e.g., "001", "002")."""
        # TypeQL 使用聚合查询来找到最大ID
        query = f"""
        match
            $p isa platform, has platform-uid "{platform_id}";
            (hosting-platform: $p, hosted-asset: $s) isa platform-asset;
            $s isa sticker, has sticker-uid $uid;
        reduce $max_id = max($uid);
        """
        response = tx.query(query).resolve()
        answers = list(response.as_concept_rows())

        max_id_num = 0
        if answers and (max_id_concept := answers[0].get("max_id")):
            max_id_str = max_id_concept.as_attribute().get_value().as_string()
            # 从 "platform_sticker_001" 中提取数字
            numeric_part = ''.join(filter(str.isdigit, max_id_str))
            if numeric_part:
                max_id_num = int(numeric_part)

        next_id_num = max_id_num + 1
        return f"{next_id_num:03d}"

    async def add_sticker(
        self,
        platform_id: str,
        filename: str,
        impression: str,
        source_image_hash: str,
        perceptual_hash: str
    ) -> str | None:
        """添加一个新的表情包元数据记录，并将其与对应的平台关联."""
        driver = self.conn_manager.get_driver()
        db_name = self.conn_manager.get_database_name()

        def db_write() -> str | None:
            with driver.transaction(db_name, TransactionType.WRITE) as tx:
                # 在事务内获取下一个ID
                next_id = asyncio.run(self._get_next_sticker_id(platform_id, tx))
                sticker_uid = f"{platform_id}_sticker_{next_id}"

                query = f"""
                match $p isa platform, has platform-uid "{platform_id}";
                insert $s isa sticker,
                    has sticker-uid "{sticker_uid}",
                    has filename "{filename.replace('"', '\\"')}",
                    has impression "{impression.replace('"', '\\"')}",
                    has image-hash "{source_image_hash}",
                    has perceptual-hash "{perceptual_hash}";
                $pa (hosting-platform: $p, hosted-asset: $s) isa platform-asset;
                """
                tx.query(query).resolve()
                tx.commit()
                return sticker_uid

        try:
            sticker_uid = await asyncio.to_thread(db_write)
            logger.info(f"新表情包 '{sticker_uid}' 已添加到 TypeDB 并关联到平台 '{platform_id}'。")
            return sticker_uid
        except Exception as e:
            logger.error(f"添加表情包到 TypeDB 失败: {e}", exc_info=True)
            return None

    # TODO: [PERFORMANCE] 当前的相似度检查实现存在性能瓶颈。
    #  它会获取平台下的所有哈希值到内存中进行比较。当表情包数量巨大时，这会导致高昂的I/O和CPU开销。
    #  未来的优化方向:
    #  1.  **数据库层面优化**: 利用 ArangoSearch View 和 LEVENSHTEIN_MATCH 函数，
    #      将汉明距离的计算下推到数据库层面，利用其C++实现的索引进行高效的近似最近邻搜索。
    #  2.  **LSH (局部敏感哈希)**: 在应用层实现LSH算法，将相似的哈希映射到相同的桶中，
    #      查询时只需比较同桶内的少量候选者。
    #  对于当前版本，此实现是可接受的。
    #  这个注释不许删! ╰（‵□′）╯

    async def find_similar_sticker_by_phash(
        self, platform_id: str, phash_to_check: str, tolerance: int = 5
    ) -> dict[str, Any] | None:
        """根据感知哈希查找视觉上相似的表情包."""
        query = f"""
        match
            $p isa platform, has platform-uid "{platform_id}";
            (hosting-platform: $p, hosted-asset: $s) isa platform-asset;
            $s isa sticker, has sticker-uid $uid, has perceptual-hash $phash;
        select $uid, $phash;
        """
        driver = self.conn_manager.get_driver()
        db_name = self.conn_manager.get_database_name()

        def db_read_and_compare() -> (dict[str, Any] | None):
            with driver.transaction(db_name, TransactionType.READ) as tx:
                response = tx.query(query).resolve()
                for answer in response.as_concept_rows():
                    candidate_uid = answer.get("uid").as_attribute().get_value().as_string()
                    candidate_phash = answer.get("phash").as_attribute().get_value().as_string()
                    if compare_phashes(phash_to_check, candidate_phash, tolerance):
                        logger.info(
                            f"发现视觉相似的表情包: ID {candidate_uid} (pHash 距离 <= {tolerance})"
                        )
                        return {"sticker_id": candidate_uid, "phash": candidate_phash}
            return None

        try:
            return await asyncio.to_thread(db_read_and_compare)
        except Exception as e:
            logger.error(f"查找相似表情包时失败: {e}", exc_info=True)
            return None

    async def remove_sticker(self, sticker_uid: str) -> bool:
        """从 TypeDB 中删除一个表情包元数据记录."""
        query = f"""
        match $s isa sticker, has sticker-uid "{sticker_uid}";
        delete $s isa sticker;
        """

        driver = self.conn_manager.get_driver()
        db_name = self.conn_manager.get_database_name()

        def db_write_and_check() -> bool:
            """执行数据库写入操作并检查删除的表情包."""
            with driver.transaction(db_name, TransactionType.WRITE) as tx:
                response = tx.query(query).resolve()
                deleted_concepts = [row.get("s") for row in response.as_concept_rows()]
                if not deleted_concepts:
                    logger.warning(f"尝试删除一个不存在的表情包: {sticker_uid}")
                    return False
                tx.commit()
                return True

        try:
            success = await asyncio.to_thread(db_write_and_check)
            if success:
                logger.info(f"表情包 '{sticker_uid}' 已从 TypeDB 移除。")
            return success
        except Exception as e:
            logger.error(f"移除表情包 '{sticker_uid}' 失败: {e}", exc_info=True)
            return False

    async def edit_impression(self, sticker_uid: str, new_impression: str) -> bool:
        """编辑指定表情包的印象描述."""
        driver = self.conn_manager.get_driver()
        db_name = self.conn_manager.get_database_name()

        def db_update() -> bool:
            with driver.transaction(db_name, TransactionType.WRITE) as tx:
                # 1. 找到表情包和它的旧印象
                match_query = f"""
                match $s isa sticker, has sticker-uid "{sticker_uid}", has impression $old_imp;
                select $s, $old_imp;
                """
                response = tx.query(match_query).resolve()
                answers = list(response.as_concept_rows())
                if not answers:
                    logger.warning(f"尝试编辑一个不存在的表情包印象: {sticker_uid}")
                    return False

                # 2. 删除旧印象
                delete_query = f"""
                match $s isa sticker, has sticker-uid "{sticker_uid}", has impression $old_imp;
                delete $s has $old_imp;
                """
                tx.query(delete_query).resolve()

                # 3. 插入新印象
                insert_query = f"""
                match $s isa sticker, has sticker-uid "{sticker_uid}";
                insert $s has impression "{new_impression.replace('"', '\\"')}";
                """
                tx.query(insert_query).resolve()
                tx.commit()
                return True
        try:
            success = await asyncio.to_thread(db_update)
            if success:
                logger.info(f"表情包 '{sticker_uid}' 的印象已更新。")
            return success
        except Exception as e:
            logger.error(f"编辑表情包 '{sticker_uid}' 印象失败: {e}", exc_info=True)
            return False

    async def get_all_stickers(self, platform_uid: str) -> list[dict]:
        """获取指定平台的所有表情包元数据."""
        query = f"""
        match
            $p isa platform, has platform-uid "{platform_uid}";
            (hosting-platform: $p, hosted-asset: $s) isa platform-asset;
            $s isa sticker;
            $s has sticker-uid $uid;
            $s has filename $fn;
            $s has impression $imp;
            $s has image-hash $hash;
        select $uid, $fn, $imp, $hash;
        sort $uid asc;
        """

        driver = self.conn_manager.get_driver()
        db_name = self.conn_manager.get_database_name()

        def db_read() -> list[dict]:
            """执行数据库读取操作."""
            stickers = []
            with driver.transaction(db_name, TransactionType.READ) as tx:
                response = tx.query(query).resolve()
                for answer in response.as_concept_rows():
                    stickers.append({
                        "sticker_id": answer.get("uid").as_attribute().get_value().as_string(),
                        "filename": answer.get("fn").as_attribute().get_value().as_string(),
                        "impression": answer.get("imp").as_attribute().get_value().as_string(),
                        "image_hash": answer.get("hash").as_attribute().get_value().as_string(),
                    })
            return stickers

        try:
            return await asyncio.to_thread(db_read)
        except Exception as e:
            logger.error(f"获取平台 '{platform_uid}' 的所有表情包失败: {e}", exc_info=True)
            return []

    async def get_sticker_by_id(self, sticker_uid: str) -> dict[str, Any] | None:
        """根据UID获取单个表情包的元数据."""
        query = f"""
        match
            $s isa sticker, has sticker-uid "{sticker_uid}";
            $s has filename $fn;
            $s has impression $imp;
            $s has image-hash $hash;
            $s has perceptual-hash $phash;
        select $fn, $imp, $hash, $phash;
        """
        driver = self.conn_manager.get_driver()
        db_name = self.conn_manager.get_database_name()

        def db_read() -> (dict[str, Any] | None):
            with driver.transaction(db_name, TransactionType.READ) as tx:
                response = tx.query(query).resolve()
                answers = list(response.as_concept_rows())
                if answers:
                    answer = answers[0]
                    return {
                        "sticker_id": sticker_uid,
                        "filename": answer.get("fn").as_attribute().get_value().as_string(),
                        "impression": answer.get("imp").as_attribute().get_value().as_string(),
                        "image_hash": answer.get("hash").as_attribute().get_value().as_string(),
                    "perceptual_hash": answer.get("phash").as_attribute().get_value().as_string()
                    }
            return None

        try:
            return await asyncio.to_thread(db_read)
        except Exception as e:
            logger.error(f"获取表情包 '{sticker_uid}' 失败: {e}", exc_info=True)
            return None
