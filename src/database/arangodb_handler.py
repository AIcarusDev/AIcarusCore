# src/database/arangodb_handler.py
import asyncio
import datetime
import os
import re
import uuid
from typing import Any, Dict, List # 确保导入 Dict 和 List

from arango import ArangoClient
from arango.collection import StandardCollection
from arango.database import StandardDatabase
from arango.exceptions import (
    AQLQueryExecuteError,
    ArangoClientError,
    ArangoServerError,
    DocumentInsertError,
    DocumentUpdateError,
)

from src.common.custom_logging.logger_manager import get_logger

module_logger = get_logger("AIcarusCore.database")


class ArangoDBHandler:
    RAW_CHAT_MESSAGES_COLLECTION_NAME = "RawChatMessages"
    THOUGHTS_COLLECTION_NAME = "thoughts_collection"
    INTRUSIVE_THOUGHTS_POOL_COLLECTION_NAME = "intrusive_thoughts_pool"
    # 🐾 小猫爪：在这里定义我们新的集合名称！
    SUB_MIND_ACTIVITY_LOG_COLLECTION_NAME = "SubMindActivityLog"

    def __init__(self, client: ArangoClient, db: StandardDatabase) -> None:
        self.client: ArangoClient = client
        self.db: StandardDatabase = db
        self.logger = get_logger(f"AIcarusCore.database.{self.__class__.__name__}")
        self.logger.info(f"ArangoDBHandler 实例已使用数据库 '{db.name}' 初始化。")

    @classmethod
    async def create(cls) -> "ArangoDBHandler":
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
            await asyncio.to_thread(db_instance.properties)
            module_logger.info(f"成功连接到 ArangoDB！主机: {host}, 数据库: {db_name_from_env}")
            return cls(client_instance, db_instance)
        except (ArangoServerError, ArangoClientError) as e:
            message = f"连接 ArangoDB 时发生错误 (Host: {host}, DB: {db_name_from_env}): {e}"
            module_logger.critical(message, exc_info=True)
            raise RuntimeError(message) from e
        except Exception as e:
            message = f"连接 ArangoDB 时发生未知或权限错误 (Host: {host}, DB: {db_name_from_env}, User: {user}): {e}"
            module_logger.critical(message, exc_info=True)
            raise RuntimeError(message) from e

    async def ensure_collection_exists(self, collection_name: str) -> StandardCollection:
        if not await asyncio.to_thread(self.db.has_collection, collection_name):
            self.logger.info(f"集合 '{collection_name}' 在数据库 '{self.db.name}' 中不存在，正在创建...")
            collection = await asyncio.to_thread(self.db.create_collection, collection_name)
            if collection_name == self.RAW_CHAT_MESSAGES_COLLECTION_NAME:
                self.logger.info(
                    f"为集合 '{self.RAW_CHAT_MESSAGES_COLLECTION_NAME}' 创建复合索引 ['conversation_id', 'timestamp']..."
                )
                await asyncio.to_thread(
                    collection.add_persistent_index, # type: ignore
                    fields=["conversation_id", "timestamp"],
                    unique=False,
                    sparse=False,
                    in_background=True,
                )
                self.logger.info(
                    f"为集合 '{self.RAW_CHAT_MESSAGES_COLLECTION_NAME}' 创建时间戳索引 ['timestamp'] (数值型)..."
                )
                await asyncio.to_thread(
                    collection.add_persistent_index, # type: ignore
                    fields=["timestamp"],
                    unique=False,
                    sparse=False,
                    in_background=True,
                )
            # 🐾 小猫爪：如果创建的是新的子思维活动日志集合，也可以考虑在这里添加索引
            elif collection_name == self.SUB_MIND_ACTIVITY_LOG_COLLECTION_NAME:
                self.logger.info(
                    f"为集合 '{self.SUB_MIND_ACTIVITY_LOG_COLLECTION_NAME}' 创建索引 ['conversation_id', 'timestamp'] (用于按会话和时间查询)..."
                )
                await asyncio.to_thread(
                    collection.add_persistent_index, # type: ignore
                    fields=["conversation_id", "timestamp"], # 假设这个集合也有这两个字段
                    unique=False, # 通常活动日志不需要唯一索引
                    sparse=True, # 如果某些日志可能没有这些字段，可以设为True
                    in_background=True,
                )
                self.logger.info(
                    f"为集合 '{self.SUB_MIND_ACTIVITY_LOG_COLLECTION_NAME}' 创建时间戳索引 ['timestamp']..."
                )
                await asyncio.to_thread(
                    collection.add_persistent_index, # type: ignore
                    fields=["timestamp"],
                    unique=False,
                    sparse=False,
                    in_background=True,
                )
            # 🐾 小懒猫加的：为侵入性思维池创建索引
            elif collection_name == self.INTRUSIVE_THOUGHTS_POOL_COLLECTION_NAME: #
                self.logger.info( #
                    f"为集合 '{self.INTRUSIVE_THOUGHTS_POOL_COLLECTION_NAME}' 创建索引 ['used', 'timestamp_generated']..." #
                ) #
                await asyncio.to_thread( #
                    collection.add_persistent_index, # type: ignore
                    fields=["used", "timestamp_generated"], #
                    unique=False, #
                    sparse=False, #
                    in_background=True, #
                ) #
            
            self.logger.info(f"集合 '{collection_name}' 创建成功。")
            return collection # type: ignore
        self.logger.debug(f"集合 '{collection_name}' 已在数据库 '{self.db.name}' 中存在。")
        return await asyncio.to_thread(self.db.collection, collection_name)

    async def save_raw_chat_message(self, message_data: Dict[str, Any]) -> str | None:
        if not message_data.get("conversation_id"):
            self.logger.error("保存聊天消息失败：缺少 conversation_id。")
            return None
        ts_value = message_data.get("timestamp")
        final_numeric_timestamp: int | None = None
        if isinstance(ts_value, int | float):
            final_numeric_timestamp = int(ts_value)
        elif isinstance(ts_value, str):
            self.logger.warning(
                f"消息 {message_data.get('platform_message_id', 'N/A')} 的顶层 timestamp 是字符串 '{ts_value}'，将尝试转换为数值型UTC毫秒。"
            )
            try:
                dt_obj = datetime.datetime.fromisoformat(ts_value.replace("Z", "+00:00"))
                if dt_obj.tzinfo is None or dt_obj.tzinfo.utcoffset(dt_obj) is None:
                    aware_dt = dt_obj.replace(tzinfo=datetime.timezone.utc)
                    self.logger.debug(f"Naive ISO string '{ts_value}' 被假定为 UTC。")
                else:
                    aware_dt = dt_obj.astimezone(datetime.timezone.utc)
                final_numeric_timestamp = int(aware_dt.timestamp() * 1000)
                self.logger.info(f"成功将字符串时间戳 '{ts_value}' 转换为数值毫秒: {final_numeric_timestamp}")
            except ValueError as e:
                self.logger.error(
                    f"无法将字符串时间戳 '{ts_value}' 转换为datetime对象: {e}。将尝试使用 raw_message_info_dump.time。"
                )
        if (
            final_numeric_timestamp is None
            and "raw_message_info_dump" in message_data
            and isinstance(message_data["raw_message_info_dump"], dict)
            and "time" in message_data["raw_message_info_dump"]
        ):
            raw_time = message_data["raw_message_info_dump"]["time"]
            if isinstance(raw_time, int | float):
                final_numeric_timestamp = int(raw_time)
                self.logger.info(
                    f"使用了 raw_message_info_dump.time ({final_numeric_timestamp}) 作为消息的顶层时间戳。"
                )
            else:
                self.logger.warning(f"raw_message_info_dump.time 的类型不是数字 ({type(raw_time)})，无法用作时间戳。")
        if final_numeric_timestamp is None:
            current_time_ms = int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)
            self.logger.warning(
                f"消息 {message_data.get('platform_message_id', 'N/A')} 无法确定有效时间戳，"
                f"将使用当前UTC时间 ({current_time_ms} ms) 作为回退。"
            )
            final_numeric_timestamp = current_time_ms
        message_data["timestamp"] = final_numeric_timestamp
        if not message_data.get("conversation_id") or message_data.get("timestamp") is None:
            self.logger.error("保存聊天消息失败：关键字段 conversation_id 或转换后的 timestamp 丢失。")
            return None
        try:
            chat_messages_collection = await self.ensure_collection_exists(self.RAW_CHAT_MESSAGES_COLLECTION_NAME)
            key_to_use: str
            if "_key" in message_data and message_data["_key"]:
                key_to_use = str(message_data["_key"])
            elif "platform_message_id" in message_data and message_data["platform_message_id"]:
                candidate_key = str(message_data["platform_message_id"])
                candidate_key = re.sub(r"[^a-zA-Z0-9_:.@()+,$\!*\'=-]", "_", candidate_key)
                if not candidate_key or candidate_key.startswith(("_", "-")) or len(candidate_key) > 254:
                    self.logger.debug(
                        f"平台消息ID '{message_data['platform_message_id']}' 处理后 ('{candidate_key}') 不适合作为 _key，将生成UUID。"
                    )
                    key_to_use = str(uuid.uuid4())
                else:
                    key_to_use = candidate_key
            else:
                key_to_use = str(uuid.uuid4())
            message_data["_key"] = key_to_use
            insert_result = await asyncio.to_thread(chat_messages_collection.insert, message_data, overwrite=False)
            doc_key = insert_result.get("_key")
            if doc_key:
                self.logger.info(
                    f"聊天消息 (文档 Key: {doc_key}, 会话: {message_data.get('conversation_id')}, "
                    f"数值时间戳: {message_data.get('timestamp')}) 已成功保存到集合 '{self.RAW_CHAT_MESSAGES_COLLECTION_NAME}'。"
                )
                return str(doc_key)
            else:
                self.logger.error(f"错误：保存聊天消息到 ArangoDB 后未能获取文档 _key。Insert result: {insert_result}")
                return None
        except DocumentInsertError as e_insert:
            if e_insert.http_code == 409:
                self.logger.warning(
                    f"聊天消息 (Key: {message_data.get('_key')}) 已存在于 '{self.RAW_CHAT_MESSAGES_COLLECTION_NAME}'。"
                    f"错误: {e_insert.http_code} - {e_insert.error_message}"
                )
                return message_data.get("_key")
            self.logger.error(
                f"错误：保存聊天消息到 ArangoDB 集合 '{self.RAW_CHAT_MESSAGES_COLLECTION_NAME}' 失败 (DocumentInsertError): {e_insert}",
                exc_info=True,
            )
            return None
        except (ArangoServerError, ArangoClientError) as e_db:
            self.logger.error(
                f"错误：保存聊天消息到 ArangoDB 集合 '{self.RAW_CHAT_MESSAGES_COLLECTION_NAME}' 时发生数据库错误: {e_db}",
                exc_info=True,
            )
            return None
        except Exception as e:
            self.logger.error(
                f"错误：保存聊天消息到 ArangoDB 集合 '{self.RAW_CHAT_MESSAGES_COLLECTION_NAME}' 时发生未知错误: {e}",
                exc_info=True,
            )
            return None

    async def get_recent_chat_messages(self, conversation_id: str, limit: int = 30) -> List[Dict[str, Any]]:
        if not conversation_id:
            self.logger.warning("获取最近聊天记录失败：未提供 conversation_id。")
            return []
        self.logger.debug(
            f"正在从集合 '{self.RAW_CHAT_MESSAGES_COLLECTION_NAME}' 获取会话 '{conversation_id}' 的最近 {limit} 条消息 (基于数值时间戳)..."
        )
        try:
            aql_query = """
                LET latest_messages_subquery = (
                    FOR doc IN @@collection_name
                        FILTER doc.conversation_id == @conversation_id
                        SORT doc.timestamp DESC
                        LIMIT @limit
                        RETURN doc
                )
                FOR message IN latest_messages_subquery
                    SORT message.timestamp ASC
                    RETURN message
            """
            bind_vars = {
                "@collection_name": self.RAW_CHAT_MESSAGES_COLLECTION_NAME,
                "conversation_id": conversation_id,
                "limit": limit,
            }
            cursor = await asyncio.to_thread(self.db.aql.execute, aql_query, bind_vars=bind_vars, ttl=30)
            messages = list(cursor) # type: ignore
            self.logger.debug(f"为会话 '{conversation_id}' 获取到 {len(messages)} 条最近消息 (数值时间戳)。")
            return messages
        except (AQLQueryExecuteError, ArangoServerError, ArangoClientError) as e_db:
            self.logger.error(
                f"获取会话 '{conversation_id}' 的最近聊天记录时发生数据库错误: {e_db}",
                exc_info=True,
            )
            return []
        except Exception as e:
            self.logger.error(
                f"获取会话 '{conversation_id}' 的最近聊天记录时发生未知错误: {e}",
                exc_info=True,
            )
            return []

    async def get_recent_chat_messages_for_context(
        self, duration_minutes: int, conversation_id: str | None = None, limit_per_conversation: int = 20
    ) -> List[Dict[str, Any]]:
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        cutoff_datetime_obj = now_utc - datetime.timedelta(minutes=duration_minutes)
        calculated_cutoff_ms_in_python = int(cutoff_datetime_obj.timestamp() * 1000)
        self.logger.debug(
            f"获取最近 {duration_minutes} 分钟的聊天记录上下文。截止时间 (Python计算的UTC毫秒): {calculated_cutoff_ms_in_python} (对应ISO: {cutoff_datetime_obj.isoformat()})."
            f"{' 特定会话: ' + conversation_id if conversation_id else ' 所有相关会话。'}"
        )
        let_statements = """
            LET rawDump = doc.raw_message_info_dump
            LET userInfo = IS_OBJECT(rawDump.user_info) ? rawDump.user_info : {}
            LET groupInfo = IS_OBJECT(rawDump.group_info) ? rawDump.group_info : {}
        """
        return_statement = """
            RETURN {
                _key: doc._key,
                platform: rawDump.platform,
                group_id: groupInfo.group_id,
                group_name: groupInfo.group_name,
                message_id: doc.platform_message_id,
                timestamp: doc.timestamp,
                sender_id: userInfo.user_id,
                sender_nickname: userInfo.user_nickname,
                sender_group_card: userInfo.user_cardname,
                sender_group_titlename: userInfo.user_titlename,
                sender_group_permission: (userInfo.permission_level != null ? userInfo.permission_level : userInfo.role),
                post_type: doc.message_type,
                sub_type: rawDump.sub_type,
                message_content: doc.content_segments,
                conversation_id: doc.conversation_id
            }
        """
        aql_query: str
        bind_vars: Dict[str, Any] = {
            "@messages_collection": self.RAW_CHAT_MESSAGES_COLLECTION_NAME,
            "cutoff_timestamp_ms_param": calculated_cutoff_ms_in_python,
        }
        filter_condition_time_aql = "doc.timestamp >= @cutoff_timestamp_ms_param"
        sort_timestamp_field_aql = "doc.timestamp"
        if conversation_id:
            aql_query = f"""
                FOR doc IN @@messages_collection
                    FILTER {filter_condition_time_aql} AND doc.conversation_id == @conversation_id
                    SORT {sort_timestamp_field_aql} DESC
                    LIMIT @limit_per_conv
                    {let_statements}
                    SORT {sort_timestamp_field_aql} ASC
                    {return_statement}
            """
            bind_vars["conversation_id"] = conversation_id
            bind_vars["limit_per_conv"] = limit_per_conversation
        else:
            self.logger.info("正在获取所有会话的聊天记录上下文（使用Python计算的数值型截止时间戳进行过滤）。")
            aql_query = f"""
                FOR doc IN @@messages_collection
                    FILTER {filter_condition_time_aql}
                    {let_statements}
                    SORT {sort_timestamp_field_aql} ASC
                    {return_statement}
            """
        self.logger.info(f"即将执行的AQL查询语句: {aql_query}")
        self.logger.info(f"AQL查询的绑定变量 (bind_vars): {bind_vars}")
        try:
            self.logger.debug(
                f"Executing AQL for chat context (numeric top-level timestamp filter): {aql_query} with bind_vars: {bind_vars}"
            )
            cursor = await asyncio.to_thread(self.db.aql.execute, aql_query, bind_vars=bind_vars, ttl=60)
            messages_from_db = list(cursor) # type: ignore
            self.logger.info(f"AQL查询直接从cursor转换后的原始结果数量: {len(messages_from_db)}")
            processed_messages = []
            for msg_data in messages_from_db:
                content = msg_data.get("message_content")
                if not isinstance(content, list):
                    msg_data["message_content"] = [{"type": "text", "data": {"text": str(content or "")}}]
                processed_messages.append(msg_data)
            self.logger.debug(f"获取到 {len(processed_messages)} 条用于上下文的聊天记录 (经数值型顶层时间戳过滤)。")
            return processed_messages
        except (AQLQueryExecuteError, ArangoServerError, ArangoClientError) as e_db:
            self.logger.error(f"获取聊天记录上下文时发生数据库错误 (数值型顶层时间戳过滤): {e_db}", exc_info=True)
            return []
        except Exception as e:
            self.logger.error(f"获取聊天记录上下文时发生未知错误 (数值型顶层时间戳过滤): {e}", exc_info=True)
            return []

    async def get_latest_thought_document_raw(self) -> Dict[str, Any] | None:
        collection_name = self.THOUGHTS_COLLECTION_NAME
        self.logger.debug(
            f"在 get_latest_thought_document_raw 中：准备从 ArangoDB 集合 '{collection_name}' (数据库: '{self.db.name}') 获取最新思考文档..."
        )
        try:
            aql_query = """
                FOR doc IN @@collection_name
                    SORT doc.timestamp DESC
                    LIMIT 1
                    RETURN doc
            """
            bind_vars = {"@collection_name": collection_name}
            cursor = await asyncio.to_thread(self.db.aql.execute, aql_query, bind_vars=bind_vars, ttl=30)
            latest_document = next(cursor, None) # type: ignore
            self.logger.debug(
                f"在 get_latest_thought_document_raw 中：AQL 查询完成。是否找到思考文档: {latest_document is not None}"
            )
            return latest_document
        except (AQLQueryExecuteError, ArangoServerError, ArangoClientError) as e_db:
            self.logger.error(
                f"在 get_latest_thought_document_raw 中执行 ArangoDB 操作时发生错误 (集合: {collection_name}): {e_db}",
                exc_info=True,
            )
            return None
        except Exception as e:
            self.logger.error(
                f"在 get_latest_thought_document_raw 中获取最新思考文档时发生未知错误 (集合: {collection_name}): {e}",
                exc_info=True,
            )
            return None

    async def save_thought_document(self, document_to_save: Dict[str, Any]) -> str | None:
        if not document_to_save:
            self.logger.warning("save_thought_document 收到空的 document_to_save，不执行保存。")
            return None
        try:
            thoughts_collection = await self.ensure_collection_exists(self.THOUGHTS_COLLECTION_NAME)
            insert_result = await asyncio.to_thread(thoughts_collection.insert, document_to_save, overwrite=False)
            doc_key = insert_result.get("_key")
            if doc_key:
                self.logger.info(
                    f"思考结果 (文档 Key: {doc_key}) 已成功保存到 ArangoDB 集合 '{thoughts_collection.name}'。"
                )
                return str(doc_key)
            else:
                self.logger.error(f"错误：保存思考结果到 ArangoDB 后未能获取文档 _key。Insert result: {insert_result}")
                return None
        except DocumentInsertError as e_insert:
            self.logger.error(
                f"错误：保存思考结果到 ArangoDB 集合 '{self.THOUGHTS_COLLECTION_NAME}' 失败 (DocumentInsertError): {e_insert}",
                exc_info=True,
            )
            return None
        except (ArangoServerError, ArangoClientError) as e_db:
            self.logger.error(
                f"错误：保存思考结果到 ArangoDB 集合 '{self.THOUGHTS_COLLECTION_NAME}' 时发生数据库错误: {e_db}",
                exc_info=True,
            )
            return None
        except Exception as e:
            self.logger.error(
                f"错误：保存思考结果到 ArangoDB 集合 '{self.THOUGHTS_COLLECTION_NAME}' 时发生未知错误: {e}",
                exc_info=True,
            )
            return None

    async def mark_action_result_as_seen(self, action_id: str = "") -> None:
        if not action_id:
            self.logger.debug("mark_action_result_as_seen 收到空的 action_id，不执行操作。")
            return
        collection_name = self.THOUGHTS_COLLECTION_NAME
        aql_query = """
            FOR doc IN @@collection_name
                FILTER doc.action_attempted.action_id == @action_id
                    AND doc.action_attempted.status IN ["COMPLETED_SUCCESS", "COMPLETED_FAILURE", "CRITICAL_FAILURE"]
                    AND (doc.action_attempted.result_seen_by_shuang == false OR !HAS(doc.action_attempted, "result_seen_by_shuang"))
                LIMIT 1
                UPDATE doc WITH {
                    action_attempted: MERGE(doc.action_attempted, {
                        result_seen_by_shuang: true,
                        updated_at: @timestamp
                    })
                } IN @@collection_name
                RETURN OLD
        """
        bind_vars = {
            "@collection_name": collection_name,
            "action_id": action_id,
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        try:
            update_cursor = await asyncio.to_thread(self.db.aql.execute, aql_query, bind_vars=bind_vars)
            stats = update_cursor.statistics()
            if stats and stats.get("writes_executed", 0) > 0:
                self.logger.info(
                    f"动作结果 (ID: {action_id[:8]}) 已在 ArangoDB 集合 '{collection_name}' 中成功标记为已阅。"
                )
        except (AQLQueryExecuteError, ArangoServerError, ArangoClientError) as e_db:
            self.logger.error(
                f"错误: 在 ArangoDB 集合 '{collection_name}' 中标记动作 (ID: {action_id[:8]}) 结果为已阅时发生数据库错误: {e_db}",
                exc_info=True,
            )
        except Exception as e:
            self.logger.error(
                f"错误: 在 ArangoDB 集合 '{collection_name}' 中标记动作 (ID: {action_id[:8]}) 结果为已阅时发生未知错误: {e}",
                exc_info=True,
            )

    async def update_intrusive_thought_status(self, thought_key: str, used: bool) -> bool: #
        """
        更新侵入性思维的 'used' 状态。
        Args:
            thought_key (str): 要更新的侵入性思维文档的 _key。
            used (bool): 设置为 True 表示已使用，False 表示未使用。
        Returns:
            bool: 更新是否成功。
        """
        if not thought_key: #
            self.logger.warning("更新侵入性思维状态失败：thought_key 为空。") #
            return False #
        collection_name = self.INTRUSIVE_THOUGHTS_POOL_COLLECTION_NAME #
        try:
            intrusive_thoughts_collection = await self.ensure_collection_exists(collection_name) #
            
            # 使用 AQL 进行更新以确保原子性
            aql_query = """
                FOR doc IN @@collection_name
                    FILTER doc._key == @thought_key
                    UPDATE doc WITH { used: @used_status, timestamp_used: @timestamp } IN @@collection_name
                    RETURN NEW._key
            """ #
            bind_vars = {
                "@collection_name": collection_name,
                "thought_key": thought_key,
                "used_status": used,
                "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat() if used else None
            } #

            update_cursor = await asyncio.to_thread(self.db.aql.execute, aql_query, bind_vars=bind_vars) #
            stats = update_cursor.statistics() #
            if stats and stats.get("writes_executed", 0) > 0: #
                self.logger.debug(f"侵入性思维 (Key: {thought_key}) 'used' 状态已成功更新为 {used}。") #
                return True #
            else:
                self.logger.warning(f"更新侵入性思维 (Key: {thought_key}) 'used' 状态失败，或文档未找到/无变化。") #
                return False #
        except (AQLQueryExecuteError, ArangoServerError, ArangoClientError) as e_db: #
            self.logger.error(
                f"更新侵入性思维 (Key: {thought_key}) 'used' 状态时发生数据库错误: {e_db}",
                exc_info=True,
            ) #
            return False #
        except Exception as e: #
            self.logger.error(
                f"更新侵入性思维 (Key: {thought_key}) 'used' 状态时发生未知错误: {e}",
                exc_info=True,
            ) #
            return False #

    async def update_action_status_in_document(
        self,
        doc_key: str,
        action_id_for_log: str,
        updates: Dict[str, Any],
        expected_conditions: Dict[str, Any] | None = None,
    ) -> bool:
        collection_name = self.THOUGHTS_COLLECTION_NAME
        if not doc_key:
            self.logger.error(
                f"错误: update_action_status_in_document 收到空的 doc_key (action_id: {action_id_for_log})。无法更新。"
            )
            return False
        update_object_for_merge = updates.copy()
        update_object_for_merge["updated_at"] = datetime.datetime.now(
            datetime.timezone.utc
        ).isoformat()
        merge_dict_aql_parts = []
        bind_vars_for_aql: Dict[str, Any] = {
            "doc_key_to_update": doc_key,
            "@collection_name": collection_name,
        }
        for key, value in update_object_for_merge.items():
            bind_key_name = f"update_val_{re.sub(r'[^a-zA-Z0-9_]', '_', key)}"
            merge_dict_aql_parts.append(f"'{key}': @{bind_key_name}")
            bind_vars_for_aql[bind_key_name] = value
        merge_object_aql_string = f"{{{', '.join(merge_dict_aql_parts)}}}"
        filter_clauses = ["doc._key == @doc_key_to_update"]
        conditions_were_specified = False
        if expected_conditions:
            conditions_were_specified = True
            for cond_key, cond_value in expected_conditions.items():
                bind_cond_key_name = f"cond_val_{re.sub(r'[^a-zA-Z0-9_]', '_', cond_key)}"
                filter_clauses.append(f"doc.action_attempted.{cond_key} == @{bind_cond_key_name}")
                bind_vars_for_aql[bind_cond_key_name] = cond_value
        filter_aql_string = " AND ".join(filter_clauses)
        aql_query = f"""
            FOR doc IN @@collection_name
                FILTER {filter_aql_string}
                LIMIT 1
                LET current_action_attempted = IS_OBJECT(doc.action_attempted) ? doc.action_attempted : {{}}
                LET merged_action_attempted = MERGE(current_action_attempted, {merge_object_aql_string})
                UPDATE doc WITH {{ action_attempted: merged_action_attempted }} IN @@collection_name
                OPTIONS {{ ignoreErrors: false }}
                RETURN OLD
        """
        try:
            update_cursor = await asyncio.to_thread(self.db.aql.execute, aql_query, bind_vars=bind_vars_for_aql)
            stats = update_cursor.statistics()
            writes_executed_count = stats.get("writes_executed", 0) if stats else 0
            old_doc_list = list(update_cursor) # type: ignore
            filter_passed_and_doc_found = len(old_doc_list) > 0
            if writes_executed_count > 0:
                self.logger.info(
                    f"成功更新 ArangoDB 中动作状态 (DocKey: {doc_key}, ActionID: {action_id_for_log}). "
                    f"更新内容: {updates}. 条件: {expected_conditions}. Writes: {writes_executed_count}"
                )
                return True
            else:
                doc_after_attempt_dict = await asyncio.to_thread(self.db.collection(collection_name).get, doc_key)
                action_attempted_after = (
                    doc_after_attempt_dict.get("action_attempted", {}) if doc_after_attempt_dict else None
                )
                current_db_val_str = (
                    str(action_attempted_after)[:300]
                    if action_attempted_after is not None
                    else "文档未找到或无action_attempted"
                )
                if conditions_were_specified and not filter_passed_and_doc_found:
                    self.logger.warning(
                        f"警告(条件不符): 更新 ArangoDB 中动作状态时，文档 _key '{doc_key}' (action_id: {action_id_for_log}) 更新未执行，因为期望条件未满足. "
                        f"意图更新: {updates}. 期望条件: {expected_conditions}. 当前DB值 (action_attempted): {current_db_val_str}..."
                    )
                else:
                    self.logger.debug(
                        f"调试(内容无变化或文档未找到): 更新 ArangoDB 中动作状态时，文档 _key '{doc_key}' (action_id: {action_id_for_log}) 数据未发生实际变化或文档可能未找到. "
                        f"意图更新: {updates}. 期望条件: {expected_conditions}. 当前DB值 (action_attempted): {current_db_val_str}..."
                    )
                return False
        except AQLQueryExecuteError as e_aql:
            if e_aql.http_code == 404 and e_aql.error_code == 1202:
                self.logger.warning(
                    f"警告: 更新 ArangoDB 中动作状态时，文档 _key '{doc_key}' (action_id: {action_id_for_log}) 在集合 '{collection_name}' 中未找到 (AQL Error 1202)。"
                )
            else:
                self.logger.error(
                    f"错误: 更新 ArangoDB 中动作 (DocKey: {doc_key}, ActionID: {action_id_for_log}, 集合: {collection_name}) 状态失败 (AQL执行错误): {e_aql}",
                    exc_info=True,
                )
            return False
        except (ArangoServerError, ArangoClientError, DocumentUpdateError) as e_db:
            self.logger.error(
                f"错误: 更新 ArangoDB 中动作 (DocKey: {doc_key}, ActionID: {action_id_for_log}, 集合: {collection_name}) 状态失败 (数据库服务器/客户端错误): {e_db}",
                exc_info=True,
            )
            return False
        except Exception as e:
            self.logger.error(
                f"错误: 更新 ArangoDB 中动作 (DocKey: {doc_key}, ActionID: {action_id_for_log}, 集合: {collection_name}) 状态失败 (未知错误): {e}",
                exc_info=True,
            )
            return False

    async def update_action_status_by_action_id(self, action_id: str, updates: Dict[str, Any]) -> bool:
        collection_name = self.THOUGHTS_COLLECTION_NAME
        if not action_id:
            self.logger.error("update_action_status_by_action_id: action_id 为空，无法更新。")
            return False
        update_object_for_merge = updates.copy()
        update_object_for_merge["updated_at"] = datetime.datetime.now(
            datetime.timezone.utc
        ).isoformat()
        merge_dict_aql_parts = []
        bind_vars_for_aql: Dict[str, Any] = {
            "action_id_to_find": action_id,
            "@collection_name": collection_name,
        }
        for key, value in update_object_for_merge.items():
            bind_key_name = f"val_{re.sub(r'[^a-zA-Z0-9_]', '_', key)}"
            merge_dict_aql_parts.append(f"'{key}': @{bind_key_name}")
            bind_vars_for_aql[bind_key_name] = value
        if not merge_dict_aql_parts:
            self.logger.warning(f"没有有效的更新内容应用到 action_id {action_id}。")
            return False
        merge_object_aql_string = f"{{{', '.join(merge_dict_aql_parts)}}}"
        aql_query = f"""
            FOR doc IN @@collection_name
                FILTER doc.action_attempted.action_id == @action_id_to_find
                LIMIT 1
                LET current_action_attempted = IS_OBJECT(doc.action_attempted) ? doc.action_attempted : {{}}
                UPDATE doc WITH {{ action_attempted: MERGE(current_action_attempted, {merge_object_aql_string}) }} IN @@collection_name
                OPTIONS {{ ignoreErrors: false }}
                RETURN NEW._key
        """
        try:
            update_cursor = await asyncio.to_thread(self.db.aql.execute, aql_query, bind_vars=bind_vars_for_aql)
            stats = update_cursor.statistics()
            writes_executed = stats.get("writes_executed", 0) if stats else 0
            if writes_executed > 0:
                self.logger.info(
                    f"成功通过 action_id '{action_id}' 更新 ArangoDB 中动作状态. 更新内容: {updates}. Writes: {writes_executed}"
                )
                return True
            else:
                self.logger.warning(
                    f"通过 action_id '{action_id}' 更新 ArangoDB 动作状态时，没有文档被修改 (writes_executed: 0). "
                    f"可能未找到匹配的 action_id 或 action_attempted 结构不正确. 意图更新: {updates}."
                )
                return False
        except Exception as e:
            self.logger.error(f"错误: 通过 action_id '{action_id}' 更新 ArangoDB 动作状态失败: {e}", exc_info=True)
            return False

    async def save_intrusive_thoughts_batch(self, thoughts_to_insert: List[Dict[str, Any]]) -> None:
        if not thoughts_to_insert:
            self.logger.debug("(BackgroundIntrusive) 没有新的侵入性思维需要保存。")
            return
        try:
            intrusive_thoughts_collection = await self.ensure_collection_exists(
                self.INTRUSIVE_THOUGHTS_POOL_COLLECTION_NAME
            )
            await asyncio.to_thread(intrusive_thoughts_collection.insert_many, thoughts_to_insert)
            self.logger.info(
                f"(BackgroundIntrusive) 已向 ArangoDB 池 '{intrusive_thoughts_collection.name}' 中存入 {len(thoughts_to_insert)} 条新的侵入性思维。"
            )
        except DocumentInsertError as e_insert:
            self.logger.error(
                f"(BackgroundIntrusive) 存入侵入性思维到 ArangoDB 集合 '{self.INTRUSIVE_THOUGHTS_POOL_COLLECTION_NAME}' 失败 (DocumentInsertError): {e_insert}",
                exc_info=True,
            )
        except (ArangoServerError, ArangoClientError) as e_db:
            self.logger.error(
                f"(BackgroundIntrusive) 存入侵入性思维到 ArangoDB 集合 '{self.INTRUSIVE_THOUGHTS_POOL_COLLECTION_NAME}' 时发生数据库错误: {e_db}",
                exc_info=True,
            )
        except Exception as e:
            self.logger.error(
                f"(BackgroundIntrusive) 存入侵入性思维到 ArangoDB 集合 '{self.INTRUSIVE_THOUGHTS_POOL_COLLECTION_NAME}' 失败 (未知错误): {e}",
                exc_info=True,
            )

    async def get_random_intrusive_thought(self) -> Dict[str, Any] | None:
        collection_name = self.INTRUSIVE_THOUGHTS_POOL_COLLECTION_NAME
        self.logger.debug(
            f"(CoreLogic) 尝试从 ArangoDB 集合 '{collection_name}' (数据库: '{self.db.name}') 随机抽取侵入性思维..."
        )
        try:
            await self.ensure_collection_exists(collection_name)
            aql_query_sample = """
                FOR doc IN @@collection_name
                    FILTER doc.used == false
                    SORT RAND()
                    LIMIT 1
                    RETURN doc
            """
            bind_vars_sample = {"@collection_name": collection_name}
            cursor_sample = await asyncio.to_thread(self.db.aql.execute, aql_query_sample, bind_vars=bind_vars_sample)
            random_thought_doc = next(cursor_sample, None) # type: ignore
            if random_thought_doc:
                self.logger.debug(f"(CoreLogic) 成功抽取到侵入性思维: {str(random_thought_doc)[:100]}...")
            else:
                self.logger.debug(f"(CoreLogic) 未能从集合 '{collection_name}' 中抽取到未使用的侵入性思维。")
            return random_thought_doc
        except (AQLQueryExecuteError, ArangoServerError, ArangoClientError) as e_sample_db:
            self.logger.warning(
                f"从 ArangoDB 侵入性思维池 '{collection_name}' 取样时发生数据库错误: {e_sample_db}", exc_info=True
            )
            return None
        except Exception as e_sample_intrusive:
            self.logger.warning(
                f"从 ArangoDB 侵入性思维池 '{collection_name}' 取样时出错: {e_sample_intrusive}", exc_info=True
            )
            return None

    # 🐾 小猫爪：这是为子思维活动日志新增的保存方法！
    async def save_sub_mind_activity(self, activity_data: Dict[str, Any]) -> str | None:
        """
        保存一条子思维的活动记录到 SubMindActivityLog 集合。
        activity_data 应该包含 conversation_id, timestamp (ISO格式字符串或数值型毫秒),
        以及其他如 mood, reasoning, reply_text, main_thought_context, llm_input_prompt, llm_output_json 等。
        """
        if not activity_data:
            self.logger.warning("save_sub_mind_activity 收到空的 activity_data，不执行保存。")
            return None

        # 确保关键字段存在
        if not activity_data.get("conversation_id"):
            self.logger.error("保存子思维活动日志失败：缺少 conversation_id。")
            return None
        if not activity_data.get("timestamp"): # 假设时间戳由调用方提供，且格式正确
            self.logger.warning("保存子思维活动日志：未提供明确的 timestamp，将使用当前时间。")
            activity_data["timestamp"] = datetime.datetime.now(datetime.timezone.utc).isoformat() + "Z"
        elif isinstance(activity_data["timestamp"], (int, float)): # 如果是数值型，转换为ISO字符串
             activity_data["timestamp"] = datetime.datetime.fromtimestamp(
                activity_data["timestamp"] / 1000.0, tz=datetime.timezone.utc
            ).isoformat() + "Z"


        collection_name = self.SUB_MIND_ACTIVITY_LOG_COLLECTION_NAME
        try:
            log_collection = await self.ensure_collection_exists(collection_name)
            # 自动生成 _key
            activity_data.pop("_key", None) # 移除可能存在的_key，让DB生成

            insert_result = await asyncio.to_thread(log_collection.insert, activity_data)
            doc_key = insert_result.get("_key")
            if doc_key:
                self.logger.info(
                    f"子思维活动日志 (文档 Key: {doc_key}, 会话: {activity_data.get('conversation_id')}) "
                    f"已成功保存到集合 '{collection_name}'。"
                )
                return str(doc_key)
            else:
                self.logger.error(f"错误：保存子思维活动日志到 ArangoDB 后未能获取文档 _key。Insert result: {insert_result}")
                return None
        except DocumentInsertError as e_insert:
            self.logger.error(
                f"错误：保存子思维活动日志到 ArangoDB 集合 '{collection_name}' 失败 (DocumentInsertError): {e_insert}",
                exc_info=True,
            )
            return None
        except (ArangoServerError, ArangoClientError) as e_db:
            self.logger.error(
                f"错误：保存子思维活动日志到 ArangoDB 集合 '{collection_name}' 时发生数据库错误: {e_db}",
                exc_info=True,
            )
            return None
        except Exception as e:
            self.logger.error(
                f"错误：保存子思维活动日志到 ArangoDB 集合 '{collection_name}' 时发生未知错误: {e}",
                exc_info=True,
            )
            return None

    # 🐾 小猫爪：新增一个获取子思维上次发言和想法的方法（如果选择从SubMindActivityLog获取）
    async def get_sub_mind_last_activity(self, conversation_id: str) -> Dict[str, Any] | None:
        """
        从 SubMindActivityLog 获取指定会话的最新一条子思维活动记录。
        这条记录应包含其上次的发言(reply_text)和当时的想法(reasoning)。
        """
        if not conversation_id:
            self.logger.warning("获取子思维上次活动失败：未提供 conversation_id。")
            return None

        collection_name = self.SUB_MIND_ACTIVITY_LOG_COLLECTION_NAME
        self.logger.debug(
            f"正在从集合 '{collection_name}' 获取会话 '{conversation_id}' 的最新子思维活动记录..."
        )
        try:
            await self.ensure_collection_exists(collection_name) # 确保集合存在
            aql_query = """
                FOR doc IN @@collection_name
                    FILTER doc.conversation_id == @conversation_id
                    SORT doc.timestamp DESC  // 假设 timestamp 是 ISO 字符串或可比较的数值
                    LIMIT 1
                    RETURN {
                        last_reply_text: doc.reply_text, // 假设字段名为 reply_text
                        last_reasoning: doc.reasoning,   // 假设字段名为 reasoning
                        timestamp: doc.timestamp
                    }
            """
            bind_vars = {
                "@collection_name": collection_name,
                "conversation_id": conversation_id,
            }
            cursor = await asyncio.to_thread(self.db.aql.execute, aql_query, bind_vars=bind_vars, ttl=30)
            last_activity = next(cursor, None) # type: ignore
            if last_activity:
                self.logger.debug(f"为会话 '{conversation_id}' 获取到最新的子思维活动记录。")
            else:
                self.logger.debug(f"未找到会话 '{conversation_id}' 的子思维活动记录。")
            return last_activity
        except (AQLQueryExecuteError, ArangoServerError, ArangoClientError) as e_db:
            self.logger.error(
                f"获取会话 '{conversation_id}' 的最新子思维活动记录时发生数据库错误: {e_db}",
                exc_info=True,
            )
            return None
        except Exception as e:
            self.logger.error(
                f"获取会话 '{conversation_id}' 的最新子思维活动记录时发生未知错误: {e}",
                exc_info=True,
            )
            return None


    async def close(self) -> None:
        try:
            self.logger.info("ArangoDBHandler 正在请求关闭 (通常不需要显式关闭客户端)。")
        except Exception as e:
            self.logger.error(f"关闭 ArangoDB client 时发生错误: {e}", exc_info=True)

    async def execute_query(self, query: str, bind_vars: Dict[str, Any] | None = None) -> List[Dict[str, Any]]:
        self.logger.debug(f"Executing generic AQL query: {query[:100]}... with bind_vars: {bind_vars}")
        try:
            cursor = await asyncio.to_thread(self.db.aql.execute, query, bind_vars=bind_vars, ttl=60)
            results = list(cursor) # type: ignore
            self.logger.debug(f"Generic AQL query returned {len(results)} results.")
            return results
        except (AQLQueryExecuteError, ArangoServerError, ArangoClientError) as e_db:
            self.logger.error(f"执行通用AQL查询时发生数据库错误: {e_db}", exc_info=True)
            return []
        except Exception as e:
            self.logger.error(f"执行通用AQL查询时发生未知错误: {e}", exc_info=True)
            return []
