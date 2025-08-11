# src/database/services/sticker_storage_service.py
import time
from typing import Any

from src.common.custom_logging.logging_config import get_logger
from src.database import ArangoDBConnectionManager, CoreDBCollections
from src.database.models import StickerDocument

logger = get_logger(__name__)


class StickerStorageService:
    """服务类，负责管理表情包元数据的存储和检索."""

    def __init__(self, conn_manager: ArangoDBConnectionManager) -> None:
        self.conn_manager = conn_manager
        self.collection_name = CoreDBCollections.STICKER_COLLECTION
        logger.info(f"StickerStorageService 初始化，操作集合 '{self.collection_name}'。")

    async def _get_next_sticker_id(self, platform: str) -> str:
        """原子性地获取下一个可用的表情包ID (e.g., "001", "002")."""
        query = """
            LET max_id = MAX(
                FOR s IN @@collection
                FILTER s.platform == @platform
                RETURN TO_NUMBER(s.sticker_id)
            )
            RETURN { next_id: (max_id == null ? 1 : max_id + 1) }
        """
        bind_vars = {"@collection": self.collection_name, "platform": platform}
        result = await self.conn_manager.execute_query(query, bind_vars)
        next_id_num = result[0]["next_id"] if result and result[0] else 1
        return f"{next_id_num:03d}"

    async def add_sticker(
        self, platform: str, filename: str, impression: str, source_image_hash: str
    ) -> StickerDocument | None:
        """添加一个新的表情包元数据记录."""
        try:
            next_id = await self._get_next_sticker_id(platform)
            sticker_doc = StickerDocument(
                _key=f"{platform}_{next_id}",
                sticker_id=next_id,
                platform=platform,
                filename=filename,
                impression=impression,
                source_image_hash=source_image_hash,
                added_at=int(time.time() * 1000),
            )
            collection = await self.conn_manager.get_collection(self.collection_name)
            await collection.insert(sticker_doc.to_dict())
            logger.info(f"新表情包 '{sticker_doc.sticker_id}' 已添加到数据库。")
            return sticker_doc
        except Exception as e:
            logger.error(f"添加表情包元数据失败: {e}", exc_info=True)
            return None

    async def remove_sticker(self, platform: str, sticker_id: str) -> bool:
        """根据ID移除一个表情包元数据记录."""
        try:
            collection = await self.conn_manager.get_collection(self.collection_name)
            await collection.delete(f"{platform}_{sticker_id}")
            logger.info(f"表情包 '{sticker_id}' 已从数据库移除。")
            return True
        except Exception as e:
            logger.error(f"移除表情包 '{sticker_id}' 失败: {e}", exc_info=True)
            return False

    async def edit_impression(self, platform: str, sticker_id: str, new_impression: str) -> bool:
        """编辑指定表情包的印象描述."""
        try:
            collection = await self.conn_manager.get_collection(self.collection_name)
            await collection.update(
                {"_key": f"{platform}_{sticker_id}", "impression": new_impression}
            )
            logger.info(f"{platform}表情包 '{sticker_id}' 的印象已更新。")
            return True
        except Exception as e:
            logger.error(f"编辑{platform}表情包 '{sticker_id}' 印象失败: {e}", exc_info=True)
            return False

    async def get_all_stickers(self, platform: str) -> list[dict[str, Any]]:
        """获取所有表情包的元数据，按ID升序排列."""
        query = """
            FOR s IN @@collection
            FILTER s.platform == @platform
            SORT s.sticker_id ASC
            RETURN s
        """
        bind_vars = {"@collection": self.collection_name, "platform": platform}
        results = await self.conn_manager.execute_query(query, bind_vars)
        return results if results is not None else []

    async def get_sticker_by_id(self, platform: str, sticker_id: str) -> dict[str, Any] | None:
        """根据ID获取单个表情包的元数据."""
        try:
            collection = await self.conn_manager.get_collection(self.collection_name)
            return await collection.get(f"{platform}_{sticker_id}")
        except Exception as e:
            logger.error(f"获取表情包 '{sticker_id}' 失败: {e}", exc_info=True)
            return None
