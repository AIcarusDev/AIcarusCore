import asyncio
import time
from typing import Any

from src.common.custom_logging.logging_config import get_logger
from src.services.database.utils import compare_phashes
from typedb.driver import Transaction, TransactionType, TypeDBDriverException

from ..core.connection_manager import TypeDBConnectionManager

logger = get_logger(__name__)


class StickerStorageService:
    """Service for managing sticker storage operations in TypeDB.

    This service provides methods for adding, retrieving, updating, and removing
    stickers from a TypeDB database. It handles sticker metadata including
    filenames, impressions, image hashes, and perceptual hashes for similarity
    detection.

    Attributes:
    ----------
    conn_manager : TypeDBConnectionManager
        The connection manager for TypeDB database operations.

    Methods:
    -------
    add_sticker(platform_id, filename, impression, source_image_hash, perceptual_hash)
        Add a new sticker to a specific platform.
    find_similar_sticker_by_phash(platform_id, phash_to_check, tolerance=5)
        Find a sticker with similar perceptual hash on a platform.
    remove_sticker(platform_id, sticker_id)
        Remove a sticker from a specific platform.
    edit_impression(platform_id, sticker_id, new_impression)
        Edit the impression text of a sticker.
    get_all_stickers(platform_id)
        Get all stickers for a specific platform.
    get_sticker_by_id(platform_id, sticker_id)
        Get a sticker by its ID from a specific platform.
    get_distinct_platforms()
        Get all distinct platform IDs that have stickers.
    """

    def __init__(self, conn_manager: TypeDBConnectionManager) -> None:
        self.conn_manager = conn_manager
        logger.info("StickerStorageService (TypeDB gRPC) 初始化完成。")

    def _get_next_sticker_id_num(self, platform_id: str, tx: Transaction) -> int:
        query = f"""
        match
            $p isa platform, has platform-uid "{platform_id}";
            (hosting-platform: $p, hosted-asset: $s) isa platform-asset;
            $s isa sticker, has sticker-id $sid;
        select $sid;
        """
        answers = list(tx.query(query).resolve().as_concept_rows())
        max_id_num = 0
        if answers:
            for answer in answers:
                if sid_attr := answer.get("sid"):
                    sid_val = sid_attr.as_attribute().get_value()
                    max_id_num = max(max_id_num, sid_val)
        return max_id_num + 1

    async def add_sticker(
        self,
        platform_id: str,
        filename: str,
        impression: str,
        source_image_hash: str,
        perceptual_hash: str,
    ) -> dict[str, Any] | None:
        """Add a new sticker to a specific platform.

        Parameters
        ----------
        platform_id : str
            The platform identifier where the sticker will be added.
        filename : str
            The filename of the sticker image.
        impression : str
            The impression or description text for the sticker.
        source_image_hash : str
            The hash of the original source image.
        perceptual_hash : str
            The perceptual hash used for similarity detection.

        Returns:
        -------
        dict[str, Any] | None
            A dictionary containing sticker details (sticker_id, sticker_uid, filename,
            impression, added_at) if successful, None otherwise.
        """
        driver = self.conn_manager.get_driver()
        db_name = self.conn_manager.database_name

        def db_write() -> dict[str, Any] | None:
            with driver.transaction(db_name, TransactionType.WRITE) as tx:
                next_id_num = self._get_next_sticker_id_num(platform_id, tx)
                next_sticker_id_str = f"{next_id_num:03d}"
                sticker_uid = f"{platform_id}_sticker_{next_sticker_id_str}"
                added_at_ts = int(time.time())
                query = f"""
                match $p isa platform, has platform-uid "{platform_id}";
                insert $s isa sticker,
                    has sticker-uid "{sticker_uid}",
                    has sticker-id {next_id_num},
                    has filename "{filename.replace('"', '\\"')}",
                    has impression "{impression.replace('"', '\\"')}",
                    has image-hash "{source_image_hash}",
                    has perceptual-hash "{perceptual_hash}",
                    has added-at {added_at_ts};
                insert (hosting-platform: $p, hosted-asset: $s) isa platform-asset;
                """
                tx.query(query).resolve()
                tx.commit()
                return {
                    "sticker_id": next_sticker_id_str,
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
        """Find a sticker with similar perceptual hash on a specific platform.

        Parameters
        ----------
        platform_id : str
            The platform identifier to search within.
        phash_to_check : str
            The perceptual hash to compare against existing stickers.
        tolerance : int, optional
            The maximum allowed difference between hashes, by default 5.

        Returns:
        -------
        dict[str, Any] | None
            A dictionary containing sticker_uid and phash if a similar sticker is found,
            None otherwise.
        """
        query = f"""
        match
            $p isa platform, has platform-uid "{platform_id}";
            (hosting-platform: $p, hosted-asset: $s) isa platform-asset;
            $s isa sticker, has sticker-uid $uid, has perceptual-hash $phash;
        select $uid, $phash;
        """
        driver = self.conn_manager.get_driver()
        db_name = self.conn_manager.database_name

        def db_read_and_compare() -> dict[str, Any] | None:
            with driver.transaction(db_name, TransactionType.READ) as tx:
                answers = list(tx.query(query).resolve().as_concept_rows())
                for answer in answers:
                    if (uid_attr := answer.get("uid")) and (phash_attr := answer.get("phash")):
                        candidate_uid = uid_attr.as_attribute().get_value()
                        candidate_phash = phash_attr.as_attribute().get_value()
                        if compare_phashes(phash_to_check, candidate_phash, tolerance):
                            return {"sticker_uid": candidate_uid, "phash": candidate_phash}
            return None

        try:
            return await asyncio.to_thread(db_read_and_compare)
        except Exception as e:
            logger.error(f"查找相似表情包时失败: {e}", exc_info=True)
            return None

    async def remove_sticker(self, platform_id: str, sticker_id: str) -> bool:
        """Remove a sticker from a specific platform.

        Parameters
        ----------
        platform_id : str
            The platform identifier where the sticker belongs.
        sticker_id : str
            The unique identifier of the sticker within the platform.

        Returns:
        -------
        bool
            True if the sticker was successfully removed, False otherwise.
        """
        sticker_uid = f"{platform_id}_sticker_{sticker_id}"
        query = f'match $s isa sticker, has sticker-uid "{sticker_uid}"; delete $s;'
        driver = self.conn_manager.get_driver()
        db_name = self.conn_manager.database_name

        def db_write() -> bool:
            with driver.transaction(db_name, TransactionType.WRITE) as tx:
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
        """Edit the impression of a sticker.

        Parameters
        ----------
        platform_id : str
            The platform identifier where the sticker belongs.
        sticker_id : str
            The unique identifier of the sticker within the platform.
        new_impression : str
            The new impression text to set for the sticker.

        Returns:
        -------
        bool
            True if the impression was successfully updated, False otherwise.
        """
        sticker_uid = f"{platform_id}_sticker_{sticker_id}"
        new_impression_safe = new_impression.replace('"', '\\"')
        driver = self.conn_manager.get_driver()
        db_name = self.conn_manager.database_name

        def db_update() -> bool:
            with driver.transaction(db_name, TransactionType.WRITE) as tx:
                delete_query = f"""
                match
                    $s isa sticker, has sticker-uid "{sticker_uid}";
                    $s has impression $old_imp;
                delete
                    has $old_imp of $s;
                """
                tx.query(delete_query).resolve()
                insert_query = f"""
                match
                    $s isa sticker, has sticker-uid "{sticker_uid}";
                insert
                    $s has impression "{new_impression_safe}";
                """
                try:
                    tx.query(insert_query).resolve()
                except TypeDBDriverException as e:
                    logger.warning(f"尝试编辑一个不存在的表情包印象: {sticker_uid} - {e}")
                    tx.close()
                    return False
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
        """Get all stickers for a specific platform.

        Parameters
        ----------
        platform_id : str
            The platform identifier to retrieve stickers from.

        Returns:
        -------
        list[dict]
            A list of dictionaries containing sticker details (sticker_id, filename,
            impression, image_hash) sorted by sticker ID in ascending order.
        """
        query = f"""
        match
            $p isa platform, has platform-uid "{platform_id}";
            (hosting-platform: $p, hosted-asset: $s) isa platform-asset;
            $s isa sticker,
                has sticker-id $sid,
                has filename $fn,
                has impression $imp,
                has image-hash $hash;
        select $sid, $fn, $imp, $hash;
        sort $sid asc;
        """
        driver = self.conn_manager.get_driver()
        db_name = self.conn_manager.database_name

        def db_read() -> list[dict]:
            stickers = []
            with driver.transaction(db_name, TransactionType.READ) as tx:
                answers = list(tx.query(query).resolve().as_concept_rows())
                for answer in answers:
                    stickers.append(
                        {
                            "sticker_id": f"{answer.get('sid').as_attribute().get_value():03d}",
                            "filename": answer.get("fn").as_attribute().get_value(),
                            "impression": answer.get("imp").as_attribute().get_value(),
                            "image_hash": answer.get("hash").as_attribute().get_value(),
                        }
                    )
            return stickers

        try:
            return await asyncio.to_thread(db_read)
        except Exception as e:
            logger.error(f"获取平台 '{platform_id}' 的所有表情包失败: {e}", exc_info=True)
            return []

    async def get_sticker_by_id(self, platform_id: str, sticker_id: str) -> dict[str, Any] | None:
        """Get a sticker by its ID from a specific platform.

        Parameters
        ----------
        platform_id : str
            The platform identifier where the sticker belongs.
        sticker_id : str
            The unique identifier of the sticker within the platform.

        Returns:
        -------
        dict[str, Any] | None
            A dictionary containing sticker details (sticker_id, filename, impression,
            source_image_hash, perceptual_hash) if found, None otherwise.
        """
        sticker_uid = f"{platform_id}_sticker_{sticker_id}"
        query = f"""
        match $s isa sticker, has sticker-uid "{sticker_uid}";
        fetch {{
            "filename": $s.filename,
            "impression": $s.impression,
            "source_image_hash": $s.image-hash,
            "perceptual_hash": $s.perceptual-hash
        }};
        """
        driver = self.conn_manager.get_driver()
        db_name = self.conn_manager.database_name

        def db_read() -> dict[str, Any] | None:
            with driver.transaction(db_name, TransactionType.READ) as tx:
                answers = list(tx.query(query).resolve().as_concept_documents())
                if answers:
                    answer = answers[0]
                    return {
                        "sticker_id": sticker_id,
                        "filename": answer.get("filename"),
                        "impression": answer.get("impression"),
                        "source_image_hash": answer.get("source_image_hash"),
                        "perceptual_hash": answer.get("perceptual_hash"),
                    }
            return None

        try:
            return await asyncio.to_thread(db_read)
        except Exception as e:
            logger.error(f"获取表情包 '{sticker_uid}' 失败: {e}", exc_info=True)
            return None

    async def get_distinct_platforms(self) -> list[str]:
        """Get all distinct platform IDs that have stickers.

        Returns:
        -------
        list[str]
            A list of platform UIDs that have associated stickers.
        """
        query = """
        match
            $p isa platform, has platform-uid $puid;
            $s isa sticker;
            (hosting-platform: $p, hosted-asset: $s) isa platform-asset;
        select $puid; distinct;
        """
        driver = self.conn_manager.get_driver()
        db_name = self.conn_manager.database_name

        def db_read() -> list[str]:
            with driver.transaction(db_name, TransactionType.READ) as tx:
                answers = list(tx.query(query).resolve().as_concept_rows())
                return [a.get("puid").as_attribute().get_value() for a in answers if a.get("puid")]

        try:
            return await asyncio.to_thread(db_read)
        except Exception as e:
            logger.error(f"查询所有表情包平台失败: {e}", exc_info=True)
            return []
