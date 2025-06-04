# src/database/arangodb_handler.py
import asyncio
import datetime
import os
import time
import uuid
from typing import Any

from arango import ArangoClient
from arango.collection import StandardCollection
from arango.database import StandardDatabase
from arango.exceptions import (
    AQLQueryExecuteError,
    ArangoClientError,
    ArangoServerError,
)

from src.common.custom_logging.logger_manager import get_logger  # 假设路径正确

module_logger = get_logger("AIcarusCore.database")


class ArangoDBHandler:
    """ArangoDB 数据库处理器"""

    # 集合名称常量 - 只保留需要的
    THOUGHTS_COLLECTION_NAME = "thoughts_collection"
    INTRUSIVE_THOUGHTS_POOL_COLLECTION_NAME = "intrusive_thoughts_pool"
    ACTION_LOGS_COLLECTION_NAME = "action_logs"

    # v1.4.0协议集合
    EVENTS_COLLECTION_NAME = "events"
    USERS_COLLECTION_NAME = "users"
    CONVERSATIONS_COLLECTION_NAME = "conversations"
    CONVERSATION_MEMBERS_COLLECTION_NAME = "conversation_members"
    MESSAGE_CONTENT_COLLECTION_NAME = "message_content"

    def __init__(self, client: ArangoClient, db: StandardDatabase) -> None:
        self.client: ArangoClient = client
        self.db: StandardDatabase = db
        self.logger = get_logger(f"AIcarusCore.database.{self.__class__.__name__}")
        self.logger.info(f"ArangoDBHandler 实例已使用数据库 '{db.name}' 初始化。")

    @classmethod
    async def create_from_config(cls, database_config: dict[str, Any]) -> "ArangoDBHandler":
        """从配置对象创建ArangoDBHandler实例"""
        # 尝试从配置对象获取属性，支持不同的属性名
        host = (
            getattr(database_config, "host", None)
            or getattr(database_config, "url", None)
            or getattr(database_config, "arangodb_host", None)
        )
        username = (
            getattr(database_config, "username", None)
            or getattr(database_config, "user", None)
            or getattr(database_config, "arangodb_user", None)
        )
        password = getattr(database_config, "password", None) or getattr(database_config, "arangodb_password", None)
        database_name = (
            getattr(database_config, "name", None)
            or getattr(database_config, "database_name", None)
            or getattr(database_config, "arangodb_database", None)
        )

        # 如果配置对象没有这些属性，尝试从环境变量获取
        if not host:
            host = os.getenv("ARANGODB_HOST")
        if not username:
            username = os.getenv("ARANGODB_USER")
        if not password:
            password = os.getenv("ARANGODB_PASSWORD")
        if not database_name:
            database_name = os.getenv("ARANGODB_DATABASE")

        if not all([host, username, password, database_name]):
            missing_vars = []
            if not host:
                missing_vars.append("host/url")
            if not username:
                missing_vars.append("username/user")
            if not password:
                missing_vars.append("password")
            if not database_name:
                missing_vars.append("database_name/name")

            message = f"错误：ArangoDB 连接所需的配置参数未完全设置。缺失: {', '.join(missing_vars)}"
            module_logger.critical(message)
            raise ValueError(message)

        try:
            client_instance: ArangoClient = await asyncio.to_thread(ArangoClient, hosts=host)
            sys_db: StandardDatabase = await asyncio.to_thread(
                client_instance.db, "_system", username=username, password=password
            )
            if not await asyncio.to_thread(sys_db.has_database, database_name):
                module_logger.info(f"数据库 '{database_name}' 不存在，正在尝试创建...")
                await asyncio.to_thread(sys_db.create_database, database_name)
                module_logger.info(f"数据库 '{database_name}' 创建成功。")
            db_instance: StandardDatabase = await asyncio.to_thread(
                client_instance.db, database_name, username=username, password=password
            )
            await asyncio.to_thread(db_instance.properties)  # 验证连接
            module_logger.info(f"成功连接到 ArangoDB！主机: {host}, 数据库: {database_name}")
            return cls(client_instance, db_instance)
        except (ArangoServerError, ArangoClientError) as e:
            message = f"连接 ArangoDB 时发生错误 (Host: {host}, DB: {database_name}): {e}"
            module_logger.critical(message, exc_info=True)
            raise RuntimeError(message) from e
        except Exception as e:
            message = f"连接 ArangoDB 时发生未知或权限错误 (Host: {host}, DB: {database_name}, User: {username}): {e}"
            module_logger.critical(message, exc_info=True)
            raise RuntimeError(message) from e

    @classmethod
    async def create(cls) -> "ArangoDBHandler":
        """从环境变量创建ArangoDBHandler实例（保持向后兼容）"""
        host_env_var_name = "ARANGODB_HOST"
        user_env_var_name = "ARANGODB_USER"
        password_env_var_name = "ARANGODB_PASSWORD"
        database_env_var_name = "ARANGODB_DATABASE"

        host = os.getenv(host_env_var_name)
        user = os.getenv(user_env_var_name)
        password = os.getenv(password_env_var_name)
        db_name_from_env = os.getenv(database_env_var_name)

        if not all([host, user, password, db_name_from_env]):
            missing_vars = [
                var_name
                for var_name, var_val in [
                    (host_env_var_name, host),
                    (user_env_var_name, user),
                    (password_env_var_name, password),
                    (database_env_var_name, db_name_from_env),
                ]
                if not var_val
            ]
            message = f"错误：ArangoDB 连接所需的环境变量未完全设置。缺失: {', '.join(missing_vars)}"
            module_logger.critical(message)
            raise ValueError(message)
        try:
            client_instance: ArangoClient = await asyncio.to_thread(ArangoClient, hosts=host)
            sys_db: StandardDatabase = await asyncio.to_thread(
                client_instance.db, "_system", username=user, password=password
            )
            if not await asyncio.to_thread(sys_db.has_database, db_name_from_env):
                module_logger.info(f"数据库 '{db_name_from_env}' 不存在，正在尝试创建...")
                await asyncio.to_thread(sys_db.create_database, db_name_from_env)
                module_logger.info(f"数据库 '{db_name_from_env}' 创建成功。")
            db_instance: StandardDatabase = await asyncio.to_thread(
                client_instance.db, db_name_from_env, username=user, password=password
            )
            await asyncio.to_thread(db_instance.properties)  # 验证连接
            module_logger.info(f"成功连接到 ArangoDB！主机: {host}, 数据库: {db_name_from_env}")
            return cls(client_instance, db_instance)
        except (ArangoServerError, ArangoClientError) as e:
            message = f"连接 ArangoDB 时发生错误 (Host: {host}, DB: {db_name_from_env}): {e}"
            module_logger.critical(message, exc_info=True)
            raise RuntimeError(message) from e
        except Exception as e:  # 捕获其他可能的异常，如权限问题
            message = f"连接 ArangoDB 时发生未知或权限错误 (Host: {host}, DB: {db_name_from_env}, User: {user}): {e}"
            module_logger.critical(message, exc_info=True)
            raise RuntimeError(message) from e

    async def ensure_collection_exists(self, collection_name: str) -> StandardCollection:
        if not await asyncio.to_thread(self.db.has_collection, collection_name):
            self.logger.info(f"集合 '{collection_name}' 不存在，正在创建...")
            collection = await asyncio.to_thread(self.db.create_collection, collection_name)

            # 简化索引创建 - 只创建必要的
            if collection_name == self.EVENTS_COLLECTION_NAME:
                await self._create_events_indexes(collection)
            elif collection_name == self.THOUGHTS_COLLECTION_NAME:
                await self._create_thoughts_indexes(collection)
            elif collection_name == self.ACTION_LOGS_COLLECTION_NAME:
                await self._create_action_logs_indexes(collection)

            self.logger.info(f"集合 '{collection_name}' 创建成功。")
            return collection

        return await asyncio.to_thread(self.db.collection, collection_name)

    async def _create_events_indexes(self, collection: StandardCollection) -> None:
        """为Events集合创建索引"""
        indexes = [
            (["event_type", "timestamp"], False),
            (["platform", "bot_id", "timestamp"], False),
            (["conversation_id", "timestamp"], False),
            (["timestamp"], False),
        ]

        for fields, unique in indexes:
            try:
                await asyncio.to_thread(
                    collection.add_persistent_index, fields=fields, unique=unique, in_background=True
                )
            except Exception as e:
                self.logger.warning(f"创建索引 {fields} 失败: {e}")

    async def _create_thoughts_indexes(self, collection: StandardCollection) -> None:
        """为思考集合创建索引"""
        try:
            await asyncio.to_thread(
                collection.add_persistent_index, fields=["timestamp"], unique=False, in_background=True
            )
        except Exception as e:
            self.logger.warning(f"创建思考索引失败: {e}")

    async def _create_action_logs_indexes(self, collection: StandardCollection) -> None:
        """为动作日志集合创建索引"""
        indexes = [
            (["action_id"], True),
            (["timestamp"], False),
        ]

        for fields, unique in indexes:
            try:
                await asyncio.to_thread(
                    collection.add_persistent_index, fields=fields, unique=unique, in_background=True
                )
            except Exception as e:
                self.logger.warning(f"创建索引 {fields} 失败: {e}")

    async def save_event_v14(self, event_data: dict) -> bool:
        """保存v1.4.0格式的事件 - 简化版本"""
        try:
            await self.ensure_collection_exists(self.EVENTS_COLLECTION_NAME)

            if not event_data.get("event_id"):
                event_data["event_id"] = str(uuid.uuid4())

            if not event_data.get("timestamp"):
                event_data["timestamp"] = time.time() * 1000.0

            if not event_data.get("protocol_version"):
                event_data["protocol_version"] = "1.4.0"

            collection = self.db.collection(self.EVENTS_COLLECTION_NAME)
            result = await asyncio.to_thread(collection.insert, event_data)

            return bool(result.get("_id"))

        except Exception as e:
            self.logger.error(f"保存事件失败: {e}", exc_info=True)
            return False

    async def get_recent_chat_messages_for_context(
        self, duration_minutes: int = 10, conversation_id: str = None, limit: int = 50
    ) -> list[dict]:
        """获取最近的聊天消息 - 直接从Events获取"""
        try:
            await self.ensure_collection_exists(self.EVENTS_COLLECTION_NAME)

            current_time_ms = time.time() * 1000.0
            threshold_time_ms = current_time_ms - (duration_minutes * 60 * 1000)

            filters = ["event.timestamp >= @threshold_time", "event.event_type LIKE 'message.%'"]
            bind_vars = {"threshold_time": threshold_time_ms, "limit": limit}

            if conversation_id:
                filters.append("event.conversation_id == @conversation_id")
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
            return results or []

        except Exception as e:
            self.logger.error(f"获取最近聊天消息失败: {e}", exc_info=True)
            return []

    async def get_latest_thought_document_raw(self, limit: int = 1) -> list[dict]:
        """获取最新的思考文档"""
        try:
            await self.ensure_collection_exists(self.THOUGHTS_COLLECTION_NAME)

            query = f"""
                FOR thought IN {self.THOUGHTS_COLLECTION_NAME}
                    SORT thought.timestamp DESC
                    LIMIT @limit
                    RETURN thought
            """

            bind_vars = {"limit": limit}
            results = await self.execute_query(query, bind_vars)
            return results or []

        except Exception as e:
            self.logger.error(f"获取最新思考文档失败: {e}", exc_info=True)
            return []

    async def save_thought_document(self, document: dict[str, Any]) -> str | None:
        """保存思考文档"""
        try:
            if "timestamp" not in document:
                document["timestamp"] = datetime.datetime.now(datetime.UTC).isoformat()

            result = await asyncio.to_thread(self.db.collection(self.THOUGHTS_COLLECTION_NAME).insert, document)

            if result and "_key" in result:
                return result["_key"]
            return None

        except Exception as e:
            self.logger.error(f"保存思考文档失败: {e}", exc_info=True)
            return None

    async def save_intrusive_thoughts_batch(self, thoughts_list: list) -> bool:
        """批量保存侵入性思维"""
        try:
            if not thoughts_list:
                return True

            await self.ensure_collection_exists(self.INTRUSIVE_THOUGHTS_POOL_COLLECTION_NAME)

            thoughts_to_insert = []
            current_time = time.time() * 1000.0

            for thought in thoughts_list:
                if isinstance(thought, str):
                    thought_doc = {"thought_id": str(uuid.uuid4()), "text": thought, "timestamp": current_time}
                elif isinstance(thought, dict):
                    thought_doc = thought.copy()
                    if "thought_id" not in thought_doc:
                        thought_doc["thought_id"] = str(uuid.uuid4())
                    if "timestamp" not in thought_doc:
                        thought_doc["timestamp"] = current_time
                else:
                    continue

                thoughts_to_insert.append(thought_doc)

            if thoughts_to_insert:
                collection = self.db.collection(self.INTRUSIVE_THOUGHTS_POOL_COLLECTION_NAME)
                results = await asyncio.to_thread(collection.insert_many, thoughts_to_insert)
                return len([r for r in results if r.get("_id")]) > 0

            return False

        except Exception as e:
            self.logger.error(f"批量保存侵入性思维失败: {e}", exc_info=True)
            return False

    async def get_random_intrusive_thought(self) -> dict | None:
        """获取随机侵入性思维"""
        try:
            await self.ensure_collection_exists(self.INTRUSIVE_THOUGHTS_POOL_COLLECTION_NAME)

            query = f"""
                FOR thought IN {self.INTRUSIVE_THOUGHTS_POOL_COLLECTION_NAME}
                    SORT RAND()
                    LIMIT 1
                    RETURN thought
            """

            results = await self.execute_query(query)
            return results[0] if results else None

        except Exception as e:
            self.logger.error(f"获取随机侵入性思维失败: {e}", exc_info=True)
            return None

    async def update_action_status_in_document(
        self,
        doc_key: str,
        action_id: str,
        status_update: dict[str, Any],
        expected_conditions: dict[str, Any] | None = None,
    ) -> bool:
        """更新思考文档中特定动作的状态"""
        try:
            # 简化更新逻辑，不做复杂的条件检查
            bind_vars = {"doc_key": doc_key, "action_id": action_id}

            # 构建更新对象
            update_parts = []
            for key, value in status_update.items():
                var_name = f"new_{key}"
                update_parts.append(f'"{key}": @{var_name}')
                bind_vars[var_name] = value

            update_object = "{" + ", ".join(update_parts) + "}"

            query = f"""
                FOR doc IN {self.THOUGHTS_COLLECTION_NAME}
                    FILTER doc._key == @doc_key
                    FILTER doc.action_attempted != null
                    FILTER doc.action_attempted.action_id == @action_id
                    UPDATE doc WITH {{
                        action_attempted: MERGE(doc.action_attempted, {update_object})
                    }} IN {self.THOUGHTS_COLLECTION_NAME}
                    RETURN NEW
            """

            result = await self.execute_query(query, bind_vars)
            return len(result) > 0

        except Exception as e:
            self.logger.error(f"更新动作状态失败: {e}", exc_info=True)
            return False

    async def save_raw_chat_message(self, message_data: dict) -> bool:
        """保存原始聊天消息 - 兼容旧接口，实际保存为事件"""
        try:
            # 将旧格式消息转换为新的事件格式
            event_data = {
                "event_id": message_data.get("message_id", str(uuid.uuid4())),
                "event_type": "message.group.normal" if message_data.get("group_id") else "message.private.friend",
                "timestamp": message_data.get("timestamp", time.time() * 1000.0),
                "platform": message_data.get("platform", "unknown"),
                "bot_id": message_data.get("bot_id", "unknown"),
                "conversation_id": message_data.get("group_id") or message_data.get("sender_id", "unknown"),
                "sender_id": message_data.get("sender_id"),
                "content": message_data.get("message_content", []),
                # 保留原始数据
                "raw_data": message_data,
            }

            return await self.save_event_v14(event_data)

        except Exception as e:
            self.logger.error(f"保存原始聊天消息失败: {e}", exc_info=True)
            return False

    async def execute_query(self, query: str, bind_vars: dict = None) -> list:
        """执行AQL查询"""
        try:
            bind_vars = bind_vars or {}
            self.logger.debug(f"执行AQL查询: {query[:100]}{'...' if len(query) > 100 else ''}")
            if bind_vars:
                self.logger.debug(f"绑定变量: {bind_vars}")

            cursor = await asyncio.to_thread(self.db.aql.execute, query, bind_vars=bind_vars)
            results = await asyncio.to_thread(list, cursor)

            self.logger.debug(f"查询执行成功，返回 {len(results)} 条结果")
            return results

        except AQLQueryExecuteError as e:
            self.logger.error(f"AQL查询执行错误: {e}")
            self.logger.error(f"查询语句: {query}")
            self.logger.error(f"绑定变量: {bind_vars}")
            raise
        except Exception as e:
            self.logger.error(f"查询执行时发生未知错误: {e}", exc_info=True)
            return []
