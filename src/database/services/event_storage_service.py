# src/database/services/event_storage_service.py
import asyncio
import json
import time
from typing import Any

from src.common.custom_logging.logging_config import get_logger
from typedb.driver import TransactionType, Transaction
from ..core.connection_manager import TypeDBConnectionManager

logger = get_logger(__name__)


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
                # [修正] tx.query 是方法
                answers = list(tx.query(match_query).resolve())
                if answers:
                    logger.warning(f"尝试插入已存在的事件 Event ID: {event_id}。操作被跳过。")
                    return True

                insert_parts = [
                    f'$e isa event, has event-id "{event_id}"',
                    f'has event-type "{event_doc_data.get("event_type", "unknown")}"',
                    f"has timestamp {event_doc_data.get('time', 0)}", # 协议对象用 time
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
                # [修正] tx.query 是方法
                tx.query(insert_query).resolve()
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
                # [修正] tx.query 是方法
                answers = list(tx.query(query).resolve())
                if not answers:
                    return None
                
                event_concept = answers[0].get("e")
                if not event_concept: return None

                # 在同一个事务内完成后续查询
                event_id_query = f"match $x iid {event_concept.get_iid()}; $x has event-id $id; get $id;"
                # [修正] tx.query 是方法
                event_id_answers = list(tx.query(event_id_query).resolve())

                if event_id_answers and (id_attr := event_id_answers[0].get("id")):
                    event_id = id_attr.as_attribute().get_value().get_string()
                    logger.debug(f"通过图片哈希 '{image_hash}' 成功找到事件 '{event_id}'。")
                    # 返回一个包含所有属性的完整文档
                    return self._get_full_event_doc_sync(tx, event_id)
            return None

        try:
            return await asyncio.to_thread(db_read)
        except Exception as e:
            logger.error(f"通过图片哈希 '{image_hash}' 查找事件时失败: {e}", exc_info=True)
            return None

    def _get_full_event_doc_sync(self, tx: Transaction, event_id: str) -> dict[str, Any] | None:
        """[Helper] 在一个事务内，根据 event-id 获取完整的事件文档字典."""
        query = f"""
        match $e isa event, has event-id "{event_id}";
        $e has $attr;
        $attr isa attribute;
        $attr has $value;
        $attr_type = $attr.type;
        $attr_type has label $attr_label;
        get $attr_label, $value;
        """
        # [修正] tx.query 是方法
        answers = list(tx.query(query).resolve())
        if not answers:
            return None

        doc = {"_key": event_id, "event_id": event_id}
        for ans in answers:
            label = ans.get("attr_label").as_attribute().get_value().get_string()
            value_concept = ans.get("value")
            
            # 这是一个简化的值提取逻辑，需要根据实际值类型进行扩展
            py_value = value_concept.get_value()
            
            if label.endswith("-json"):
                key = label.replace("-json", "")
                doc[key] = json.loads(py_value)
            else:
                doc[label.replace("-", "_")] = py_value
        return doc

    async def get_recent_chat_message_documents(
        self, conversation_id: str, limit: int = 50, fetch_all_event_types: bool = False
    ) -> list[dict[str, Any]]:
        """获取指定会话最近的聊天消息事件文档."""
        event_type_filter = 'message\\..*' if not fetch_all_event_types else '.*'
        
        query = rf"""
        match
            $e isa event, has conversation-info-json $ci;
            $ci like '.*"conversation_id": "{conversation_id}".*';
            $e has event-type $et; $et like "{event_type_filter}";
            $e has timestamp $ts;
        get $e, $ts;
        sort $ts desc; limit {limit};
        """
        driver = self.conn_manager.get_driver()
        db_name = self.conn_manager.database_name

        def db_read() -> list[dict[str, Any]]:
            with driver.transaction(db_name, TransactionType.READ) as tx:
                # [修正] tx.query 是方法
                answers = list(tx.query(query).resolve())
                docs = []
                for ans in answers:
                    event_concept = ans.get("e")
                    if not event_concept: continue

                    event_id_query = f"match $x iid {event_concept.get_iid()}; $x has event-id $id; get $id;"
                    # [修正] tx.query 是方法
                    event_id_answers = list(tx.query(event_id_query).resolve())
                    if event_id_answers and (id_attr := event_id_answers[0].get("id")):
                        event_id = id_attr.as_attribute().get_value().get_string()
                        full_doc = self._get_full_event_doc_sync(tx, event_id)
                        if full_doc:
                            docs.append(full_doc)
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
                    delete_query = f'match $e isa event, has event-id "{event_id}", has status $s; delete $e has $s;'
                    # [修正] tx.query 是方法
                    tx.query(delete_query).resolve()

                    insert_query = f'match $e isa event, has event-id "{event_id}"; insert $e has status "{new_status}";'
                    # [修正] tx.query 是方法
                    tx.query(insert_query).resolve()
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
            
    async def get_all_conversation_vectors_for_iis(self) -> list[list[list[float]]]:
        """专门为IIS模型训练获取所有对话的向量序列."""
        query = """
        match
            $event isa event, has event-type $type;
            $type like "message\\..*";
            $event has embedding-json $embedding_json;
            $event has conversation-info-json $conv_info_json;
            $event has timestamp $ts;
        get $embedding_json, $conv_info_json, $ts;
        """
        driver = self.conn_manager.get_driver()
        db_name = self.conn_manager.database_name

        def db_read_and_group() -> list[list[list[float]]]:
            conversations: dict[str, list[tuple[int, list[float]]]] = {}
            with driver.transaction(db_name, TransactionType.READ) as tx:
                # [修正] tx.query 是方法
                answers = list(tx.query(query).resolve())
                for ans in answers:
                    try:
                        conv_info_str = ans.get("conv_info_json").as_attribute().get_value().get_string()
                        embedding_str = ans.get("embedding_json").as_attribute().get_value().get_string()
                        ts = ans.get("ts").as_attribute().get_value().get_integer()

                        conv_info = json.loads(conv_info_str)
                        embedding = json.loads(embedding_str)
                        
                        conv_id = conv_info.get("conversation_id")
                        if conv_id and isinstance(embedding, list):
                            if conv_id not in conversations:
                                conversations[conv_id] = []
                            conversations[conv_id].append((ts, embedding))
                    except (json.JSONDecodeError, AttributeError, KeyError) as e:
                        logger.warning(f"解析事件向量时跳过一个无效条目: {e}")
                        continue
            
            sorted_conversations = []
            for conv_id, events in conversations.items():
                if len(events) >= 2:
                    events.sort(key=lambda x: x[0])
                    sorted_conversations.append([vec for ts, vec in events])
            
            return sorted_conversations

        try:
            return await asyncio.to_thread(db_read_and_group)
        except Exception as e:
            logger.error(f"为IIS模型获取事件向量时失败: {e}", exc_info=True)
            return []