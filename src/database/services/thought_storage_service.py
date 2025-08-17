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
    """A service for managing thought storage operations in TypeDB.

    This class provides methods for saving, retrieving, and managing thought documents
    and intrusive thoughts in a TypeDB database. It handles thought chains, action results,
    and maintains pointers to the latest thoughts.

    Attributes:
    ----------
    conn_manager : TypeDBConnectionManager
        The connection manager for TypeDB database operations.

    Methods:
    -------
    save_thought_and_link(thought_data: ThoughtChainDocument) -> str | None
        Save a thought document and link it to the previous thought.
    get_latest_thought_document() -> dict | None
        Get the latest thought document from the database.
    save_action_result_to_thought(thought_key: str, result_text: str) -> bool
        Save an action result to a specific thought document.
    save_intrusive_thoughts_batch(thought_document_list: list[dict[str, Any]]) -> bool
        Save a batch of intrusive thoughts to the database.
    get_random_unused_intrusive_thought_document() -> dict[str, Any] | None
        Get a random unused intrusive thought document.
    mark_intrusive_thought_document_used(thought_text: str) -> bool
        Mark an intrusive thought document as used.
    """

    def __init__(self, conn_manager: TypeDBConnectionManager) -> None:
        self.conn_manager = conn_manager
        logger.info("ThoughtStorageService (TypeDB) 初始化完成。")

    async def save_thought_and_link(self, thought_data: ThoughtChainDocument) -> str | None:
        """Save a thought document to the database and link it to the previous thought.

        Parameters
        ----------
        thought_data : ThoughtChainDocument
            The thought document containing all the data to be saved to the database.

        Returns:
        -------
        str | None
            The unique key of the saved thought if successful, None if the operation failed.
        """
        driver, db_name = self.conn_manager.get_driver(), self.conn_manager.database_name

        def db_write() -> str | None:
            with driver.transaction(db_name, TransactionType.WRITE) as tx:
                # 1. 获取上一个思想节点的 key
                answers = list(
                    tx.query(
                        f'match $p isa system-pointer, has pointer-name "{LATEST_THOUGHT_POINTER_KEY}"; $p has target-key $key; select $key;'
                    )
                    .resolve()
                    .as_concept_rows()
                )
                last_thought_key = (
                    answers[0].get("key").as_attribute().get_value() if answers else None
                )
                new_key = thought_data._key

                # 2. 构建属性插入部分
                insert_parts = [
                    f'has thought-id "{new_key}"',
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
                    insert_parts.append(
                        f'has action-payload-json "{json.dumps(thought_data.action_payload, ensure_ascii=False).replace('"', '\\"')}"'
                    )
                attributes_str = ",\n    ".join(insert_parts)

                # 3. 使用单一、原子性的查询来插入新思想并建立连接
                if last_thought_key:
                    full_query = f"""
                    match
                        $prev isa thought-chain-node, has thought-id "{last_thought_key}";
                    insert
                        $curr isa thought-chain-node, {attributes_str};
                        (preceding-thought: $prev, succeeding-thought: $curr) isa precedes-thought;
                    """
                else:
                    full_query = f"""
                    insert $curr isa thought-chain-node, {attributes_str};
                    """
                tx.query(full_query).resolve()

                # 4. 原子性地更新指针
                if last_thought_key:
                    tx.query(
                        f'match $p isa system-pointer, has pointer-name "{LATEST_THOUGHT_POINTER_KEY}"; $p has target-key $old_key; delete has $old_key of $p; insert $p has target-key "{new_key}";'
                    ).resolve()
                else:
                    tx.query(
                        f'insert $p isa system-pointer, has pointer-name "{LATEST_THOUGHT_POINTER_KEY}", has target-key "{new_key}";'
                    ).resolve()

                tx.commit()
                return new_key

        try:
            return await asyncio.to_thread(db_write)
        except Exception as e:
            logger.error(f"思想链操作事务执行失败: {e}", exc_info=True)
            return None

    async def get_latest_thought_document(self) -> dict | None:
        """Get the latest thought document from the database.

        Returns:
        -------
        dict | None
            A dictionary containing the latest thought document data if found,
            None otherwise. The dictionary includes fields like '_key', 'timestamp',
            'mood', 'think', and other thought-related attributes.
        """
        # [FIXED] 修正了 TypeQL 语法，使用 '==' 来提取和匹配值
        query = f"""
        match
            $p isa system-pointer, has pointer-name "{LATEST_THOUGHT_POINTER_KEY}";
            $p has target-key $key_attr;
            $key_attr == $key_value;
            $t isa thought-chain-node, has thought-id $key_value;
            $t has $attr;
            $attr isa $attr_type;
        select $attr, $attr_type;
        """
        driver, db_name = self.conn_manager.get_driver(), self.conn_manager.database_name

        def db_read() -> dict | None:
            with driver.transaction(db_name, TransactionType.READ) as tx:
                answers = list(tx.query(query).resolve().as_concept_rows())
                if not answers:
                    return None
                doc = {}
                for answer in answers:
                    attr_type_concept = answer.get("attr_type")
                    attr_concept = answer.get("attr")
                    if attr_type_concept and attr_concept:
                        label = attr_type_concept.as_attribute_type().get_label()
                        py_value = attr_concept.as_attribute().get_value()
                        key_map = {"thought-id": "_key", "action-payload-json": "action_payload"}
                        doc_key = key_map.get(label, label.replace("-", "_"))
                        if isinstance(py_value, str) and "json" in label:
                            try:
                                doc[doc_key] = json.loads(py_value)
                            except json.JSONDecodeError:
                                doc[doc_key] = py_value
                        else:
                            doc[doc_key] = py_value
                if "timestamp" in doc and isinstance(doc["timestamp"], int):
                    doc["timestamp"] = datetime.datetime.fromtimestamp(
                        doc["timestamp"] / 1000, tz=datetime.UTC
                    ).isoformat()
                if doc and "thought_id" in doc:
                    doc["_key"] = doc["thought_id"]
                return doc

        try:
            return await asyncio.to_thread(db_read)
        except Exception as e:
            logger.error(f"获取最新思想点时发生错误: {e}", exc_info=True)
            return None

    async def save_action_result_to_thought(self, thought_key: str, result_text: str) -> bool:
        """Save an action result to a specific thought document in the database.

        Parameters
        ----------
        thought_key : str
            The unique identifier of the thought to update with the action result.
        result_text : str
            The text content of the action result to save.

        Returns:
        -------
        bool
            True if the action result was successfully saved, False otherwise.
        """
        if not thought_key or not result_text:
            return False
        driver, db_name = self.conn_manager.get_driver(), self.conn_manager.database_name

        def db_write() -> bool:
            with driver.transaction(db_name, TransactionType.WRITE) as tx:
                safe_result_text = result_text.replace('"', '\\"')
                tx.query(
                    f'match $t isa thought-chain-node, has thought-id "{thought_key}"; insert $t has action-result "{safe_result_text}";'  # noqa: E501
                ).resolve()
                tx.commit()
                return True

        try:
            return await asyncio.to_thread(db_write)
        except Exception as e:
            logger.error(f"为思想点 '{thought_key}' 保存行动结果时失败: {e}", exc_info=True)
            return False

    async def save_intrusive_thoughts_batch(
        self, thought_document_list: list[dict[str, Any]]
    ) -> bool:
        """Save a batch of intrusive thoughts to the database.

        Parameters
        ----------
        thought_document_list : list[dict[str, Any]]
            A list of dictionaries containing thought documents to save.
            Each dictionary should have a 'text' key with the thought content.

        Returns:
        -------
        bool
            True if at least one thought was successfully saved, False otherwise.
        """
        if not thought_document_list:
            return True
        driver, db_name = self.conn_manager.get_driver(), self.conn_manager.database_name

        def db_write() -> int:
            with driver.transaction(db_name, TransactionType.WRITE) as tx:
                successful_inserts = 0
                for doc_data in thought_document_list:
                    key, text, ts = (
                        str(uuid.uuid4()),
                        (doc_data.get("text") or "").replace('"', '\\"'),
                        int(time.time() * 1000),
                    )
                    tx.query(
                        f'insert $it isa intrusive-thought, has thought-id "{key}", has thought-text "{text}", has used false, has timestamp-generated {ts};'  # noqa: E501
                    ).resolve()
                    successful_inserts += 1
                tx.commit()
            return successful_inserts

        try:
            return await asyncio.to_thread(db_write) > 0
        except Exception as e:
            logger.error(f"批量保存侵入性思维时发生严重错误: {e}", exc_info=True)
            return False

    async def get_random_unused_intrusive_thought_document(self) -> dict[str, Any] | None:
        """Get a random unused intrusive thought document from the database.

        Returns:
        -------
        dict[str, Any] | None
            A dictionary containing the thought text if found, None otherwise.
            The dictionary has the format: {"text": "thought content"}
        """
        query = "match $it isa intrusive-thought, has used false; $it has thought-text $text; select $text;"  # noqa: E501
        driver, db_name = self.conn_manager.get_driver(), self.conn_manager.database_name

        def db_read() -> list[str]:
            with driver.transaction(db_name, TransactionType.READ) as tx:
                return [
                    a.get("text").as_attribute().get_value()
                    for a in tx.query(query).resolve().as_concept_rows()
                ]

        try:
            unused_thoughts = await asyncio.to_thread(db_read)
            if not unused_thoughts:
                return None
            import random

            return {"text": random.choice(unused_thoughts)}
        except Exception as e:
            logger.error(f"获取随机未使用的侵入性思维失败: {e}", exc_info=True)
            return None

    async def mark_intrusive_thought_document_used(self, thought_text: str) -> bool:
        """Mark an intrusive thought document as used in the database.

        Parameters
        ----------
        thought_text : str
            The text content of the intrusive thought to mark as used.

        Returns:
        -------
        bool
            True if the thought was successfully marked as used, False otherwise.
        """
        if not thought_text:
            return False
        driver, db_name = self.conn_manager.get_driver(), self.conn_manager.database_name

        def db_write() -> bool:
            with driver.transaction(db_name, TransactionType.WRITE) as tx:
                text_safe = thought_text.replace('"', '\\"')
                tx.query(
                    f'match $it isa intrusive-thought, has thought-text "{text_safe}", has used false; delete has false of $it;'  # noqa: E501
                ).resolve()
                tx.query(
                    f'match $it isa intrusive-thought, has thought-text "{text_safe}"; insert $it has used true;'  # noqa: E501
                ).resolve()
                tx.commit()
            return True

        try:
            return await asyncio.to_thread(db_write)
        except Exception as e:
            logger.error(f"标记侵入性思维为已使用时失败: {e}", exc_info=True)
            return False
