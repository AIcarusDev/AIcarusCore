# src/database/services/thought_storage_service.py
import asyncio
import datetime
import json
import time
import uuid
from typing import Any
from src.common.custom_logging.logging_config import get_logger
from typedb.driver import TransactionType
from ..core.connection_manager import TypeDBConnectionManager
from ..models import ThoughtChainDocument

logger = get_logger(__name__)

LATEST_THOUGHT_POINTER_KEY = "latest_thought_pointer"


class ThoughtStorageService:
    """服务类，负责处理思想链相关的数据库存储操作 (TypeDB 版本)."""

    def __init__(self, conn_manager: TypeDBConnectionManager) -> None:
        self.conn_manager = conn_manager
        logger.info("ThoughtStorageService (TypeDB) 初始化完成。")

    async def save_thought_and_link(self, thought_data: ThoughtChainDocument) -> str | None:
        """使用单个原子事务保存思想点并更新思想链."""
        driver = self.conn_manager.get_driver()
        db_name = self.conn_manager.database_name

        def db_write() -> str | None:
            with driver.transaction(db_name, TransactionType.WRITE) as tx:
                get_pointer_query = f"""
                match $p isa system-pointer, has pointer-name "{LATEST_THOUGHT_POINTER_KEY}";
                $p has target-key $key; get $key;
                """
                # [修正] tx.query 是方法
                answers = list(tx.query(get_pointer_query).resolve())
                last_thought_key = (
                    answers[0].get("key").as_attribute().get_value().get_string()
                    if answers
                    else None
                )

                new_key = thought_data._key
                insert_parts = [
                    f'$t isa thought-chain-node, has thought-id "{new_key}"',
                    f"has timestamp {int(datetime.datetime.fromisoformat(thought_data.timestamp).timestamp() * 1000)}",
                    f'has mood "{thought_data.mood.replace('"', '\\"')}"',
                    f'has think "{thought_data.think.replace('"', '\\"')}"',
                ]
                if thought_data.intent:
                    insert_parts.append(f'has intent "{thought_data.intent.replace('"', '\\"')}"')
                if thought_data.source_type:
                    insert_parts.append(f'has source-type "{thought_data.source_type}"')
                if thought_data.source_id:
                    insert_parts.append(f'has source-id "{thought_data.source_id}"')
                if thought_data.action_id:
                    insert_parts.append(f'has action-id "{thought_data.action_id}"')
                if thought_data.action_payload:
                    payload_str = json.dumps(
                        thought_data.action_payload, ensure_ascii=False
                    ).replace('"', '\\"')
                    insert_parts.append(f'has action-payload-json "{payload_str}"')

                insert_thought_query = "insert " + ",\n".join(insert_parts) + ";"
                # [修正] tx.query 是方法
                tx.query(insert_thought_query).resolve()

                if last_thought_key:
                    link_query = f"""
                    match
                        $prev isa thought-chain-node, has thought-id "{last_thought_key}";
                        $curr isa thought-chain-node, has thought-id "{new_key}";
                    insert
                        (preceding-thought: $prev, succeeding-thought: $curr) isa precedes-thought;
                    """
                    # [修正] tx.query 是方法
                    tx.query(link_query).resolve()

                if last_thought_key:
                    update_pointer_query = f"""
                    match
                        $p isa system-pointer, has pointer-name "{LATEST_THOUGHT_POINTER_KEY}";
                        $p has target-key $old_key;
                    delete $p has $old_key;
                    insert $p has target-key "{new_key}";
                    """
                    # [修正] tx.query 是方法
                    tx.query(update_pointer_query).resolve()
                else:
                    insert_pointer_query = f"""
                    insert $p isa system-pointer,
                        has pointer-name "{LATEST_THOUGHT_POINTER_KEY}",
                        has target-key "{new_key}";
                    """
                    # [修正] tx.query 是方法
                    tx.query(insert_pointer_query).resolve()

                tx.commit()
                return new_key

        try:
            saved_key = await asyncio.to_thread(db_write)
            if saved_key:
                logger.info(f"思想点 '{saved_key}' 已成功串入思想链。")
            return saved_key
        except Exception as e:
            logger.error(f"思想链操作事务执行失败: {e}", exc_info=True)
            return None

    async def get_latest_thought_document(self) -> dict | None:
        """获取最新的思想点文档."""
        query = f"""
        match
            $p isa system-pointer, has pointer-name "{LATEST_THOUGHT_POINTER_KEY}";
            $p has target-key $key;
            $t isa thought-chain-node, has thought-id $key;
            $t has $attr;
            $attr isa attribute;
            $attr has $value;
            $attr_type = $attr.type;
            $attr_type has label $attr_label;
        get $attr_label, $value;
        """
        driver = self.conn_manager.get_driver()
        db_name = self.conn_manager.database_name

        def db_read() -> dict | None:
            with driver.transaction(db_name, TransactionType.READ) as tx:
                # [修正] tx.query 是方法
                answers = list(tx.query(query).resolve())
                if not answers:
                    return None

                doc = {}
                for answer in answers:
                    label = answer.get("attr_label").as_attribute().get_value().get_string()
                    value_concept = answer.get("value")
                    py_value = value_concept.get_value()

                    key_map = {
                        "thought-id": "_key", "action-payload-json": "action_payload",
                        "timestamp": "timestamp", "mood": "mood", "think": "think",
                        "intent": "intent", "source-type": "source_type", "source-id": "source_id",
                        "action-id": "action_id", "action-result": "action_result"
                    }
                    doc_key = key_map.get(label, label.replace("-", "_"))

                    if isinstance(py_value, str) and doc_key.endswith("payload"):
                        try:
                            doc[doc_key] = json.loads(py_value)
                        except json.JSONDecodeError:
                            doc[doc_key] = py_value
                    else:
                        doc[doc_key] = py_value
                
                if "timestamp" in doc and isinstance(doc["timestamp"], int):
                    doc["timestamp"] = datetime.datetime.fromtimestamp(doc["timestamp"] / 1000, tz=datetime.UTC).isoformat()

                if doc:
                    doc["_key"] = doc.get("thought_id")
                    logger.debug(f"成功获取最新的思想点: {doc.get('_key')}")
                return doc

        try:
            return await asyncio.to_thread(db_read)
        except Exception as e:
            logger.error(f"获取最新思想点时发生错误: {e}", exc_info=True)
            return None

    async def save_action_result_to_thought(self, thought_key: str, result_text: str) -> bool:
        """把行动的回执单贴到对应的思想点上."""
        if not thought_key or not result_text:
            return False

        driver = self.conn_manager.get_driver()
        db_name = self.conn_manager.database_name

        def db_write() -> bool:
            with driver.transaction(db_name, TransactionType.WRITE) as tx:
                update_query = f"""
                match $t isa thought-chain-node, has thought-id "{thought_key}";
                insert $t has action-result "{result_text.replace('"', '\\"')}";
                """
                # [修正] tx.query 是方法
                tx.query(update_query).resolve()
                tx.commit()
                return True

        try:
            success = await asyncio.to_thread(db_write)
            if success:
                logger.info(f"已将行动结果保存到思想点 '{thought_key}'。")
            return success
        except Exception as e:
            logger.error(f"为思想点 '{thought_key}' 保存行动结果时失败: {e}", exc_info=True)
            return False

    async def save_intrusive_thoughts_batch(
        self, thought_document_list: list[dict[str, Any]]
    ) -> bool:
        """批量保存侵入性思维文档到数据库."""
        if not thought_document_list:
            return True

        driver = self.conn_manager.get_driver()
        db_name = self.conn_manager.database_name

        def db_write() -> int:
            successful_inserts = 0
            with driver.transaction(db_name, TransactionType.WRITE) as tx:
                for doc_data in thought_document_list:
                    key = str(uuid.uuid4())
                    text = (doc_data.get("text") or "").replace('"', '\\"')
                    ts = int(time.time() * 1000)

                    insert_query = f"""
                    insert $it isa intrusive-thought,
                        has thought-id "{key}",
                        has thought-text "{text}",
                        has used false,
                        has timestamp-generated {ts};
                    """
                    # [修正] tx.query 是方法
                    tx.query(insert_query).resolve()
                    successful_inserts += 1
                tx.commit()
            return successful_inserts

        try:
            count = await asyncio.to_thread(db_write)
            logger.info(f"已成功批量保存 {count} 条侵入性思维。")
            return count > 0
        except Exception as e:
            logger.error(f"批量保存侵入性思维时发生严重错误: {e}", exc_info=True)
            return False

    async def get_random_unused_intrusive_thought_document(
        self,
    ) -> dict[str, Any] | None:
        """从侵入性思维池中获取一个随机的、未被使用过的侵入性思维文档."""
        query = "match $it isa intrusive-thought, has used false; $it has thought-text $text; get $text;"
        driver = self.conn_manager.get_driver()
        db_name = self.conn_manager.database_name

        def db_read() -> list[str]:
            with driver.transaction(db_name, TransactionType.READ) as tx:
                # [修正] tx.query 是方法
                answers = list(tx.query(query).resolve())
                return [a.get("text").as_attribute().get_value().get_string() for a in answers]

        try:
            unused_thoughts = await asyncio.to_thread(db_read)
            if not unused_thoughts:
                logger.info("侵入性思维池中当前没有未被使用过的思维。")
                return None

            import random

            return {"text": random.choice(unused_thoughts)}
        except Exception as e:
            logger.error(f"获取随机未使用的侵入性思维失败: {e}", exc_info=True)
            return None

    async def mark_intrusive_thought_document_used(self, thought_text: str) -> bool:
        """根据侵入性思维的文本，将其标记为已使用."""
        if not thought_text:
            return False

        driver = self.conn_manager.get_driver()
        db_name = self.conn_manager.database_name

        def db_write() -> bool:
            with driver.transaction(db_name, TransactionType.WRITE) as tx:
                text_safe = thought_text.replace('"', '\\"')
                delete_query = f"""
                match $it isa intrusive-thought, has thought-text "{text_safe}";
                $it has used false;
                delete $it has used false;
                """
                # [修正] tx.query 是方法
                tx.query(delete_query).resolve()

                insert_query = f"""
                match $it isa intrusive-thought, has thought-text "{text_safe}";
                insert $it has used true;
                """
                # [修正] tx.query 是方法
                tx.query(insert_query).resolve()
                tx.commit()
            return True

        try:
            success = await asyncio.to_thread(db_write)
            if success:
                logger.debug(f"侵入性思维 '{thought_text[:20]}...' 已成功标记为已使用。")
            return success
        except Exception as e:
            logger.error(f"标记侵入性思维为已使用时失败: {e}", exc_info=True)
            return False