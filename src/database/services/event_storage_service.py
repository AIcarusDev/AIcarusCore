import asyncio
import json
from typing import Any

from loguru import logger
from typedb.driver import TransactionType

from ..core.connection_manager import TypeDBConnectionManager


class EventStorageService:
    """服务类，负责所有与事件（Events）相关的存储操作 (TypeDB 版本)."""

    def __init__(self, conn_manager: TypeDBConnectionManager) -> None:
        self.conn_manager = conn_manager
        logger.info("EventStorageService (TypeDB) 初始化完成。")

    async def save_event_document(self, event_doc_data: dict[str, Any]) -> bool:
        """保存事件文档到数据库."""
        if not event_doc_data or not isinstance(event_doc_data, dict):
            logger.warning("无效的 'event_doc_data' (空或非字典类型)。无法保存事件。")
            return False

        event_id = event_doc_data.get("_key") or event_doc_data.get("event_id")
        if not event_id:
            logger.error("事件文档缺少 '_key' 或 'event_id'。")
            return False

        driver = self.conn_manager.get_driver()
        db_name = self.conn_manager.database_name

        def db_write() -> bool:
            with driver.transaction(db_name, TransactionType.WRITE) as tx:
                match_query = f'match $e isa event, has event-id "{event_id}"; get $e;'
                answers = list(tx.query.get(match_query).resolve())
                if answers:
                    logger.warning(f"尝试插入已存在的事件 Event ID: {event_id}。操作被跳过。")
                    return True

                insert_parts = [
                    f'$e isa event, has event-id "{event_id}"',
                    f'has event-type "{event_doc_data.get("event_type", "unknown")}"',
                    f"has timestamp {event_doc_data.get('timestamp', 0)}",
                    f'has platform "{event_doc_data.get("platform", "unknown")}"',
                    f'has bot-id "{event_doc_data.get("bot_id", "unknown")}"',
                    f'has status "{event_doc_data.get("status", "unread")}"',
                ]

                for key, attr_name in [
                    ("content", "content-json"),
                    ("user_info", "user-info-json"),
                    ("conversation_info", "conversation-info-json"),
                    ("embedding", "embedding-json"),
                    ("image_analysis", "image-analysis-json"),
                ]:
                    if value := event_doc_data.get(key):
                        json_str = json.dumps(value, ensure_ascii=False).replace('"', '\\"')
                        insert_parts.append(f'has {attr_name} "{json_str}"')

                for key, attr_name in [
                    ("person_id_associated", "person-id-associated"),
                    ("motivation", "motivation"),
                    ("narrative_sentence", "narrative-sentence"),
                ]:
                    if value := event_doc_data.get(key):
                        safe_value = str(value).replace('"', '\\"')
                        insert_parts.append(f'has {attr_name} "{safe_value}"')

                insert_query = "insert " + ",\n".join(insert_parts) + ";"
                tx.query.insert(insert_query).resolve()
                tx.commit()
                return True

        try:
            success = await asyncio.to_thread(db_write)
            if success:
                logger.info(f"事件文档 '{event_id}' 已保存。")
            return success
        except Exception as e:
            logger.error(f"保存事件文档 '{event_id}' 失败: {e}", exc_info=True)
            return False

    async def find_event_by_image_hash(self, image_hash: str) -> dict[str, Any] | None:
        """根据图片内容的哈希值查找包含该图片的最新事件."""
        if not image_hash:
            return None

        query = f"""
        match
            $e isa event, has content-json $cj;
            $cj like ".*{image_hash}.*";
            $e has timestamp $ts;
        get $e, $ts;
        sort $ts desc; limit 1;
        """
        driver = self.conn_manager.get_driver()
        db_name = self.conn_manager.database_name

        def db_read() -> dict[str, Any] | None:
            with driver.transaction(db_name, TransactionType.READ) as tx:
                answers = list(tx.query.get(query).resolve())
                if answers:
                    event_concept = answers[0].get("e")
                    # 修正：在一个事务内完成后续查询
                    event_id_answers = list(
                        tx.query.get(
                            f"match $x iid {event_concept.get_iid()}; $x has event-id $id; get $id;"
                        ).resolve()
                    )
                    if event_id_answers:
                        event_id = (
                            event_id_answers[0].get("id").as_attribute().get_value().get_string()
                        )
                        logger.debug(f"通过图片哈希 '{image_hash}' 成功找到事件 '{event_id}'。")
                        return {"_key": event_id}
            return None

        try:
            return await asyncio.to_thread(db_read)
        except Exception as e:
            logger.error(f"通过图片哈希 '{image_hash}' 查找事件时失败: {e}", exc_info=True)
            return None

    async def get_recent_chat_message_documents(
        self, conversation_id: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        """获取指定会话最近的聊天消息事件文档."""
        # 修正点：在 f-string 前面加上 'r'
        query = rf"""
        match
            $e isa event, has conversation-info-json $ci;
            $ci like '.*"conversation_id": "{conversation_id}".*';
            $e has event-type $et; $et like "message\..*";
            $e has timestamp $ts;
        get $e, $ts;
        sort $ts desc; limit {limit};
        """
        driver = self.conn_manager.get_driver()
        db_name = self.conn_manager.database_name

        def db_read() -> list[dict[str, Any]]:
            with driver.transaction(db_name, TransactionType.READ) as tx:
                answers = list(tx.query.get(query).resolve())
                docs = []
                for ans in answers:
                    event_concept = ans.get("e")
                    # 修正：在一个事务内完成后续查询
                    event_id_answers = list(
                        tx.query.get(
                            f"match $x iid {event_concept.get_iid()}; $x has event-id $id; get $id;"
                        ).resolve()
                    )
                    if event_id_answers:
                        event_id = (
                            event_id_answers[0].get("id").as_attribute().get_value().get_string()
                        )
                        docs.append({"_key": event_id})  # Simplified
                return docs

        try:
            return await asyncio.to_thread(db_read)
        except Exception as e:
            logger.error(f"获取会话 '{conversation_id}' 的最近事件失败: {e}", exc_info=True)
            return []

    async def update_events_status(self, event_ids: list[str], new_status: str) -> bool:
        """批量更新指定ID列表的事件的 status 字段."""
        if not event_ids:
            return True

        driver = self.conn_manager.get_driver()
        db_name = self.conn_manager.database_name

        def db_write() -> bool:
            with driver.transaction(db_name, TransactionType.WRITE) as tx:
                for event_id in event_ids:
                    match_query = f'match $e isa event, has event-id "{event_id}", has status $old_status; get $e, $old_status;'  # noqa: E501
                    answers = list(tx.query.get(match_query).resolve())
                    if not answers:
                        logger.warning(f"尝试更新状态时未找到事件: {event_id}")
                        continue

                    delete_query = f'match $e isa event, has event-id "{event_id}", has status $s; delete $e has $s;'  # noqa: E501
                    tx.query.delete(delete_query).resolve()

                    insert_query = f'match $e isa event, has event-id "{event_id}"; insert $e has status "{new_status}";'  # noqa: E501
                    tx.query.insert(insert_query).resolve()
                tx.commit()
            return True

        try:
            success = await asyncio.to_thread(db_write)
            if success:
                logger.info(f"成功将 {len(event_ids)} 个事件的状态更新为 '{new_status}'。")
            return success
        except Exception as e:
            logger.error(f"批量更新事件状态为 '{new_status}' 时失败: {e}", exc_info=True)
            return False
