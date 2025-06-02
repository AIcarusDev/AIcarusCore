# src/database/arangodb_handler.py
import asyncio
import datetime
import os
import re
import uuid
from typing import Any

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

from src.common.custom_logging.logger_manager import get_logger  # 假设路径正确

module_logger = get_logger("AIcarusCore.database")


class ArangoDBHandler:
    RAW_CHAT_MESSAGES_COLLECTION_NAME = "RawChatMessages"
    THOUGHTS_COLLECTION_NAME = "thoughts_collection"
    INTRUSIVE_THOUGHTS_POOL_COLLECTION_NAME = "intrusive_thoughts_pool"

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
            self.logger.info(f"集合 '{collection_name}' 在数据库 '{self.db.name}' 中不存在，正在创建...")
            collection = await asyncio.to_thread(self.db.create_collection, collection_name)
            if collection_name == self.RAW_CHAT_MESSAGES_COLLECTION_NAME:
                self.logger.info(
                    f"为集合 '{self.RAW_CHAT_MESSAGES_COLLECTION_NAME}' 创建复合索引 ['conversation_id', 'timestamp']..."
                )
                # 索引字段 'timestamp' 现在将是数值型
                await asyncio.to_thread(
                    collection.add_persistent_index,  # type: ignore
                    fields=["conversation_id", "timestamp"],
                    unique=False,
                    sparse=False,
                    in_background=True,
                )
                self.logger.info(
                    f"为集合 '{self.RAW_CHAT_MESSAGES_COLLECTION_NAME}' 创建时间戳索引 ['timestamp'] (数值型)..."
                )
                await asyncio.to_thread(
                    collection.add_persistent_index,  # type: ignore
                    fields=["timestamp"],
                    unique=False,
                    sparse=False,
                    in_background=True,  # 'timestamp' is now numeric
                )
            self.logger.info(f"集合 '{collection_name}' 创建成功。")
            return collection  # type: ignore
        self.logger.debug(f"集合 '{collection_name}' 已在数据库 '{self.db.name}' 中存在。")
        return await asyncio.to_thread(self.db.collection, collection_name)

    async def save_raw_chat_message(self, message_data: dict[str, Any]) -> str | None:
        # 确保 conversation_id 存在
        if not message_data.get("conversation_id"):
            self.logger.error("保存聊天消息失败：缺少 conversation_id。")
            return None

        # --- 时间戳处理开始 ---
        ts_value = message_data.get("timestamp")
        final_numeric_timestamp: int | None = None

        if isinstance(ts_value, int | float):  # 如果已经是数字
            final_numeric_timestamp = int(ts_value)
        elif isinstance(ts_value, str):  # 如果是字符串，尝试转换
            self.logger.warning(
                f"消息 {message_data.get('platform_message_id', 'N/A')} 的顶层 timestamp 是字符串 '{ts_value}'，将尝试转换为数值型UTC毫秒。"
            )
            try:
                # 假设字符串是 ISO 8601 UTC 格式 (如 '...Z' 或 '...+00:00')
                # 替换 'Z' 以便 fromisoformat 正确处理
                dt_obj = datetime.datetime.fromisoformat(ts_value.replace("Z", "+00:00"))

                # 确保是 UTC 时间
                if dt_obj.tzinfo is None or dt_obj.tzinfo.utcoffset(dt_obj) is None:  # 如果是 naive ISO string
                    # 假设 naive ISO string 本身就是 UTC 时间
                    aware_dt = dt_obj.replace(tzinfo=datetime.UTC)
                    self.logger.debug(f"Naive ISO string '{ts_value}' 被假定为 UTC。")
                else:  # 如果有时区信息，则转换为 UTC
                    aware_dt = dt_obj.astimezone(datetime.UTC)

                final_numeric_timestamp = int(aware_dt.timestamp() * 1000)
                self.logger.info(f"成功将字符串时间戳 '{ts_value}' 转换为数值毫秒: {final_numeric_timestamp}")
            except ValueError as e:
                self.logger.error(
                    f"无法将字符串时间戳 '{ts_value}' 转换为datetime对象: {e}。将尝试使用 raw_message_info_dump.time。"
                )

        # 如果 ts_value 不是数字或可转换的字符串，或者转换失败，尝试从 raw_message_info_dump 获取
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

        # 如果最终还是没有有效的时间戳，则使用当前时间作为回退
        if final_numeric_timestamp is None:
            current_time_ms = int(datetime.datetime.now(datetime.UTC).timestamp() * 1000)
            self.logger.warning(
                f"消息 {message_data.get('platform_message_id', 'N/A')} 无法确定有效时间戳，"
                f"将使用当前UTC时间 ({current_time_ms} ms) 作为回退。"
            )
            final_numeric_timestamp = current_time_ms

        message_data["timestamp"] = final_numeric_timestamp  # 更新/设置顶层 timestamp 为数值型
        # --- 时间戳处理结束 ---

        # 再次检查 conversation_id 和 timestamp （现在是数值）都存在
        if not message_data.get("conversation_id") or message_data.get("timestamp") is None:
            self.logger.error("保存聊天消息失败：关键字段 conversation_id 或转换后的 timestamp 丢失。")
            return None

        try:
            chat_messages_collection = await self.ensure_collection_exists(self.RAW_CHAT_MESSAGES_COLLECTION_NAME)

            # _key 生成逻辑
            key_to_use: str
            if "_key" in message_data and message_data["_key"]:
                key_to_use = str(message_data["_key"])
            elif "platform_message_id" in message_data and message_data["platform_message_id"]:
                candidate_key = str(message_data["platform_message_id"])
                # 清理 platform_message_id 使其符合 _key 规范
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
            if e_insert.http_code == 409:  # HTTP 409 Conflict - 文档已存在
                self.logger.warning(
                    f"聊天消息 (Key: {message_data.get('_key')}) 已存在于 '{self.RAW_CHAT_MESSAGES_COLLECTION_NAME}'。"
                    f"错误: {e_insert.http_code} - {e_insert.error_message}"
                )
                return message_data.get("_key")  # 返回已存在的 key
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

    async def get_recent_chat_messages(self, conversation_id: str, limit: int = 30) -> list[dict[str, Any]]:
        # 此方法假设 'timestamp' 已经是数值型 (ms)
        if not conversation_id:
            self.logger.warning("获取最近聊天记录失败：未提供 conversation_id。")
            return []
        self.logger.debug(
            f"正在从集合 '{self.RAW_CHAT_MESSAGES_COLLECTION_NAME}' 获取会话 '{conversation_id}' 的最近 {limit} 条消息 (基于数值时间戳)..."
        )
        try:
            # AQL 查询现在基于数值型 timestamp 排序
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
            messages = list(cursor)  # type: ignore
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
    ) -> list[dict[str, Any]]:
        now_utc = datetime.datetime.now(datetime.UTC)
        cutoff_datetime_obj = now_utc - datetime.timedelta(minutes=duration_minutes)
        # --- 修改开始 ---
        # 我们不再需要ISO字符串给AQL了
        # cutoff_time_iso_for_aql_conversion = cutoff_datetime_obj.isoformat()

        # 直接在Python里计算出截止时间的UTC毫秒数
        calculated_cutoff_ms_in_python = int(cutoff_datetime_obj.timestamp() * 1000)
        # --- 修改结束 ---

        self.logger.debug(
            # --- 修改日志打印 ---
            f"获取最近 {duration_minutes} 分钟的聊天记录上下文。截止时间 (Python计算的UTC毫秒): {calculated_cutoff_ms_in_python} (对应ISO: {cutoff_datetime_obj.isoformat()})."
            f"{' 特定会话: ' + conversation_id if conversation_id else ' 所有相关会话。'}"
        )

        let_statements = """
            LET rawDump = doc.raw_message_info_dump
            LET userInfo = IS_OBJECT(rawDump.user_info) ? rawDump.user_info : {}
            LET groupInfo = IS_OBJECT(rawDump.group_info) ? rawDump.group_info : {}
        """

        # 返回的 doc.timestamp 现在将是数值型 (ms)
        return_statement = """
            RETURN {
                _key: doc._key,
                platform: rawDump.platform,
                group_id: groupInfo.group_id,
                group_name: groupInfo.group_name,
                message_id: doc.platform_message_id,
                timestamp: doc.timestamp, // <<< 这将是数值型 (ms) 时间戳
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
        bind_vars: dict[str, Any] = {
            "@messages_collection": self.RAW_CHAT_MESSAGES_COLLECTION_NAME,
            "cutoff_timestamp_ms_param": calculated_cutoff_ms_in_python,
        }

        # AQL LET 语句，用于将 @cutoff_time_iso_str (ISO string) 转换为毫秒级 Unix 时间戳
        # let_cutoff_conversion_aql = "LET cutoff_timestamp_ms = DATE_TIMESTAMP(@cutoff_time_iso_str) * 1000"
        # 过滤条件现在基于数值型的 doc.timestamp
        filter_condition_time_aql = "doc.timestamp >= @cutoff_timestamp_ms_param"
        # 排序字段现在是数值型的 doc.timestamp
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
            messages_from_db = list(cursor)  # type: ignore
            messages_from_db_direct = list(cursor)  # type: ignore
            self.logger.info(f"AQL查询直接从cursor转换后的原始结果数量: {len(messages_from_db_direct)}")
            # --- ↑↑↑ 加在这里 ↑↑↑ ---

            processed_messages = []
            for msg_data in messages_from_db:
                content = msg_data.get("message_content")
                if not isinstance(content, list):  # 确保 message_content 是列表格式
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

    async def get_latest_thought_document_raw(self) -> dict[str, Any] | None:
        # ... (此方法保持不变, 假设 thoughts_collection 中的 timestamp 格式不受影响或单独处理)
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
            """  # 假设 thoughts_collection 的 timestamp 也是可比较的 (例如 ISO string or number)
            bind_vars = {"@collection_name": collection_name}
            cursor = await asyncio.to_thread(self.db.aql.execute, aql_query, bind_vars=bind_vars, ttl=30)
            latest_document = next(cursor, None)  # type: ignore
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

    async def save_thought_document(self, document_to_save: dict[str, Any]) -> str | None:
        # ... (此方法保持不变, 假设 thoughts_collection 中的 timestamp 格式不受影响或单独处理)
        if not document_to_save:
            self.logger.warning("save_thought_document 收到空的 document_to_save，不执行保存。")
            return None
        try:
            # 确保 thoughts_collection 的 timestamp 也是一致的，如果它也需要是数值型，这里也要处理
            # 例如: if isinstance(document_to_save.get("timestamp"), str): convert it
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
        # ... (此方法保持不变)
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
        """  # @timestamp in AQL should be an ISO string for updated_at
        bind_vars = {
            "@collection_name": collection_name,
            "action_id": action_id,
            "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
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

    async def update_action_status_in_document(
        self,
        doc_key: str,
        action_id_for_log: str,
        updates: dict[str, Any],
        expected_conditions: dict[str, Any] | None = None,
    ) -> bool:
        # ... (此方法保持不变, updated_at 仍使用 ISO string)
        collection_name = self.THOUGHTS_COLLECTION_NAME
        if not doc_key:
            self.logger.error(
                f"错误: update_action_status_in_document 收到空的 doc_key (action_id: {action_id_for_log})。无法更新。"
            )
            return False
        update_object_for_merge = updates.copy()
        update_object_for_merge["updated_at"] = datetime.datetime.now(
            datetime.UTC
        ).isoformat()  # ISO string for updated_at
        merge_dict_aql_parts = []
        bind_vars_for_aql: dict[str, Any] = {
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
            old_doc_list = list(update_cursor)  # type: ignore
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
                else:  # 包括文档未找到或内容无变化的情况
                    self.logger.debug(
                        f"调试(内容无变化或文档未找到): 更新 ArangoDB 中动作状态时，文档 _key '{doc_key}' (action_id: {action_id_for_log}) 数据未发生实际变化或文档可能未找到. "
                        f"意图更新: {updates}. 期望条件: {expected_conditions}. 当前DB值 (action_attempted): {current_db_val_str}..."
                    )
                return False
        except AQLQueryExecuteError as e_aql:
            if e_aql.http_code == 404 and e_aql.error_code == 1202:  # Document not found
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

    async def update_action_status_by_action_id(self, action_id: str, updates: dict[str, Any]) -> bool:
        # ... (此方法保持不变, updated_at 仍使用 ISO string)
        collection_name = self.THOUGHTS_COLLECTION_NAME
        if not action_id:
            self.logger.error("update_action_status_by_action_id: action_id 为空，无法更新。")
            return False
        update_object_for_merge = updates.copy()
        update_object_for_merge["updated_at"] = datetime.datetime.now(
            datetime.UTC
        ).isoformat()  # ISO string for updated_at
        merge_dict_aql_parts = []
        bind_vars_for_aql: dict[str, Any] = {
            "action_id_to_find": action_id,
            "@collection_name": collection_name,
        }
        for key, value in update_object_for_merge.items():
            bind_key_name = f"val_{re.sub(r'[^a-zA-Z0-9_]', '_', key)}"
            merge_dict_aql_parts.append(f"'{key}': @{bind_key_name}")
            bind_vars_for_aql[bind_key_name] = value
        if not merge_dict_aql_parts:  # 如果没有有效的更新内容
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

    async def save_intrusive_thoughts_batch(self, thoughts_to_insert: list[dict[str, Any]]) -> None:
        # ... (此方法保持不变, 假设 intrusive_thoughts_pool 中的 timestamp 格式不受影响或单独处理)
        if not thoughts_to_insert:
            self.logger.debug("(BackgroundIntrusive) 没有新的侵入性思维需要保存。")
            return
        try:
            intrusive_thoughts_collection = await self.ensure_collection_exists(
                self.INTRUSIVE_THOUGHTS_POOL_COLLECTION_NAME
            )
            # 确保 intrusive_thoughts_pool 的 timestamp 也是一致的
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

    async def get_random_intrusive_thought(self) -> dict[str, Any] | None:
        # ... (此方法保持不变)
        collection_name = self.INTRUSIVE_THOUGHTS_POOL_COLLECTION_NAME
        self.logger.debug(
            f"(CoreLogic) 尝试从 ArangoDB 集合 '{collection_name}' (数据库: '{self.db.name}') 随机抽取侵入性思维..."
        )
        try:
            await self.ensure_collection_exists(collection_name)  # 确保集合存在
            aql_query_sample = """
                FOR doc IN @@collection_name
                    FILTER doc.used == false
                    SORT RAND()
                    LIMIT 1
                    RETURN doc
            """
            bind_vars_sample = {"@collection_name": collection_name}
            cursor_sample = await asyncio.to_thread(self.db.aql.execute, aql_query_sample, bind_vars=bind_vars_sample)
            random_thought_doc = next(cursor_sample, None)  # type: ignore
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

    async def close(self) -> None:
        try:
            self.logger.info("ArangoDBHandler 正在请求关闭 (通常不需要显式关闭客户端)。")
            # ArangoClient 通常不需要显式关闭，除非有特定资源需要释放
        except Exception as e:
            self.logger.error(f"关闭 ArangoDB client 时发生错误: {e}", exc_info=True)

    async def execute_query(self, query: str, bind_vars: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        self.logger.debug(f"Executing generic AQL query: {query[:100]}... with bind_vars: {bind_vars}")
        try:
            cursor = await asyncio.to_thread(self.db.aql.execute, query, bind_vars=bind_vars, ttl=60)
            results = list(cursor)  # type: ignore
            self.logger.debug(f"Generic AQL query returned {len(results)} results.")
            return results
        except (AQLQueryExecuteError, ArangoServerError, ArangoClientError) as e_db:
            self.logger.error(f"执行通用AQL查询时发生数据库错误: {e_db}", exc_info=True)
            return []
        except Exception as e:
            self.logger.error(f"执行通用AQL查询时发生未知错误: {e}", exc_info=True)
            return []
