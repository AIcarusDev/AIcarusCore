# src/database/services/event_storage_service.py
import asyncio
import json
from typing import TYPE_CHECKING, Any

from src.common.custom_logging.logging_config import get_logger
from src.common.utils import parse_entity_uid
from typedb.driver import Transaction, TransactionType

from ..core.connection_manager import TypeDBConnectionManager

if TYPE_CHECKING:
    from .entity_graph_service import EntityGraphService
logger = get_logger(__name__)


class EventStorageService:
    """事件存储服务，负责与 TypeDB 数据库交互，存储和检索事件数据."""

    def __init__(self, conn_manager: TypeDBConnectionManager) -> None:
        self.conn_manager = conn_manager
        self.entity_graph_service: EntityGraphService | None = None
        logger.info("EventStorageService (TypeDB) 初始化完成。")

    def set_entity_graph_service(self, service: "EntityGraphService") -> None:
        """Sets the entity graph service."""
        self.entity_graph_service = service

    async def save_event_document(self, event_doc_data: dict[str, Any]) -> bool:
        if not (event_id := (event_doc_data.get("_key") or event_doc_data.get("event_id"))):
            return False
        if not (platform_uid := event_doc_data.get("platform")):
            return False
        driver, db_name = self.conn_manager.get_driver(), self.conn_manager.database_name

        def db_write() -> bool:
            with driver.transaction(db_name, TransactionType.WRITE) as tx:
                if list(tx.query(f'match $e isa event, has event-id "{event_id}";').resolve()):
                    return True
                insert_parts = [
                    f'$e isa event, has event-id "{event_id}"',
                    f'has event-type "{event_doc_data.get("event_type", "unknown")}"',
                    f"has timestamp {event_doc_data.get('time', event_doc_data.get('timestamp', 0))}",
                    f'has bot-id "{event_doc_data.get("bot_id", "unknown")}"',
                    f'has status "{event_doc_data.get("status", "unread")}"',
                ]
                for key, attr in [
                    ("content", "content-json"),
                    ("user_info", "user-info-json"),
                    ("conversation_info", "conversation-info-json"),
                    ("embedding", "embedding-json"),
                    ("image_analysis", "image-analysis-json"),
                ]:
                    if val := event_doc_data.get(key):
                        insert_parts.append(
                            f'has {attr} "{json.dumps(val, ensure_ascii=False).replace('"', '\\"')}"'
                        )
                for key, attr in [
                    ("person_id_associated", "person-id-associated"),
                    ("motivation", "motivation"),
                    ("narrative_sentence", "narrative-sentence"),
                ]:
                    if val := event_doc_data.get(key):
                        insert_parts.append(f'has {attr} "{str(val).replace('"', '\\"')}"')
                tx.query(
                    f'match $p isa platform, has platform-uid "{platform_uid}"; insert {", ".join(insert_parts)}; insert (source-platform: $p, sourced-event: $e) isa event-source;'
                ).resolve()
                tx.commit()
                return True

        try:
            return await asyncio.to_thread(db_write)
        except Exception as e:
            logger.error(f"保存事件文档 '{event_id}' 失败: {e}", exc_info=True)
            return False

    async def find_event_by_image_hash(self, image_hash: str) -> dict[str, Any] | None:
        if not image_hash:
            return None
        query = f'match $e isa event, has content-json $cj; $cj like ".*{image_hash}.*"; $e has timestamp $ts; sort $ts desc; limit 1; select $e;'
        driver, db_name = self.conn_manager.get_driver(), self.conn_manager.database_name

        def db_read() -> dict[str, Any] | None:
            with driver.transaction(db_name, TransactionType.READ) as tx:
                answers = list(tx.query(query).resolve().as_concept_rows())
                if not answers or not (e_concept := answers[0].get("e")):
                    return None
                eid_query = f"match $x iid {e_concept.get_iid()}, has event-id $id; select $id;"
                eid_answers = list(tx.query(eid_query).resolve().as_concept_rows())
                if eid_answers and (id_attr := eid_answers[0].get("id")):
                    return self._get_full_event_doc_sync(tx, id_attr.as_attribute().get_value())
            return None

        try:
            return await asyncio.to_thread(db_read)
        except Exception as e:
            logger.error(f"通过图片哈希 '{image_hash}' 查找事件时失败: {e}", exc_info=True)
            return None

    def _get_full_event_doc_sync(self, tx: Transaction, event_id: str) -> dict[str, Any] | None:
        # [FIXED] 修正了 TypeQL 语法
        query = f"""
        match
            $e isa event, has event-id "{event_id}";
            $e has $attr;
            $attr isa $attr_type;
        select $attr, $attr_type;
        """
        answers = list(tx.query(query).resolve().as_concept_rows())
        if not answers:
            return None
        doc = {"_key": event_id, "event_id": event_id}
        for ans in answers:
            attr_type_concept = ans.get("attr_type")
            attr_concept = ans.get("attr")
            if attr_type_concept and attr_concept:
                label = attr_type_concept.as_attribute_type().get_label()
                py_value = attr_concept.as_attribute().get_value()
                key = label.replace("-json", "").replace("-", "_")
                if isinstance(py_value, str) and "json" in label:
                    try:
                        doc[key] = json.loads(py_value)
                    except json.JSONDecodeError:
                        doc[key] = py_value
                else:
                    doc[key] = py_value
        return doc

    async def get_recent_chat_message_documents(
        self, conversation_id: str, limit: int = 50, fetch_all_event_types: bool = False
    ) -> list[dict[str, Any]]:
        event_type_filter = "message\\\\..*" if not fetch_all_event_types else ".*"
        query = rf'match $e isa event, has conversation-info-json $ci; $ci like ".*\"conversation_id\": \"{conversation_id}\".*"; $e has event-type $et; $et like "{event_type_filter}"; $e has timestamp $ts; sort $ts desc; limit {limit}; select $e;'
        driver, db_name = self.conn_manager.get_driver(), self.conn_manager.database_name

        def db_read() -> list[dict[str, Any]]:
            with driver.transaction(db_name, TransactionType.READ) as tx:
                docs = []
                for ans in list(tx.query(query).resolve().as_concept_rows()):
                    if not (e_concept := ans.get("e")):
                        continue
                    eid_answers = list(
                        tx.query(
                            f"match $x iid {e_concept.get_iid()}, has event-id $id; select $id;"
                        )
                        .resolve()
                        .as_concept_rows()
                    )
                    if eid_answers and (id_attr := eid_answers[0].get("id")):
                        if full_doc := self._get_full_event_doc_sync(
                            tx, id_attr.as_attribute().get_value()
                        ):
                            docs.append(full_doc)
                return docs

        try:
            return await asyncio.to_thread(db_read)
        except Exception as e:
            logger.error(f"获取会话 '{conversation_id}' 的最近事件失败: {e}", exc_info=True)
            return []

    async def update_events_status(self, event_ids: list[str], new_status: str) -> bool:
        if not event_ids:
            return True
        driver, db_name = self.conn_manager.get_driver(), self.conn_manager.database_name

        def db_write() -> bool:
            with driver.transaction(db_name, TransactionType.WRITE) as tx:
                for event_id in event_ids:
                    tx.query(
                        f'match $e isa event, has event-id "{event_id}", has status $s; delete has $s of $e;'
                    ).resolve()
                    tx.query(
                        f'match $e isa event, has event-id "{event_id}"; insert $e has status "{new_status}";'
                    ).resolve()
                tx.commit()
            return True

        try:
            return await asyncio.to_thread(db_write)
        except Exception as e:
            logger.error(f"批量更新事件状态为 '{new_status}' 时失败: {e}", exc_info=True)
            return False

    async def get_all_conversation_vectors_for_iis(self) -> list[list[list[float]]]:
        query = r'match $event isa event, has event-type $type; $type like "message\\..*"; $event has embedding-json $embedding_json; $event has conversation-info-json $conv_info_json; $event has timestamp $ts;'
        driver, db_name = self.conn_manager.get_driver(), self.conn_manager.database_name

        def db_read_and_group() -> list[list[list[float]]]:
            conversations: dict[str, list[tuple[int, list[float]]]] = {}
            with driver.transaction(db_name, TransactionType.READ) as tx:
                for ans in list(tx.query(query).resolve()):
                    try:
                        ci_str, emb_str, ts = (
                            ans.get("conv_info_json").as_attribute().get_value(),
                            ans.get("embedding_json").as_attribute().get_value(),
                            ans.get("ts").as_attribute().get_value(),
                        )
                        conv_info, embedding = json.loads(ci_str), json.loads(emb_str)
                        if (conv_id := conv_info.get("conversation_id")) and isinstance(
                            embedding, list
                        ):
                            conversations.setdefault(conv_id, []).append((ts, embedding))
                    except (json.JSONDecodeError, AttributeError, KeyError):
                        continue
            return [
                sorted([vec for _, vec in events], key=lambda x: x[0])
                for _, events in conversations.items()
                if len(events) >= 2
            ]

        try:
            return await asyncio.to_thread(db_read_and_group)
        except Exception as e:
            logger.error(f"为IIS模型获取事件向量时失败: {e}", exc_info=True)
            return []

    async def get_event_by_timestamp(
        self, conversation_uid: str, timestamp: int
    ) -> dict[str, Any] | None:
        _, _, conv_native_id = parse_entity_uid(conversation_uid) or (None, None, None)
        if not conv_native_id:
            return None
        query = f'match $e isa event, has conversation-info-json $ci, has timestamp {timestamp}; $ci like \'.*"conversation_id": "{conv_native_id}".*\'; $e has event-id $eid; select $eid; limit 1;'
        driver, db_name = self.conn_manager.get_driver(), self.conn_manager.database_name

        def db_read() -> dict | None:
            with driver.transaction(db_name, TransactionType.READ) as tx:
                answers = list(tx.query(query).resolve().as_concept_rows())
                if answers and (eid_attr := answers[0].get("eid")):
                    return self._get_full_event_doc_sync(tx, eid_attr.as_attribute().get_value())
            return None

        try:
            return await asyncio.to_thread(db_read)
        except Exception as e:
            logger.error(f"通过时间戳 {timestamp} 获取事件失败: {e}", exc_info=True)
            return None

    async def get_unread_count(
        self, conversation_uid: str, self_bot_ids: dict[str, str]
    ) -> dict[str, Any]:
        if not self.entity_graph_service:
            return {"unread_count": 0, "has_high_priority": False}
        last_read_ts = await self.entity_graph_service.get_conversation_last_read_timestamp(
            conversation_uid
        )
        _, _, conv_native_id = parse_entity_uid(conversation_uid) or (None, None, None)
        if not conv_native_id:
            return {"unread_count": 0, "has_high_priority": False}
        query = f'match $e isa event, has conversation-info-json $ci, has timestamp $ts; $ci like \'.*"conversation_id": "{conv_native_id}".*\'; $ts > {int(last_read_ts)}; $e has content-json $content;'
        driver, db_name = self.conn_manager.get_driver(), self.conn_manager.database_name

        def db_read_and_process() -> dict[str, Any]:
            unread_count, has_high_priority = 0, False
            with driver.transaction(db_name, TransactionType.READ) as tx:
                answers = list(tx.query(query).resolve())
                unread_count = len(answers)
                if not has_high_priority:
                    all_my_bot_ids = set(self_bot_ids.values())
                    for ans in answers:
                        content_str = ans.get("content").as_value().get_string()
                        if any(
                            f'"user_id": "{bot_id}"' in content_str for bot_id in all_my_bot_ids
                        ) and any(
                            tag in content_str for tag in ['"type": "at"', '"type": "quote"']
                        ):
                            has_high_priority = True
                            break
            return {"unread_count": unread_count, "has_high_priority": has_high_priority}

        try:
            return await asyncio.to_thread(db_read_and_process)
        except Exception as e:
            logger.error(f"计算会话 {conversation_uid} 未读数失败: {e}", exc_info=True)
            return {"unread_count": 0, "has_high_priority": False}
