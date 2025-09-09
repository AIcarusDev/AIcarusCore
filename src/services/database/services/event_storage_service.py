# src/database/services/event_storage_service.py
import asyncio
import json
import math
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
        """Save an event document to the TypeDB database using an idempotent `put` query.

        Parameters
        ----------
        event_doc_data : dict[str, Any]
            Dictionary containing event data.

        Returns:
        -------
        bool
            True if the event was successfully saved or already existed, False on error.
        """
        if not (event_id := (event_doc_data.get("_key") or event_doc_data.get("event_id"))):
            logger.error("保存事件失败：缺少 'event_id' 或 '_key'。")
            return False
        if not (platform_uid := event_doc_data.get("platform")):
            logger.error(f"保存事件 '{event_id}' 失败：缺少 'platform' 字段。")
            return False

        driver, db_name = self.conn_manager.get_driver(), self.conn_manager.database_name

        def db_write() -> bool:
            with driver.transaction(db_name, TransactionType.WRITE) as tx:
                # 构建属性插入部分
                timestamp_value = int(
                    event_doc_data.get("time", event_doc_data.get("timestamp", 0))
                )
                put_parts = [
                    f'$e isa event, has event-id "{event_id}"',
                    f'has event-type "{event_doc_data.get("event_type", "unknown")}"',
                    f"has timestamp {timestamp_value}",
                    f'has bot-id "{event_doc_data.get("bot_id", "unknown")}"',
                    f'has status "{event_doc_data.get("status", "unread")}"',
                ]

                # 处理 JSON 字符串属性
                for key, attr in [
                    ("content", "content-json"),
                    ("user_info", "user-info-json"),
                    ("conversation_info", "conversation-info-json"),
                    ("embedding", "embedding-json"),
                    ("image_analysis", "image-analysis-json"),
                ]:
                    if val := event_doc_data.get(key):
                        # 1. 先序列化为 JSON 字符串
                        json_string = json.dumps(val, ensure_ascii=False)
                        # 2. 对 JSON 字符串本身进行转义，以安全地插入 TQL 查询
                        #    必须先替换反斜杠，再替换双引号
                        safe_val = json_string.replace("\\", "\\\\").replace('"', '\\"')
                        put_parts.append(f'has {attr} "{safe_val}"')

                # 处理普通字符串属性
                for key, attr in [
                    ("person_id_associated", "person-id-associated"),
                    ("motivation", "motivation"),
                    ("narrative_sentence", "narrative-sentence"),
                ]:
                    if val := event_doc_data.get(key):
                        safe_val = str(val).replace('"', '\\"')
                        put_parts.append(f'has {attr} "{safe_val}"')

                attributes_str = ",\n    ".join(put_parts)

                # 构建完整的原子性 put 查询
                full_query = f"""
                match
                    $p isa platform, has platform-uid "{platform_uid}";
                put
                    {attributes_str};
                    $_ isa event-source, links (source-platform: $p, sourced-event: $e);
                """

                # logger.debug(
                #     f"Executing atomic event put query for event_id '{event_id}':\n{full_query}"
                # )
                tx.query(full_query).resolve()
                tx.commit()
                return True

        try:
            success = await asyncio.to_thread(db_write)
            if success:
                logger.debug(f"事件文档 '{event_id}' 已通过幂等操作保存/确认存在。")
            return success
        except Exception as e:
            logger.error(f"保存事件文档 '{event_id}' 失败: {e!r}", exc_info=True)
            return False

    async def find_event_by_image_hash(self, image_hash: str) -> dict[str, Any] | None:
        """Find an event document by searching for an image hash in the content.

        Parameters
        ----------
        image_hash : str
            The image hash to search for in event content.

        Returns:
        -------
        dict[str, Any] | None
            The full event document if found, None if no event is found or if an error occurs.
        """
        if not image_hash:
            return None
        query = f'match $e isa event, has content-json $cj; $cj like ".*{image_hash}.*"; $e has timestamp $ts; sort $ts desc; limit 1; select $e;'  # noqa: E501
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
            logger.error(f"通过图片哈希 '{image_hash}' 查找事件时失败: {e!r}", exc_info=True)
            return None

    def _get_full_event_doc_sync(self, tx: Transaction, event_id: str) -> dict[str, Any] | None:
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
        """Get recent chat message documents for a specific conversation.

        Parameters
        ----------
        conversation_id : str
            The unique identifier of the conversation to retrieve messages from.
        limit : int, optional
            Maximum number of recent messages to retrieve, by default 50.
        fetch_all_event_types : bool, optional
            Whether to fetch all event types or only message events, by default False.

        Returns:
        -------
        list[dict[str, Any]]
            A list of event documents sorted by timestamp in descending order.
            Returns empty list if no events are found or if an error occurs.
        """
        event_type_filter = "message\\\\..*" if not fetch_all_event_types else ".*"
        query = rf'match $e isa event, has conversation-info-json $ci; $ci like ".*\"conversation_id\": \"{conversation_id}\".*"; $e has event-type $et; $et like "{event_type_filter}"; $e has timestamp $ts; sort $ts desc; limit {limit}; select $e;'  # noqa: E501
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
                    if (
                        eid_answers
                        and (id_attr := eid_answers[0].get("id"))
                        and (
                            full_doc := self._get_full_event_doc_sync(
                                tx, id_attr.as_attribute().get_value()
                            )
                        )
                    ):
                        docs.append(full_doc)
                return docs

        try:
            return await asyncio.to_thread(db_read)
        except Exception as e:
            logger.error(f"获取会话 '{conversation_id}' 的最近事件失败: {e!r}", exc_info=True)
            return []

    async def update_events_status(self, event_ids: list[str], new_status: str) -> bool:
        """Update the status of multiple events.

        Parameters
        ----------
        event_ids : list[str]
            List of event IDs to update.
        new_status : str
            New status to set for the events.

        Returns:
        -------
        bool
            True if the update was successful, False otherwise.
        """
        if not event_ids:
            return True
        driver, db_name = self.conn_manager.get_driver(), self.conn_manager.database_name

        def db_write() -> bool:
            with driver.transaction(db_name, TransactionType.WRITE) as tx:
                for event_id in event_ids:
                    tx.query(
                        f'match $e isa event, has event-id "{event_id}", has status $s; delete has $s of $e;'  # noqa: E501
                    ).resolve()
                    tx.query(
                        f'match $e isa event, has event-id "{event_id}"; insert $e has status "{new_status}";'  # noqa: E501
                    ).resolve()
                tx.commit()
            return True

        try:
            return await asyncio.to_thread(db_write)
        except Exception as e:
            logger.error(f"批量更新事件状态为 '{new_status}' 时失败: {e!r}", exc_info=True)
            return False

    async def get_all_conversation_vectors_for_iis(self) -> list[list[list[float]]]:
        """Get all conversation vectors grouped by conversation for IIS model.

        Retrieves message events with embeddings from the database, groups them by
        conversation ID, and returns vectors sorted by timestamp for conversations
        with at least 2 messages.

        Returns:
        -------
        list[list[list[float]]]
            A list of conversations, where each conversation is a list of embedding
            vectors (list[float]) sorted by timestamp. Only includes conversations
            with 2 or more messages.
        """
        query = (
            r"match $event isa event, has event-type $type; "
            r'$type like "message\\..*"; '
            r"$event has embedding-json $embedding_json; "
            r"$event has conversation-info-json $conv_info_json; "
            r"$event has timestamp $ts; "
            r"select $conv_info_json, $embedding_json, $ts;"
        )
        driver, db_name = self.conn_manager.get_driver(), self.conn_manager.database_name

        def db_read_and_group() -> list[list[list[float]]]:
            conversations: dict[str, list[tuple[int, list[float]]]] = {}
            with driver.transaction(db_name, TransactionType.READ) as tx:
                for ans in list(tx.query(query).resolve().as_concept_rows()):
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
                [vec for _, vec in sorted(events, key=lambda x: x[0])]
                for _, events in conversations.items()
                if len(events) >= 2
            ]

        try:
            return await asyncio.to_thread(db_read_and_group)
        except Exception as e:
            logger.error(f"为IIS模型获取事件向量时失败: {e!r}", exc_info=True)
            return []

    async def get_event_by_timestamp(
        self, conversation_uid: str, timestamp: int
    ) -> dict[str, Any] | None:
        """Get an event by its timestamp within a specific conversation.

        Parameters
        ----------
        conversation_uid : str
            The unique identifier of the conversation.
        timestamp : int
            The timestamp of the event to retrieve.

        Returns:
        -------
        dict[str, Any] | None
            A dictionary containing the full event document if found,
            None if no event is found or if an error occurs.
        """
        _, _, conv_native_id = parse_entity_uid(conversation_uid) or (None, None, None)
        if not conv_native_id:
            return None

        # Corrected query from TypeDB-AI
        query = f"""
        match
            $event isa event,
                has conversation-info-json $json,
                has timestamp $ts,
                has event-id $event_id;
            $json contains "\\"{conv_native_id}\\"";
            $ts == {timestamp};
        select $event_id;
        limit 1;
        """
        driver, db_name = self.conn_manager.get_driver(), self.conn_manager.database_name

        def db_read() -> dict | None:
            # logger.debug(
            #     f"[PROBE_F] Executing get_event_by_timestamp for conv_uid={conversation_uid}, "
            #     f"ts={timestamp}"
            # )
            with driver.transaction(db_name, TransactionType.READ) as tx:
                answers = list(tx.query(query).resolve().as_concept_rows())
                if answers and (eid_attr := answers[0].get("event_id")):
                    return self._get_full_event_doc_sync(tx, eid_attr.as_attribute().get_value())
            return None

        try:
            return await asyncio.to_thread(db_read)
        except Exception as e:
            logger.error(f"通过时间戳 {timestamp} 获取事件失败: {e!r}", exc_info=True)
            return None

    async def get_unread_count(
        self, conversation_uid: str, self_bot_ids: dict[str, str]
    ) -> dict[str, Any]:
        """Get the count of unread messages and priority status for a conversation.

        This method has been refactored to use efficient aggregate queries,
        preventing gRPC message size limit errors.

        Parameters
        ----------
        conversation_uid : str
            The unique identifier of the conversation.
        self_bot_ids : dict[str, str]
            Dictionary mapping platforms to bot IDs for determining priority messages.

        Returns:
        -------
        dict[str, Any]
            A dictionary containing 'unread_count' (int) and 'has_high_priority' (bool).
        """
        if not self.entity_graph_service:
            return {"unread_count": 0, "has_high_priority": False}
        last_read_ts = await self.entity_graph_service.get_conversation_last_read_timestamp(
            conversation_uid
        )
        _, _, conv_native_id = parse_entity_uid(conversation_uid) or (None, None, None)
        if not conv_native_id:
            return {"unread_count": 0, "has_high_priority": False}

        driver, db_name = self.conn_manager.get_driver(), self.conn_manager.database_name

        def db_read_and_process() -> dict[str, Any]:
            unread_count = 0
            has_high_priority = False

            with driver.transaction(db_name, TransactionType.READ) as tx:
                # Query 1: Get total unread count (efficiently)
                get_total_unread_query = f"""
                match
                    $e isa event,
                        has conversation-info-json $ci,
                        has timestamp $ts;
                    $ci contains "\\"{conv_native_id}\\"";
                    $ts > {int(last_read_ts)};
                reduce $count = count;
                """
                logger.debug(f"Executing unread count query for {conversation_uid}")
                # --- [FIX START] ---
                answers_count_iterator = tx.query(get_total_unread_query).resolve()
                answers_count_rows = list(answers_count_iterator.as_concept_rows())
                if answers_count_rows:
                    count_concept = answers_count_rows[0].get("count")
                    if count_concept:
                        unread_count = count_concept.as_value().get_integer()
                # --- [FIX END] ---

                # If there are no unread messages, no need to check for high priority
                if unread_count == 0:
                    return {"unread_count": 0, "has_high_priority": False}

                # Query 2: Check for high-priority messages (mentions/quotes)
                # Build the OR clauses for all bot IDs
                bot_id_clauses = " or ".join(
                    f'{{ $content contains \'"user_id": "{bot_id}"\'; }}'
                    for bot_id in self_bot_ids.values()
                )

                if bot_id_clauses:
                    get_high_priority_unread_query = f"""
                    match
                        $e isa event,
                            has conversation-info-json $ci,
                            has timestamp $ts,
                            has content-json $content;
                        $ci contains "\\"{conv_native_id}\\"";
                        $ts > {int(last_read_ts)};
                        {{
                            $content contains \'"type": "at"\';
                        }} or {{
                            $content contains \'"type": "quote"\';
                        }};
                        {bot_id_clauses};
                    reduce $exists = count;
                    limit 1;
                    """
                    logger.debug(f"Executing high priority check for {conversation_uid}")
                    # --- [FIX START] ---
                    answers_priority_iterator = tx.query(get_high_priority_unread_query).resolve()
                    answers_priority_rows = list(answers_priority_iterator.as_concept_rows())
                    if answers_priority_rows:
                        exists_concept = answers_priority_rows[0].get("exists")
                        if exists_concept and exists_concept.as_value().get_integer() > 0:
                            has_high_priority = True
                    # --- [FIX END] ---

            return {"unread_count": unread_count, "has_high_priority": has_high_priority}

        try:
            return await asyncio.to_thread(db_read_and_process)
        except Exception as e:
            logger.error(f"计算会话 {conversation_uid} 未读数失败: {e!r}", exc_info=True)
            return {"unread_count": 0, "has_high_priority": False}

    async def get_paged_chat_history(
        self, conversation_uid: str, page: int, page_size: int
    ) -> tuple[list[dict], int, int]:
        """获取分页的聊天记录.

        Args:
            conversation_uid: 会话的持久化 UID。
            page: 要获取的页码 (从1开始)。
            page_size: 每页的消息数量。

        Returns:
            一个元组: (消息列表, 当前页码, 总页数)。
            消息列表按时间升序排列（旧消息在前）。
        """
        _, _, conv_native_id = parse_entity_uid(conversation_uid) or (None, None, None)
        if not conv_native_id:
            return [], 1, 1

        driver, db_name = self.conn_manager.get_driver(), self.conn_manager.database_name

        def db_read() -> tuple[list[dict], int, int]:
            with driver.transaction(db_name, TransactionType.READ) as tx:
                # 1. 先获取总数以计算总页数
                count_query = f"""
                match $e isa event, has conversation-info-json $ci;
                $ci contains "\\"{conv_native_id}\\"";
                reduce $count = count;
                """
                count_result = list(tx.query(count_query).resolve().as_concept_rows())
                total_count = (
                    count_result[0].get("count").as_value().get_integer() if count_result else 0
                )

                if total_count == 0:
                    return [], 1, 1

                total_pages = math.ceil(total_count / page_size)

                # 2. 计算偏移量并获取分页数据
                offset = (page - 1) * page_size

                query = f"""
                match $e isa event, has conversation-info-json $ci;
                $ci contains "\\"{conv_native_id}\\"";
                $e has timestamp $ts;
                sort $ts desc; offset {offset}; limit {page_size};
                select $e;
                """

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
                        full_doc = self._get_full_event_doc_sync(
                            tx, id_attr.as_attribute().get_value()
                        )
                        if full_doc:
                            docs.append(full_doc)

                # 3. 结果是按时间降序的，我们需要反转它，让UI上旧消息在上面
                return list(reversed(docs)), page, total_pages

        try:
            return await asyncio.to_thread(db_read)
        except Exception as e:
            logger.error(f"获取分页聊天记录失败 (UID: {conversation_uid}): {e}", exc_info=True)
            return [], 1, 1

    async def get_event_text_summary(self, event_doc: dict) -> str:
        """从事件文档中提取一个简短的文本摘要."""
        if not event_doc:
            return "[空消息]"

        content = event_doc.get("content", [])
        if not isinstance(content, list):
            return "[消息格式错误]"

        text_parts = []
        for seg in content:
            if seg.get("type") == "text":
                text_parts.append(seg.get("data", {}).get("text", ""))
            elif seg.get("type") == "image":
                text_parts.append("[图片]")
            # 可以根据需要添加对其他类型的处理

        summary = "".join(text_parts).strip()
        if not summary:
            return "[非文本消息]"

        return f"{summary[:30]}..." if len(summary) > 30 else summary

    # TODO:优化这个查询，避免在大数据集上进行全文字符串匹配
    async def get_event_by_platform_message_id(
        self, conversation_uid: str, platform_message_id: str
    ) -> dict[str, Any] | None:
        """通过平台原生的 message_id 在特定会话中查找事件.

        这是一个相对耗时的查询，应谨慎使用。
        """
        _, _, conv_native_id = parse_entity_uid(conversation_uid) or (None, None, None)
        if not conv_native_id or not platform_message_id:
            return None

        # 在JSON字符串中查找 message_id。需要对双引号进行转义。
        # "message_id": "-12345" -> \\"message_id\\": \\"-12345\\"
        message_id_pattern = f'\\"message_id\\": \\"{platform_message_id}\\"'

        # 这个查询结合了会话ID和消息ID模式，以最大化效率
        query = f"""
        match
            $e isa event,
                has conversation-info-json $ci,
                has content-json $cj;
            $ci contains "\\"{conv_native_id}\\"";
            $cj contains "{message_id_pattern}";
        select $e;
        limit 1;
        """
        driver, db_name = self.conn_manager.get_driver(), self.conn_manager.database_name

        def db_read() -> dict | None:
            with driver.transaction(db_name, TransactionType.READ) as tx:
                answers = list(tx.query(query).resolve().as_concept_rows())
                if answers and (e_concept := answers[0].get("e")):
                    # 复用内部帮助函数来获取完整的事件文档
                    eid_answers = list(
                        tx.query(
                            f"match $x iid {e_concept.get_iid()}, has event-id $id; select $id;"
                        )
                        .resolve()
                        .as_concept_rows()
                    )
                    if eid_answers and (id_attr := eid_answers[0].get("id")):
                        return self._get_full_event_doc_sync(tx, id_attr.as_attribute().get_value())
            return None

        try:
            return await asyncio.to_thread(db_read)
        except Exception as e:
            logger.error(
                f"通过 platform_message_id '{platform_message_id}' "
                f"在会话 '{conversation_uid}' 中查找事件失败: {e!r}",
                exc_info=True
            )
            return None
