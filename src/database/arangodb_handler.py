# AICC/src/database/arangodb_handler.py
# 修复 StandardCollection.update() got an unexpected keyword argument 'keep_null'
# 并进行初步清理和注释规范化
# Standard library imports
import asyncio
import datetime
import os
import time
import uuid
from contextlib import suppress
from typing import Any

# Third-party imports
from arango import ArangoClient
from arango.collection import StandardCollection
from arango.database import StandardDatabase
from arango.exceptions import (
    AQLQueryExecuteError,
    ArangoClientError,
    ArangoServerError,
    DocumentInsertError,
    DocumentRevisionError,
    DocumentUpdateError,
)

# Local imports
from src.common.custom_logging.logger_manager import get_logger
from src.database.models import DatabaseConfig  # Assuming DatabaseConfig is defined in models.py

# 假设 AttentionProfile 会在 models.py 中定义
# from src.database.models import AttentionProfile # 未来重构时可能会用到

module_logger = get_logger("AIcarusCore.database")


class ArangoDBHandler:
    """
    ArangoDB 数据库处理器。
    负责数据库连接、集合和索引的初始化，以及提供底层的数据库操作方法。
    注意：此类正在逐步重构，目标是使其更专注于连接管理和通用操作，
    具体的实体存储逻辑将迁移到专门的服务类中。
    """

    THOUGHTS_COLLECTION_NAME = "thoughts_collection"
    INTRUSIVE_THOUGHTS_POOL_COLLECTION_NAME = "intrusive_thoughts_pool"
    ACTION_LOGS_COLLECTION_NAME = "action_logs"  # 目前在核心逻辑中似乎未直接使用，但保留
    EVENTS_COLLECTION_NAME = "events"
    CONVERSATIONS_COLLECTION_NAME = "conversations"  # 存储会话信息及其注意力档案

    # 暂时不启用的集合，注释掉以保持清晰
    # USERS_COLLECTION_NAME = "users"
    # CONVERSATION_MEMBERS_COLLECTION_NAME = "conversation_members"
    # MESSAGE_CONTENT_COLLECTION_NAME = "message_content"

    def __init__(self, client: ArangoClient, db: StandardDatabase) -> None:
        self.client: ArangoClient = client
        self.db: StandardDatabase = db
        self.logger = get_logger(f"AIcarusCore.database.{self.__class__.__name__}")
        self.logger.debug(f"ArangoDBHandler instance initialized with database '{db.name}'.") # INFO -> DEBUG

    async def create_from_config(
        self, database_config_obj: "DatabaseConfig"
    ) -> "ArangoDBHandler":  # 修改参数名为 obj 以区分字典
        """
        从配置对象 (通常是 dataclass 实例) 创建 ArangoDBHandler 实例。
        优先从配置对象读取，其次尝试环境变量。
        """
        host = (
            getattr(database_config_obj, "host", None)
            or getattr(database_config_obj, "url", None)
            or getattr(database_config_obj, "arangodb_host", None)
            or os.getenv("ARANGODB_HOST")
        )
        username = (
            getattr(database_config_obj, "username", None)
            or getattr(database_config_obj, "user", None)
            or getattr(database_config_obj, "arangodb_user", None)
            or os.getenv("ARANGODB_USER")
        )
        password = (  # NOSONAR
            getattr(database_config_obj, "password", None)
            or getattr(database_config_obj, "arangodb_password", None)  # NOSONAR
            or os.getenv("ARANGODB_PASSWORD")  # NOSONAR
        )
        database_name = (
            getattr(database_config_obj, "name", None)
            or getattr(database_config_obj, "database_name", None)
            or getattr(database_config_obj, "arangodb_database", None)
            or os.getenv("ARANGODB_DATABASE")
        )

        if not all([host, database_name]):  # Username 和 password 可以为 None
            missing_params = []
            if not host:
                missing_params.append("host/ARANGODB_HOST")
            if not database_name:
                missing_params.append("database_name/ARANGODB_DATABASE")
            message = f"Error: Required ArangoDB connection parameters are not fully set. Missing: {', '.join(missing_params)}"
            module_logger.critical(message)
            raise ValueError(message)

        try:
            client_instance: ArangoClient = await asyncio.to_thread(ArangoClient, hosts=host)
            # 使用提供的用户名和密码连接到 _system 数据库以检查或创建目标数据库
            # 如果 username 或 password 为 None，ArangoClient 会尝试匿名连接或使用驱动的默认值（通常不推荐用于生产）
            sys_db: StandardDatabase = await asyncio.to_thread(
                client_instance.db, "_system", username=username, password=password
            )
            if not await asyncio.to_thread(sys_db.has_database, database_name):
                module_logger.debug(f"Database '{database_name}' does not exist. Attempting to create...") # INFO -> DEBUG
                await asyncio.to_thread(sys_db.create_database, database_name)
                module_logger.info(f"Database '{database_name}' created successfully.") # 这条创建成功的可以保留 INFO

            db_instance: StandardDatabase = await asyncio.to_thread(
                client_instance.db, database_name, username=username, password=password
            )
            await asyncio.to_thread(db_instance.properties)  # Verify connection to the target database
            module_logger.debug(f"Successfully connected to ArangoDB! Host: {host}, Database: {database_name}") # INFO -> DEBUG

            handler_instance = ArangoDBHandler(client_instance, db_instance)
            await handler_instance._ensure_core_collections_exist()
            return handler_instance

        except (ArangoServerError, ArangoClientError) as e:
            message = f"Error connecting to ArangoDB (Host: {host}, DB: {database_name}): {e}"
            module_logger.critical(message, exc_info=True)
            raise RuntimeError(message) from e
        except Exception as e:  # Catch-all for other unexpected errors
            message = f"Unknown or permission error connecting to ArangoDB (Host: {host}, DB: {database_name}, User: {username}): {e}"
            module_logger.critical(message, exc_info=True)
            raise RuntimeError(message) from e

    @classmethod
    async def create(cls) -> "ArangoDBHandler":
        """
        从环境变量创建 ArangoDBHandler 实例。
        这是 create_from_config 的一个便捷包装，当配置直接来自环境变量时使用。
        """

        # 构造一个简单的对象或字典来模拟 database_config_obj，让 create_from_config 处理
        class EnvConfig:
            pass

        env_cfg = EnvConfig()
        # create_from_config 内部会处理 os.getenv 作为后备
        return await cls.create_from_config(env_cfg)

    async def _ensure_core_collections_exist(self) -> None:
        """Ensures that all core business logic collections exist."""
        core_collections = [
            self.THOUGHTS_COLLECTION_NAME,
            self.INTRUSIVE_THOUGHTS_POOL_COLLECTION_NAME,
            self.ACTION_LOGS_COLLECTION_NAME,
            self.EVENTS_COLLECTION_NAME,
            self.CONVERSATIONS_COLLECTION_NAME,
        ]
        self.logger.info("Ensuring core collections exist...")
        for collection_name in core_collections:
            await self.ensure_collection_exists(collection_name)
        self.logger.info("Core collections ensured.")

    async def ensure_collection_exists(self, collection_name: str) -> StandardCollection:
        """
        Ensures a single collection exists, creates it if not, and applies specific indexes.
        """
        collection: StandardCollection | None = None
        if not await asyncio.to_thread(self.db.has_collection, collection_name):
            self.logger.info(f"Collection '{collection_name}' does not exist, creating...")
            collection = await asyncio.to_thread(self.db.create_collection, collection_name)
            self.logger.info(f"Collection '{collection_name}' created. Applying indexes...")

            if collection_name == self.EVENTS_COLLECTION_NAME:
                await self._create_events_indexes(collection)
            elif collection_name == self.THOUGHTS_COLLECTION_NAME:
                await self._create_thoughts_indexes(collection)
            elif collection_name == self.ACTION_LOGS_COLLECTION_NAME:
                await self._create_action_logs_indexes(collection)
            elif collection_name == self.CONVERSATIONS_COLLECTION_NAME:
                await self._create_conversations_indexes(collection)
            self.logger.info(f"Indexes for collection '{collection_name}' applied.")
            return collection
        else:
            # self.logger.debug(f"Collection '{collection_name}' already exists.") # Too verbose
            return await asyncio.to_thread(self.db.collection, collection_name)

    async def _create_events_indexes(self, collection: StandardCollection) -> None:
        """Creates indexes for the Events collection."""
        indexes = [
            (["event_type", "timestamp"], False, False),
            (["platform", "bot_id", "timestamp"], False, False),
            (["conversation_id_extracted", "timestamp"], False, False),  # Assuming these are top-level extracted fields
            (["user_id_extracted", "timestamp"], False, False),  # Assuming these are top-level extracted fields
            (["timestamp"], False, False),
        ]
        await self._apply_indexes_to_collection(collection, indexes)

    async def _create_thoughts_indexes(self, collection: StandardCollection) -> None:
        """Creates indexes for the Thoughts collection."""
        indexes = [
            (["timestamp"], False, False),
            (["action_attempted.action_id"], True, True),  # Sparse unique index on action_id if it exists
        ]
        await self._apply_indexes_to_collection(collection, indexes)

    async def _create_action_logs_indexes(self, collection: StandardCollection) -> None:  # Kept for now
        """Creates indexes for the Action Logs collection."""
        indexes = [
            (["action_id"], True, False),
            (["timestamp"], False, False),
        ]
        await self._apply_indexes_to_collection(collection, indexes)

    async def _create_conversations_indexes(self, collection: StandardCollection) -> None:
        """Creates indexes for the Conversations collection."""
        # conversation_id is usually _key, which is auto-indexed.
        indexes = [
            (["platform", "type"], False, False),
            (["updated_at"], False, False),
            (["parent_id"], False, True),  # parent_id can be null, so sparse is good
            # Future: Indexes on attention_profile fields e.g., (["attention_profile.is_suspended_by_ai"], False, True)
        ]
        await self._apply_indexes_to_collection(collection, indexes)

    async def _apply_indexes_to_collection(
        self, collection: StandardCollection, indexes_to_create: list[tuple[list[str], bool, bool]]
    ) -> None:
        """Helper to apply a list of index definitions to a collection."""
        collection_name = collection.name
        for fields, unique, sparse in indexes_to_create:
            try:
                self.logger.debug(
                    f"Applying index on '{collection_name}': fields={fields}, unique={unique}, sparse={sparse}"
                )
                await asyncio.to_thread(
                    collection.add_persistent_index, fields=fields, unique=unique, sparse=sparse, in_background=True
                )
            except Exception as e:
                # It's common for this to fail if the index already exists, which is fine.
                self.logger.warning(
                    f"Failed to create index {fields} on '{collection_name}' (may already exist or config issue): {e}"
                )

    def _get_default_attention_profile(self) -> dict:
        """
        Returns a default structure for the attention_profile.
        This should ideally be defined in a model class (e.g., AttentionProfile.get_default_dict()).
        """
        return {
            "base_importance_score": 0.5,
            "ai_preference_score": 0.5,
            "relevant_topic_tags": [],
            "last_ai_interaction_timestamp": None,
            "last_significant_event_timestamp": None,
            "cooldown_until_timestamp": None,
            "is_suspended_by_ai": False,
            "suspension_reason": None,
            "ai_custom_notes": "Newly discovered conversation. Profile awaiting initialization.",
        }

    async def upsert_conversation_info(self, conversation_info_data: dict) -> str | None:
        """
        Inserts or updates a conversation's information, including its attention profile.
        Uses 'conversation_id' from input data as the document '_key'.
        Manages 'created_at', 'updated_at', and initializes 'attention_profile' if new.
        """
        if not conversation_info_data or not isinstance(conversation_info_data, dict):
            self.logger.warning("Invalid 'conversation_info_data' (empty or not a dict). Cannot upsert.")
            return None

        conversation_id = conversation_info_data.get("conversation_id")
        if not conversation_id:
            self.logger.warning("'conversation_info_data' is missing 'conversation_id'. Cannot upsert.")
            return None

        collection = await self.ensure_collection_exists(self.CONVERSATIONS_COLLECTION_NAME)
        doc_key = str(conversation_id)
        current_time_ms = int(time.time() * 1000)

        data_for_db = conversation_info_data.copy()
        data_for_db["_key"] = doc_key  # Ensure _key is set for ArangoDB
        data_for_db["updated_at"] = current_time_ms

        existing_doc: dict | None = None

        with suppress(Exception):  # DocumentNotFoundError, etc.
            existing_doc = await asyncio.to_thread(collection.get, doc_key)

        if existing_doc:
            self.logger.debug(f"Conversation '{doc_key}' exists. Updating its profile.")
            data_for_db["created_at"] = existing_doc.get("created_at", current_time_ms)  # Preserve original created_at

            # Merge attention_profile: new data overrides, but if new is absent, keep old.
            existing_profile = existing_doc.get("attention_profile", {})
            new_profile_in_data = data_for_db.get("attention_profile")

            if new_profile_in_data is None and isinstance(existing_profile, dict) and existing_profile:
                data_for_db["attention_profile"] = existing_profile
            elif isinstance(new_profile_in_data, dict):
                data_for_db["attention_profile"] = {**existing_profile, **new_profile_in_data}
            elif new_profile_in_data is None and not existing_profile:
                data_for_db["attention_profile"] = self._get_default_attention_profile()
            # else: new_profile_in_data might be an invalid type, current logic would use it or error.

            # Merge 'extra' field similarly
            existing_extra = existing_doc.get("extra", {})
            new_extra_in_data = data_for_db.get("extra")
            if new_extra_in_data is None and isinstance(existing_extra, dict) and existing_extra:
                data_for_db["extra"] = existing_extra
            elif isinstance(new_extra_in_data, dict):
                data_for_db["extra"] = {**existing_extra, **new_extra_in_data}
            elif new_extra_in_data is None and not existing_extra:
                data_for_db["extra"] = {}

            try:
                # Use collection.update(document) - it will use the _key from the document for matching.
                # The default merge=True ensures partial update.
                await asyncio.to_thread(collection.update, data_for_db)
                self.logger.info(f"Conversation profile for '{doc_key}' updated successfully.")
                return doc_key
            except DocumentUpdateError as e:
                self.logger.error(f"Failed to update conversation profile for '{doc_key}': {e}", exc_info=True)
                return None
            except DocumentRevisionError as e_rev:
                self.logger.warning(
                    f"Revision conflict updating conversation '{doc_key}'. Retrying or specific strategy may be needed: {e_rev}"
                )
                return None
        else:
            self.logger.debug(f"Conversation '{doc_key}' is new. Creating its profile.")
            data_for_db["created_at"] = current_time_ms
            if "attention_profile" not in data_for_db or data_for_db["attention_profile"] is None:
                data_for_db["attention_profile"] = self._get_default_attention_profile()
            if (
                "extra" not in data_for_db or data_for_db["extra"] is None
            ):  # Ensure extra is at least an empty dict if not provided
                data_for_db["extra"] = {}

            try:
                # insert will fail if _key already exists.
                result = await asyncio.to_thread(collection.insert, data_for_db, overwrite=False)
                if result and result.get("_key"):
                    self.logger.info(
                        f"New conversation profile for '{doc_key}' created successfully with ID: {result['_key']}"
                    )
                    return result["_key"]
                else:
                    self.logger.error(f"Failed to get _key after inserting new conversation profile for '{doc_key}'.")
                    return None
            except DocumentInsertError as e:
                self.logger.error(
                    f"Failed to insert new conversation profile for '{doc_key}' (it might have been created concurrently): {e}",
                    exc_info=True,
                )
                # Potentially attempt a get and update here if concurrent creation is likely
                return None
        return None

    async def save_event_v14(self, event_data: dict) -> bool:
        """Saves a v1.4.0 protocol event to the database."""
        try:
            events_collection = await self.ensure_collection_exists(self.EVENTS_COLLECTION_NAME)

            if not event_data.get("event_id"):
                event_data["event_id"] = str(uuid.uuid4())
            event_data["_key"] = str(event_data["event_id"])

            if "timestamp" not in event_data or not isinstance(event_data["timestamp"], int | float):
                event_data["timestamp"] = time.time() * 1000.0
            event_data["timestamp"] = int(event_data["timestamp"])

            if "protocol_version" not in event_data:
                event_data["protocol_version"] = "1.4.0"

            # Ensure user_info and conversation_info are dicts or None
            for key_info in [
                "user_info",
                "conversation_info",
                "raw_data",
                "content",
            ]:  # content should be list of dicts
                if key_info in event_data and not isinstance(
                    event_data[key_info], dict | list | type(None)
                ):  # Allow list for content
                    self.logger.warning(
                        f"Event field '{key_info}' is not a dict, list or None, it's {type(event_data[key_info])}. Converting to string for safety."
                    )
                    event_data[key_info] = str(event_data[key_info])
                elif key_info == "content" and isinstance(event_data.get(key_info), list):
                    # Ensure all elements in content list are dicts (if it's supposed to be Seg dicts)
                    event_data[key_info] = [
                        item if isinstance(item, dict) else {"type": "unknown", "data_str": str(item)}
                        for item in event_data[key_info]
                    ]

            try:
                result = await asyncio.to_thread(events_collection.insert, event_data, overwrite=False)
            except DocumentInsertError:
                self.logger.warning(f"Attempted to insert an already existing Event ID: {event_data['_key']}. Skipped.")
                return True  # Considered success as data is present

            return bool(result and (result.get("_id") or result.get("_key")))

        except Exception as e:
            event_id_log = event_data.get("event_id", event_data.get("_key", "Unknown"))
            self.logger.error(f"Failed to save event (ID: {event_id_log}): {e}", exc_info=True)
            return False

    async def get_recent_chat_messages_for_context(
        self, duration_minutes: int = 10, conversation_id: str | None = None, limit: int = 50
    ) -> list[dict]:
        """Fetches recent chat messages, directly from the Events collection."""
        try:
            await self.ensure_collection_exists(self.EVENTS_COLLECTION_NAME)  # Ensure collection and indexes

            current_time_ms = int(time.time() * 1000.0)
            threshold_time_ms = current_time_ms - (duration_minutes * 60 * 1000)

            filters = ["event.timestamp >= @threshold_time", "event.event_type LIKE 'message.%'"]
            bind_vars: dict[str, Any] = {"threshold_time": threshold_time_ms, "limit": limit}

            if conversation_id:
                # Use conversation_id_extracted if that's the indexed/queryable field for conversation ID in events
                filters.append("event.conversation_id_extracted == @conversation_id")
                bind_vars["conversation_id"] = conversation_id

            filter_clause = " AND ".join(filters)
            query = f"""
                FOR event IN {self.EVENTS_COLLECTION_NAME}
                    FILTER {filter_clause}
                    SORT event.timestamp DESC
                    LIMIT @limit
                    RETURN event
            """
            results = await self.execute_query(query, bind_vars)
            return results if results is not None else []
        except Exception as e:
            self.logger.error(
                f"Failed to get recent chat messages for context (ConvID: {conversation_id}): {e}", exc_info=True
            )
            return []

    async def get_latest_thought_document_raw(self, limit: int = 1) -> list[dict]:
        """Fetches the latest raw thought document(s)."""
        try:
            await self.ensure_collection_exists(self.THOUGHTS_COLLECTION_NAME)
            query = f"""
                FOR thought IN {self.THOUGHTS_COLLECTION_NAME}
                    SORT thought.timestamp DESC
                    LIMIT @limit
                    RETURN thought
            """
            bind_vars: dict[str, Any] = {"limit": limit}
            results = await self.execute_query(query, bind_vars)
            return results if results is not None else []
        except Exception as e:
            self.logger.error(f"Failed to get latest thought document(s): {e}", exc_info=True)
            return []

    async def save_thought_document(self, document: dict[str, Any]) -> str | None:
        """Saves a thought document."""
        try:
            thoughts_collection = await self.ensure_collection_exists(self.THOUGHTS_COLLECTION_NAME)
            if "timestamp" not in document:  # Ensure timestamp
                document["timestamp"] = datetime.datetime.now(datetime.UTC).isoformat()
            if "_key" not in document:  # Ensure _key for idempotency if desired, or let ArangoDB generate
                document["_key"] = str(uuid.uuid4())

            result = await asyncio.to_thread(
                thoughts_collection.insert, document, overwrite=False
            )  # overwrite=False if _key should be unique
            if result and result.get("_key"):
                return result["_key"]
            self.logger.error(f"Failed to get _key after inserting thought document: {result}")
            return None
        except DocumentInsertError as e:
            self.logger.error(
                f"Failed to insert thought document (Key: {document.get('_key', 'N/A')}), it might already exist: {e}",
                exc_info=True,
            )
            return document.get("_key")  # Return key if it failed due to existence
        except Exception as e:
            self.logger.error(
                f"Failed to save thought document (Key: {document.get('_key', 'N/A')}): {e}", exc_info=True
            )
            return None

    async def save_intrusive_thoughts_batch(
        self, thoughts_list: list[Any]
    ) -> bool:  # thoughts_list can be List[str] or List[dict]
        """Saves a batch of intrusive thoughts."""
        try:
            if not thoughts_list:
                return True  # No thoughts to save is a success in this context

            pool_collection = await self.ensure_collection_exists(self.INTRUSIVE_THOUGHTS_POOL_COLLECTION_NAME)
            documents_to_insert = []
            current_time_iso = datetime.datetime.now(datetime.UTC).isoformat()

            for item in thoughts_list:
                doc: dict[str, Any]
                if isinstance(item, str):
                    doc = {
                        "_key": str(uuid.uuid4()),
                        "text": item,
                        "timestamp_generated": current_time_iso,
                        "used": False,
                    }
                elif isinstance(item, dict):
                    doc = item.copy()
                    if "_key" not in doc:
                        doc["_key"] = str(uuid.uuid4())
                    if "timestamp_generated" not in doc:
                        doc["timestamp_generated"] = current_time_iso
                    if "used" not in doc:
                        doc["used"] = False  # Ensure 'used' field
                else:
                    self.logger.warning(f"Skipping invalid item in intrusive_thoughts_list: {type(item)}")
                    continue
                documents_to_insert.append(doc)

            if not documents_to_insert:
                self.logger.info("No valid intrusive thoughts to save in batch.")
                return False

            # insert_many returns a list of result/error dicts
            results = await asyncio.to_thread(pool_collection.insert_many, documents_to_insert, overwrite=False)
            success_count = sum(1 for r in results if not r.get("error"))
            if success_count < len(documents_to_insert):
                errors = [r.get("errorMessage") for r in results if r.get("error")]
                self.logger.warning(
                    f"Batch save intrusive thoughts: {success_count}/{len(documents_to_insert)} succeeded. Errors (first 3): {errors[:3]}"
                )
            else:
                self.logger.info(f"Successfully batch saved {success_count} intrusive thoughts.")
            return success_count > 0
        except Exception as e:
            self.logger.error(f"Failed to batch save intrusive thoughts: {e}", exc_info=True)
            return False

    async def get_random_intrusive_thought(self) -> dict | None:
        """Gets a random, unused intrusive thought."""
        try:
            _pool_collection = await self.ensure_collection_exists(self.INTRUSIVE_THOUGHTS_POOL_COLLECTION_NAME)
            # Check if there are any unused thoughts first to avoid error on empty set for RAND()
            count_query = f"RETURN LENGTH(FOR doc IN {self.INTRUSIVE_THOUGHTS_POOL_COLLECTION_NAME} FILTER doc.used == false LIMIT 1 RETURN 1)"
            count_res = await self.execute_query(count_query)
            if not count_res or count_res[0] == 0:  # No unused thoughts
                self.logger.info("No unused intrusive thoughts available to fetch randomly.")
                return None

            # Query for a random unused thought
            # Using SORT RAND() can be slow on large collections.
            # Alternative: Get total count of unused, pick a random skip value.
            # For simplicity, keeping RAND() for now if collection size is manageable.
            query = f"""
                FOR thought IN {self.INTRUSIVE_THOUGHTS_POOL_COLLECTION_NAME}
                    FILTER thought.used == false
                    SORT RAND()
                    LIMIT 1
                    RETURN thought
            """
            results = await self.execute_query(query)
            return results[0] if results else None
        except Exception as e:
            self.logger.error(f"Failed to get random intrusive thought: {e}", exc_info=True)
            return None

    async def mark_intrusive_thought_used(self, thought_key: str) -> bool:
        """Marks an intrusive thought as used by its _key."""
        if not thought_key:
            self.logger.warning("Cannot mark thought as used: thought_key is empty.")
            return False
        try:
            pool_collection = await self.ensure_collection_exists(self.INTRUSIVE_THOUGHTS_POOL_COLLECTION_NAME)
            # Ensure the document exists before trying to update
            if not await asyncio.to_thread(pool_collection.has, thought_key):
                self.logger.warning(f"Cannot mark thought '{thought_key}' as used: document not found.")
                return False
            await asyncio.to_thread(pool_collection.update, {"_key": thought_key, "used": True}, merge=True)
            self.logger.debug(f"Intrusive thought '{thought_key}' marked as used.")
            return True
        except Exception as e:
            self.logger.error(f"Failed to mark intrusive thought '{thought_key}' as used: {e}", exc_info=True)
            return False

    async def update_action_status_in_document(
        self,
        doc_key: str,
        action_id: str,
        status_update: dict[str, Any],
        expected_conditions: dict[str, Any] | None = None,  # Not used in this simplified version
    ) -> bool:
        """Updates the status of a specific action within a thought document."""
        if not doc_key or not action_id:
            self.logger.warning("doc_key and action_id are required to update action status.")
            return False
        try:
            thoughts_collection = await self.ensure_collection_exists(self.THOUGHTS_COLLECTION_NAME)
            doc = await asyncio.to_thread(thoughts_collection.get, doc_key)
            if not doc:
                self.logger.error(f"Cannot update action status: Thought document '{doc_key}' not found.")
                return False

            action_attempted_data = doc.get("action_attempted")
            if not isinstance(action_attempted_data, dict) or action_attempted_data.get("action_id") != action_id:
                self.logger.error(
                    f"Action with ID '{action_id}' not found or 'action_attempted' field is malformed in document '{doc_key}'. Current: {action_attempted_data}"
                )
                return False

            # Merge the new status_update into the existing action_attempted_data
            updated_action_data = {**action_attempted_data, **status_update}
            patch_for_db = {"action_attempted": updated_action_data}

            await asyncio.to_thread(thoughts_collection.update, {"_key": doc_key, **patch_for_db})
            self.logger.info(f"Action '{action_id}' status in document '{doc_key}' updated with: {status_update}")
            return True
        except Exception as e:
            self.logger.error(
                f"Failed to update action status for action '{action_id}' in doc '{doc_key}': {e}", exc_info=True
            )
            return False

    async def save_raw_chat_message(self, message_data: dict) -> bool:
        """
        (Deprecated) Saves a raw chat message, attempting to convert it to a v1.4.0 Event.
        It's recommended to construct a proper Event object upstream and use save_event_v14.
        """
        self.logger.warning("Usage of save_raw_chat_message is deprecated. Convert to Event object upstream.")
        try:
            event_id = str(message_data.get("message_id", uuid.uuid4()))
            msg_type = message_data.get("message_type", "unknown")  # e.g. group, private

            event_type_map = {
                "group": f"message.group.{message_data.get('sub_type', 'normal')}",
                "private": f"message.private.{message_data.get('sub_type', 'friend')}",
                "channel": f"message.channel.{message_data.get('sub_type', 'normal')}",  # Example
            }
            event_type = event_type_map.get(msg_type, f"message.{msg_type}.unknown")

            ts = message_data.get("time", time.time())  # Assume seconds if 'time'
            timestamp_ms = int(ts) if ts > 1000000000000.0 else int(ts * 1000.0)  # Convert based on threshold

            content_list: list[dict] = []
            raw_message_content = message_data.get("message", message_data.get("raw_message", ""))
            if isinstance(raw_message_content, str):
                content_list.append({"type": "text", "data": {"text": raw_message_content}})
            elif isinstance(raw_message_content, list):  # Assume it's already a list of Seg dicts
                content_list = [seg for seg in raw_message_content if isinstance(seg, dict)]

            user_info_dict = None
            sender_data = message_data.get("sender")
            if isinstance(sender_data, dict):
                user_info_dict = {
                    "user_id": str(sender_data.get("user_id")),
                    "user_nickname": sender_data.get("nickname"),
                    "user_cardname": sender_data.get("card"),
                }
            elif message_data.get("user_id"):  # Fallback
                user_info_dict = {"user_id": str(message_data.get("user_id"))}

            conv_info_dict = None
            if message_data.get("group_id"):
                conv_info_dict = {
                    "conversation_id": str(message_data.get("group_id")),
                    "type": "group",  # Or map from message_type
                    "name": message_data.get("group_name"),
                }
            elif msg_type == "private" and user_info_dict and user_info_dict.get("user_id"):
                conv_info_dict = {
                    "conversation_id": user_info_dict["user_id"],  # For DMs, conv_id is often user_id
                    "type": "private",
                    "name": user_info_dict.get("user_nickname"),
                }

            event_data_to_save = {
                "event_id": event_id,
                "event_type": event_type,
                "timestamp": timestamp_ms,
                "platform": str(message_data.get("platform", "unknown_platform")),
                "bot_id": str(message_data.get("self_id", "unknown_bot")),
                "content": content_list,
                "user_info": user_info_dict,
                "conversation_info": conv_info_dict,
                "protocol_version": "from_raw_chat_message_v0",  # Indicate source
                "raw_data": message_data,  # Store original message for debugging
                # Ensure extracted fields for querying are present
                "user_id_extracted": user_info_dict.get("user_id") if user_info_dict else None,
                "conversation_id_extracted": conv_info_dict.get("conversation_id") if conv_info_dict else None,
            }
            return await self.save_event_v14(event_data_to_save)
        except Exception as e:
            self.logger.error(
                f"Failed to save raw chat message due to conversion error or DB error: {e}", exc_info=True
            )
            return False

    async def execute_query(self, query: str, bind_vars: dict[str, Any] | None = None) -> list[dict] | None:
        """Executes an AQL query and returns a list of results, or None on error."""
        try:
            final_bind_vars = bind_vars or {}
            # self.logger.debug(f"Executing AQL (first 100 chars): {query[:100]}{'...' if len(query) > 100 else ''}")
            # if final_bind_vars:
            #     self.logger.debug(f"With bind_vars: {final_bind_vars}")

            # The `count=True` in `self.db.aql.execute` is for getting total number of documents
            # that would be returned without LIMIT, useful for pagination.
            # If not doing pagination, it can be omitted.
            cursor = await asyncio.to_thread(self.db.aql.execute, query, bind_vars=final_bind_vars, count=False)
            results = await asyncio.to_thread(list, cursor)

            # self.logger.debug(f"AQL query executed successfully, {len(results)} documents returned.")
            return results
        except AQLQueryExecuteError as e:
            self.logger.error(f"AQL query execution failed. Error: {e.errors()}")
            self.logger.error(f"Failed AQL Query: {query}")
            if bind_vars:
                self.logger.error(f"Failed AQL Bind Vars: {bind_vars}")
            return None
        except Exception as e:
            self.logger.error(f"An unexpected error occurred during AQL query execution: {e}", exc_info=True)
            return None

    async def close_client(self) -> None:
        """Closes the ArangoDB client connection."""
        if self.client:
            try:
                self.logger.info("Closing ArangoDB client connection...")
                # ArangoClient.close() is a synchronous method.
                await asyncio.to_thread(self.client.close)
                self.logger.info("ArangoDB client connection closed.")
            except Exception as e:
                self.logger.error(f"Error closing ArangoDB client: {e}", exc_info=True)
            finally:
                self.client = None  # type: ignore # Mark as closed
                self.db = None  # type: ignore
        else:
            self.logger.info("ArangoDB client was not initialized or already closed.")
