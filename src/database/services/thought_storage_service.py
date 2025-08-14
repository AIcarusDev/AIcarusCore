import asyncio
import datetime
import json
import time
import uuid
from typing import Any

from loguru import logger
from typedb.driver import TransactionType

from ..core.connection_manager import TypeDBConnectionManager
from ..models import ThoughtChainDocument

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
                # 1. 获取上一个思想点的 key
                get_pointer_query = f"""
                match $p isa system-pointer, has pointer-name "{LATEST_THOUGHT_POINTER_KEY}";
                $p has target-key $key; get $key;
                """
                answers = list(tx.query.get(get_pointer_query).resolve())
                last_thought_key = (
                    answers[0].get("key").as_attribute().get_value().get_string()
                    if answers
                    else None
                )

                # 2. 插入新的思想点
                new_key = thought_data._key
                insert_parts = [
                    f'$t isa thought-chain-node, has thought-id "{new_key}"',
                    f"has timestamp {int(datetime.datetime.fromisoformat(thought_data.timestamp).timestamp() * 1000)}",  # noqa: E501
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
                tx.query.insert(insert_thought_query).resolve()

                # 3. 如果存在上一个思想点，创建 precedes-thought 关系
                if last_thought_key:
                    link_query = f"""
                    match
                        $prev isa thought-chain-node, has thought-id "{last_thought_key}";
                        $curr isa thought-chain-node, has thought-id "{new_key}";
                    insert
                        (preceding-thought: $prev, succeeding-thought: $curr) isa precedes-thought;
                    """
                    tx.query.insert(link_query).resolve()

                # 4. 更新指针 (UPSERT 逻辑)
                if last_thought_key:  # Update
                    update_pointer_query = f"""
                    match
                        $p isa system-pointer, has pointer-name "{LATEST_THOUGHT_POINTER_KEY}";
                        $p has target-key $old_key;
                    delete $p has $old_key;
                    insert $p has target-key "{new_key}";
                    """
                    tx.query.update(update_pointer_query).resolve()
                else:  # Insert
                    insert_pointer_query = f"""
                    insert $p isa system-pointer,
                        has pointer-name "{LATEST_THOUGHT_POINTER_KEY}",
                        has target-key "{new_key}";
                    """
                    tx.query.insert(insert_pointer_query).resolve()

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
            $t has $a;
            $a isa attribute;
            $a has $v;
        get $t, $a, $v;
        """
        driver = self.conn_manager.get_driver()
        db_name = self.conn_manager.database_name

        def db_read() -> dict | None:
            with driver.transaction(db_name, TransactionType.READ) as tx:
                answers = list(tx.query.get_aggregate(query).resolve())
                if not answers:
                    return None

                # 辅助函数，将TypeDB的Attribute Concept转换为Python值
                def concept_to_value(attr_concept: Any) -> Any:
                    val = attr_concept.get_value()
                    if val.is_string():
                        return val.get_string()
                    if val.is_long():
                        return val.get_integer()
                    if val.is_boolean():
                        return val.get_boolean()
                    # ...可以根据需要添加更多类型
                    return str(val)

                # 重构文档
                doc = {}
                for answer in answers:
                    attr_type_label = answer.get("a").get_type().get_label().name()
                    attr_value = concept_to_value(answer.get("a"))

                    # 转换回原始的JSON key
                    key_map = {
                        "thought-id": "_key",
                        "action-payload-json": "action_payload",
                        # ... 其他字段
                    }
                    doc_key = key_map.get(attr_type_label, attr_type_label)

                    if doc_key.endswith("_json"):
                        doc[doc_key.replace("_json", "")] = json.loads(attr_value)
                    else:
                        doc[doc_key] = attr_value

                if doc:
                    doc["_key"] = doc.get("thought-id")
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
                tx.query.insert(update_query).resolve()
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
                    tx.query.insert(insert_query).resolve()
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
        # TypeDB没有内置RANDOM，所以我们获取所有未使用的，然后在Python中随机选一个
        query = "match $it isa intrusive-thought, has used false; $it has thought-text $text; get $text;"  # noqa: E501
        driver = self.conn_manager.get_driver()
        db_name = self.conn_manager.database_name

        def db_read() -> list[str]:
            with driver.transaction(db_name, TransactionType.READ) as tx:
                answers = list(tx.query.get(query).resolve())
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
                # 1. Match and delete 'used false'
                delete_query = f"""
                match $it isa intrusive-thought, has thought-text "{text_safe}";
                $it has used false;
                delete $it has used false;
                """
                tx.query.delete(delete_query).resolve()

                # 2. Insert 'used true'
                insert_query = f"""
                match $it isa intrusive-thought, has thought-text "{text_safe}";
                insert $it has used true;
                """
                tx.query.insert(insert_query).resolve()
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
