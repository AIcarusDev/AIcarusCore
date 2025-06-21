# src/database/services/summary_storage_service.py
import asyncio
import time
import uuid

from src.common.custom_logging.logger_manager import get_logger
from src.database.core.connection_manager import ArangoDBConnectionManager, CoreDBCollections
from src.database.models import ConversationSummaryDocument

logger = get_logger("AIcarusCore.DB.SummaryStorageService")


class SummaryStorageService:
    """
    服务类，负责处理会话总结的数据库存储操作。
    """

    def __init__(self, db_manager: ArangoDBConnectionManager) -> None:
        """
        初始化服务。
        :param db_manager: ArangoDBManager 的实例。
        """
        self.db_manager = db_manager
        self.summaries_collection = None  # 在异步方法中动态获取

    async def save_summary(
        self,
        conversation_id: str,
        summary_text: str,
        platform: str,
        bot_id: str,
        event_ids_covered: list[str],
    ) -> bool:
        """
        将一个会话的最终总结保存到数据库。

        :param conversation_id: 会话的ID。
        :param summary_text: 总结的文本内容。
        :param platform: 会话所属平台。
        :param bot_id: 处理此会话的机器人ID。
        :param event_ids_covered: 此总结所覆盖的事件ID列表。
        :return: 如果保存成功，返回 True，否则返回 False。
        """
        # 在异步方法中动态获取集合，确保操作的原子性和异步正确性
        collection_name = CoreDBCollections.CONVERSATION_SUMMARIES
        try:
            self.summaries_collection = await self.db_manager.get_collection(collection_name)
            if not self.summaries_collection:
                logger.error(f"无法获取 '{collection_name}' 集合，操作中止。")
                return False
        except Exception as e:
            logger.error(f"尝试保存总结时，无法获取 '{collection_name}' 集合: {e}", exc_info=True)
            return False

        if not summary_text or not summary_text.strip():
            logger.warning("尝试保存一个空的总结，操作已取消。")
            return False

        summary_id = f"summary_{uuid.uuid4()}"
        timestamp_ms = int(time.time() * 1000)

        summary_doc = ConversationSummaryDocument(
            _key=summary_id,
            summary_id=summary_id,
            conversation_id=conversation_id,
            timestamp=timestamp_ms,
            platform=platform,
            bot_id=bot_id,
            summary_text=summary_text,
            event_ids_covered=event_ids_covered,
        )

        try:
            doc_to_insert = summary_doc.to_dict()
            # 使用 asyncio.to_thread 来执行同步的 insert 操作，避免阻塞事件循环
            await asyncio.to_thread(self.summaries_collection.insert, doc_to_insert)
            logger.info(f"成功将总结 '{summary_id}' 保存到会话 '{conversation_id}' 的数据库中。")
            return True
        except Exception as e:
            logger.error(f"将会话 '{conversation_id}' 的总结保存到数据库时失败: {e}", exc_info=True)
            return False
