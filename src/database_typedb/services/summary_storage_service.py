import asyncio
import json
import uuid

from loguru import logger
from typedb.driver import TransactionType

from ..connection_manager import TypeDBConnectionManager
from ..models import SummaryDocument


class SummaryStorageService:
    """服务类，负责处理会话总结的数据库存储操作 (TypeDB 版本)."""

    def __init__(self, conn_manager: TypeDBConnectionManager) -> None:
        self.conn_manager = conn_manager
        logger.info("SummaryStorageService (TypeDB) 初始化完成。")

    async def save_summary(self, summary_doc: SummaryDocument) -> bool:
        """将一个会话的最终总结保存到数据库."""
        if not summary_doc.summary_text.strip():
            logger.warning("尝试保存一个空的总结，操作已取消。")
            return False

        driver = self.conn_manager.get_driver()
        db_name = self.conn_manager.database_name

        def db_write() -> bool:
            with driver.transaction(db_name, TransactionType.WRITE) as tx:
                summary_id = summary_doc._key or f"summary_{uuid.uuid4()}"
                summary_text_safe = summary_doc.summary_text.replace('"', '\\"')
                event_ids_json = json.dumps(
                    summary_doc.event_ids_covered, ensure_ascii=False
                ).replace('"', '\\"')

                insert_query = f"""
                insert $s isa summary,
                    has summary-id "{summary_id}",
                    has conversation-uid "{summary_doc.conversation_uid}",
                    has timestamp {summary_doc.timestamp},
                    has summary-text "{summary_text_safe}",
                    has event-ids-covered-json "{event_ids_json}";
                """
                tx.query.insert(insert_query).resolve()
                tx.commit()
                return True

        try:
            success = await asyncio.to_thread(db_write)
            if success:
                logger.info(
                    f"成功将总结 '{summary_doc._key}' 保存到会话 "
                    f"'{summary_doc.conversation_uid}' 的数据库中。"
                )
            return success
        except Exception as e:
            logger.error(
                f"将会话 '{summary_doc.conversation_uid}' 的总结保存到数据库时失败: {e}",
                exc_info=True,
            )
            return False
