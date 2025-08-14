# src/database/services/sticker_storage_service.py
import asyncio
import time
from typing import Any

from src.common.custom_logging.logging_config import get_logger
from src.common.image_utils import compare_phashes
from typedb.driver import Transaction, TransactionType
from ..core.connection_manager import TypeDBConnectionManager

logger = get_logger(__name__)


class StickerStorageService:
    """服务类，负责管理表情包元数据的存储和检索 (TypeDB gRPC 版本)."""

    def __init__(self, conn_manager: TypeDBConnectionManager) -> None:
        """初始化表情包存储服务."""
        self.conn_manager = conn_manager
        logger.info("StickerStorageService (TypeDB gRPC) 初始化完成。")

    def _get_next_sticker_id(self, platform_id: str, tx: Transaction) -> str:
        """在事务内原子性地获取下一个可用的表情包ID (e.g., "001", "002")."""
        query = f"""
        match
            $p isa platform, has platform-uid "{platform_id}";
            (hosting-platform: $p, hosted-asset: $s) isa platform-asset;
            $s isa sticker, has sticker-id $sid;
        reduce $max_id = max($sid);
        """
        # [修正] tx.query 是方法
        answers = list(tx.query(query).resolve())

        max_id_num = 0
        if answers and (max_id_concept := answers[0]) and not max_id_concept.is_nan():
            max_id_num = max_id_concept.as_int()

        next_id_num = max_id_num + 1
        return f"{next_id_num:03d}"

    async def add_sticker(
        self,
        platform_id: str,
        filename: str,
        impression: str,
        source_image_hash: str,
        perceptual_hash: str,
    ) -> dict[str, Any] | None:
        """添加一个新的表情包元数据记录，并将其与对应的平台关联."""
        driver = self.conn_manager.get_driver()
        db_name = self.conn_manager.database_name

        def db_write() -> dict[str, Any] | None:
            with driver.transaction(db_name, TransactionType.WRITE) as tx:
                next_id_str = self._get_next_sticker_id(platform_id, tx)
                sticker_uid = f"{platform_id}_sticker_{next_id_str}"
                added_at_ts = int(time.time() * 1000)

                query = f"""
                match $p isa platform, has platform-uid "{platform_id}";
                insert $s isa sticker,
                    has sticker-uid "{sticker_uid}",
                    has sticker-id {int(next_id_str)},
                    has filename "{filename.replace('"', '\\"')}",
                    has impression "{impression.replace('"', '\\"')}",
                    has image-hash "{source_image_hash}",
                    has perceptual-hash "{perceptual_hash}",
                    has added-at {added_at_ts};
                insert (hosting-platform: $p, hosted-asset: $s) isa platform-asset;
                """
                # [修正] tx.query 是方法
                tx.query(query).resolve()
                tx.commit()
                return {
                    "sticker_id": next_id_str,
                    "sticker_uid": sticker_uid,
                    "filename": filename,
                    "impression": impression,
                    "added_at": added_at_ts,
                }

        try:
            sticker_doc = await asyncio.to_thread(db_write)
            if sticker_doc:
                logger.info(
                    f"新表情包 '{sticker_doc['sticker_uid']}' "
                    f"已添加到 TypeDB 并关联到平台 '{platform_id}'。"
                )
            return sticker_doc
        except Exception as e:
            logger.error(f"添加表情包到 TypeDB 失败: {e}", exc_info=True)
            return None

    async def find_similar_sticker_by_phash(
        self, platform_id: str, phash_to_check: str, tolerance: int = 5
    ) -> dict[str, Any] | None:
        """根据感知哈希查找视觉上相似的表情包."""
        query = f"""
        match
            $p isa platform, has platform-uid "{platform_id}";
            (hosting-platform: $p, hosted-asset: $s) isa platform-asset;
            $s isa sticker, has sticker-uid $uid, has perceptual-hash $phash;
        get $uid, $phash;
        """
        driver = self.conn_manager.get_driver()
        db_name = self.conn_manager.database_name

        def db_read_and_compare() -> dict[str, Any] | None:
            with driver.transaction(db_name, TransactionType.READ) as tx:
                # [修正] tx.query 是方法
                answers = list(tx.query(query).resolve())
                for answer in answers:
                    uid_attr = answer.get("uid")
                    phash_attr = answer.get("phash")
                    if uid_attr and phash_attr:
                        candidate_uid = uid_attr.as_attribute().get_value().get_string()
                        candidate_phash = phash_attr.as_attribute().get_value().get_string()
                        if compare_phashes(phash_to_check, candidate_phash, tolerance):
                            logger.info(
                                f"发现视觉相似的表情包: ID {candidate_uid} "
                                f"(pHash 距离 <= {tolerance})"
                            )
                            return {
                                "sticker_id": candidate_uid,
                                "phash": candidate_phash,
                            }
            return None

        try:
            return await asyncio.to_thread(db_read_and_compare)
        except Exception as e:
            logger.error(f"查找相似表情包时失败: {e}", exc_info=True)
            return None

    async def remove_sticker(self, platform_id: str, sticker_id: str) -> bool:
        """从 TypeDB 中删除一个表情包元数据记录."""
        sticker_uid = f"{platform_id}_sticker_{sticker_id}"
        query = f"""
        match $s isa sticker, has sticker-uid "{sticker_uid}";
        delete $s isa sticker;
        """

        driver = self.conn_manager.get_driver()
        db_name = self.conn_manager.database_name

        def db_write() -> bool:
            with driver.transaction(db_name, TransactionType.WRITE) as tx:
                # [修正] tx.query 是方法
                tx.query(query).resolve()
                tx.commit()
                return True

        try:
            success = await asyncio.to_thread(db_write)
            if success:
                logger.info(f"表情包 '{sticker_uid}' 已从 TypeDB 移除。")
            return success
        except Exception as e:
            logger.error(f"移除表情包 '{sticker_uid}' 失败: {e}", exc_info=True)
            return False

    async def edit_impression(self, platform_id: str, sticker_id: str, new_impression: str) -> bool:
        """编辑指定表情包的印象描述."""
        sticker_uid = f"{platform_id}_sticker_{sticker_id}"
        new_impression_safe = new_impression.replace('"', '\\"')

        driver = self.conn_manager.get_driver()
        db_name = self.conn_manager.database_name

        def db_update() -> bool:
            with driver.transaction(db_name, TransactionType.WRITE) as tx:
                match_query = f'match $s isa sticker, has sticker-uid "{sticker_uid}"; get $s;'
                # [修正] tx.query 是方法
                answers = list(tx.query(match_query).resolve())
                if not answers:
                    logger.warning(f"尝试编辑一个不存在的表情包印象: {sticker_uid}")
                    return False

                delete_query = f"""
                match $s isa sticker, has sticker-uid "{sticker_uid}";
                $s has impression $old_imp;
                delete $s has $old_imp;
                """
                # [修正] tx.query 是方法
                tx.query(delete_query).resolve()

                insert_query = f"""
                match $s isa sticker, has sticker-uid "{sticker_uid}";
                insert $s has impression "{new_impression_safe}";
                """
                # [修正] tx.query 是方法
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

    async def get_all_stickers(self, platform_id: str) -> list[dict]:
        """获取指定平台的所有表情包元数据."""
        query = f"""
        match
            $p isa platform, has platform-uid "{platform_id}";
            (hosting-platform: $p, hosted-asset: $s) isa platform-asset;
            $s isa sticker;
            $s has sticker-id $sid;
            $s has filename $fn;
            $s has impression $imp;
            $s has image-hash $hash;
        get $sid, $fn, $imp, $hash;
        sort $sid asc;
        """

        driver = self.conn_manager.get_driver()
        db_name = self.conn_manager.database_name

        def db_read() -> list[dict]:
            stickers = []
            with driver.transaction(db_name, TransactionType.READ) as tx:
                # [修正] tx.query 是方法
                answers = list(tx.query(query).resolve())
                for answer in answers:
                    stickers.append(
                        {
                            "sticker_id": f"{answer.get('sid').as_attribute().get_value().get_integer():03d}",
                            "filename": answer.get("fn").as_attribute().get_value().get_string(),
                            "impression": answer.get("imp").as_attribute().get_value().get_string(),
                            "source_image_hash": answer.get("hash")
                            .as_attribute()
                            .get_value()
                            .get_string(),
                        }
                    )
            return stickers

        try:
            return await asyncio.to_thread(db_read)
        except Exception as e:
            logger.error(f"获取平台 '{platform_id}' 的所有表情包失败: {e}", exc_info=True)
            return []

    async def get_sticker_by_id(self, platform_id: str, sticker_id: str) -> dict[str, Any] | None:
        """根据ID获取单个表情包的元数据."""
        sticker_uid = f"{platform_id}_sticker_{sticker_id}"
        query = f"""
        match
            $s isa sticker, has sticker-uid "{sticker_uid}";
            $s has filename $fn;
            $s has impression $imp;
            $s has image-hash $hash;
            $s has perceptual-hash $phash;
        get $fn, $imp, $hash, $phash;
        """
        driver = self.conn_manager.get_driver()
        db_name = self.conn_manager.database_name

        def db_read() -> dict[str, Any] | None:
            with driver.transaction(db_name, TransactionType.READ) as tx:
                # [修正] tx.query 是方法
                answers = list(tx.query(query).resolve())
                if answers:
                    answer = answers[0]
                    return {
                        "sticker_id": sticker_id,
                        "filename": answer.get("fn").as_attribute().get_value().get_string(),
                        "impression": answer.get("imp").as_attribute().get_value().get_string(),
                        "source_image_hash": answer.get("hash")
                        .as_attribute()
                        .get_value()
                        .get_string(),
                        "perceptual_hash": answer.get("phash")
                        .as_attribute()
                        .get_value()
                        .get_string(),
                    }
            return None

        try:
            return await asyncio.to_thread(db_read)
        except Exception as e:
            logger.error(f"获取表情包 '{sticker_uid}' 失败: {e}", exc_info=True)
            return None

    async def get_distinct_platforms(self) -> list[str]:
        """从表情包集合中查询出所有不重复的平台ID."""
        query = "match $p isa platform, has platform-uid $uid; get $uid; distinct $uid;"
        driver = self.conn_manager.get_driver()
        db_name = self.conn_manager.database_name

        def db_read() -> list[str]:
            with driver.transaction(db_name, TransactionType.READ) as tx:
                # [修正] tx.query 是方法
                answers = list(tx.query(query).resolve())
                return [
                    a.get("uid").as_attribute().get_value().get_string()
                    for a in answers
                    if a.get("uid")
                ]

        try:
            return await asyncio.to_thread(db_read)
        except Exception as e:
            logger.error(f"查询所有表情包平台失败: {e}", exc_info=True)
            return []