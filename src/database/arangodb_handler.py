# src/database/arangodb_handler.py
import asyncio
import datetime
import os
import re
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
    async def create_from_config(cls, database_config) -> "ArangoDBHandler":
        """从配置对象创建ArangoDBHandler实例"""
        # 尝试从配置对象获取属性，支持不同的属性名
        host = getattr(database_config, 'host', None) or getattr(database_config, 'url', None) or getattr(database_config, 'arangodb_host', None)
        username = getattr(database_config, 'username', None) or getattr(database_config, 'user', None) or getattr(database_config, 'arangodb_user', None)
        password = getattr(database_config, 'password', None) or getattr(database_config, 'arangodb_password', None)
        database_name = getattr(database_config, 'name', None) or getattr(database_config, 'database_name', None) or getattr(database_config, 'arangodb_database', None)
        
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
            if not host: missing_vars.append("host/url")
            if not username: missing_vars.append("username/user")
            if not password: missing_vars.append("password")
            if not database_name: missing_vars.append("database_name/name")
            
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

    async def test_connection(self) -> bool:
        """测试数据库连接"""
        try:
            # 尝试获取数据库信息
            info = await asyncio.to_thread(self.db.properties)
            self.logger.info(f"数据库连接成功: {info.get('name', 'unknown')}")
            return True
        except Exception as e:
            self.logger.error(f"数据库连接失败: {e}")
            raise

    async def save_raw_chat_message(self, message_data: dict) -> bool:
        """保存原始聊天消息到数据库"""
        try:
            # 添加时间戳
            message_data["_timestamp"] = time.time() * 1000.0

            # 插入到原始消息集合
            collection = self.db.collection(self.RAW_CHAT_MESSAGES_COLLECTION_NAME)
            result = await asyncio.to_thread(collection.insert, message_data)

            if result.get("_id"):
                self.logger.debug(f"成功保存消息: {result['_id']}")
                return True
            else:
                self.logger.warning(f"保存消息时未返回ID: {result}")
                return False

        except Exception as e:
            self.logger.error(f"保存聊天消息时发生错误: {e}", exc_info=True)
            return False

    async def get_recent_chat_messages_for_context(self, duration_minutes: int = 10) -> list[dict]:
        """获取最近指定时间内的聊天消息用于上下文"""
        try:
            # 确保集合存在
            await self.ensure_collection_exists(self.RAW_CHAT_MESSAGES_COLLECTION_NAME)
            
            # 计算时间阈值（毫秒）
            current_time_ms = time.time() * 1000.0
            threshold_time_ms = current_time_ms - (duration_minutes * 60 * 1000)
            
            # AQL查询获取最近消息
            query = f"""
                FOR msg IN {self.RAW_CHAT_MESSAGES_COLLECTION_NAME}
                    FILTER msg.timestamp >= @threshold_time
                    SORT msg.timestamp ASC
                    LIMIT 100
                    RETURN msg
            """
            
            bind_vars = {
                "threshold_time": threshold_time_ms
            }
            
            results = await self.execute_query(query, bind_vars)
            self.logger.debug(f"获取到 {len(results)} 条最近的聊天消息")
            return results
            
        except Exception as e:
            self.logger.error(f"获取最近聊天消息时发生错误: {e}", exc_info=True)
            return []

    async def execute_query(self, query: str, bind_vars: dict = None) -> list:
        """执行AQL查询"""
        try:
            if bind_vars is None:
                bind_vars = {}
            
            cursor = await asyncio.to_thread(
                self.db.aql.execute, query, bind_vars=bind_vars
            )
            results = await asyncio.to_thread(list, cursor)
            return results
            
        except Exception as e:
            self.logger.error(f"执行AQL查询时发生错误: {e}", exc_info=True)
            return []

    async def get_latest_thought_document_raw(self) -> dict | None:
        """获取最新的思维文档"""
        try:
            # 确保集合存在
            await self.ensure_collection_exists(self.THOUGHTS_COLLECTION_NAME)
            
            query = f"""
                FOR doc IN {self.THOUGHTS_COLLECTION_NAME}
                    SORT doc.timestamp DESC
                    LIMIT 1
                    RETURN doc
            """
            
            results = await self.execute_query(query)
            return results[0] if results else None
            
        except Exception as e:
            self.logger.error(f"获取最新思维文档时发生错误: {e}", exc_info=True)
            return None

    async def update_action_status_in_document(
        self, 
        doc_key: str, 
        action_id: str, 
        updates: dict, 
        expected_conditions: dict = None
    ) -> bool:
        """更新文档中的动作状态"""
        try:
            # 确保集合存在
            await self.ensure_collection_exists(self.THOUGHTS_COLLECTION_NAME)
            
            collection = self.db.collection(self.THOUGHTS_COLLECTION_NAME)
            
            # 构建更新语句
            update_data = {}
            for key, value in updates.items():
                update_data[f"action_attempted.{key}"] = value
            
            # 如果有条件检查
            if expected_conditions:
                # 先检查当前状态
                current_doc = await asyncio.to_thread(collection.get, doc_key)
                if not current_doc:
                    self.logger.warning(f"文档 {doc_key} 不存在")
                    return False
                
                action_state = current_doc.get("action_attempted", {})
                for cond_key, cond_value in expected_conditions.items():
                    if action_state.get(cond_key) != cond_value:
                        self.logger.debug(f"条件检查失败: {cond_key} 期望 {cond_value}, 实际 {action_state.get(cond_key)}")
                        return False
            
            # 执行更新
            result = await asyncio.to_thread(
                collection.update, doc_key, update_data
            )
            
            return result.get("_rev") is not None
            
        except Exception as e:
            self.logger.error(f"更新动作状态时发生错误: {e}", exc_info=True)
            return False

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
