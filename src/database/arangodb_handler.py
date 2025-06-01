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

from src.common.custom_logging.logger_manager import get_logger
from src.config.alcarus_configs import DatabaseSettings

logger = get_logger("AIcarusCore.database")

RAW_CHAT_MESSAGES_COLLECTION_NAME = "RawChatMessages"
THOUGHTS_COLLECTION_NAME = "thoughts_collection"
INTRUSIVE_THOUGHTS_POOL_COLLECTION_NAME = "intrusive_thoughts_pool"


async def connect_to_arangodb(db_config: DatabaseSettings | None = None) -> tuple[ArangoClient, StandardDatabase]:
    """
    异步连接到ArangoDB并返回客户端和数据库对象。
    会从环境变量读取数据库连接信息。
    """
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
        logger.critical(message)
        raise ValueError(message)

    try:
        # ArangoClient 的初始化是同步的，因此在异步函数中使用 to_thread
        client: ArangoClient = await asyncio.to_thread(ArangoClient, hosts=host)
        # 连接到 _system 数据库以检查目标数据库是否存在
        sys_db: StandardDatabase = await asyncio.to_thread(client.db, "_system", username=user, password=password)

        # 如果目标数据库不存在，则创建它
        if not await asyncio.to_thread(sys_db.has_database, db_name_from_env):
            logger.info(f"数据库 '{db_name_from_env}' 不存在，正在尝试创建...")
            await asyncio.to_thread(sys_db.create_database, db_name_from_env)
            logger.info(f"数据库 '{db_name_from_env}' 创建成功。")

        # 连接到目标数据库
        db: StandardDatabase = await asyncio.to_thread(client.db, db_name_from_env, username=user, password=password)
        await asyncio.to_thread(db.properties)  # 验证连接和权限
        logger.info(f"成功连接到 ArangoDB！主机: {host}, 数据库: {db_name_from_env}")
        return client, db
    except (ArangoServerError, ArangoClientError) as e:
        message = f"连接 ArangoDB 时发生错误 (Host: {host}, DB: {db_name_from_env}): {e}"
        logger.critical(message, exc_info=True)
        raise RuntimeError(message) from e
    except Exception as e:  # 捕获其他可能的初始化或权限错误
        message = f"连接 ArangoDB 时发生未知或权限错误 (Host: {host}, DB: {db_name_from_env}, User: {user}): {e}"
        logger.critical(message, exc_info=True)
        raise RuntimeError(message) from e


async def ensure_collection_exists(db: StandardDatabase, collection_name: str) -> StandardCollection:
    """
    异步确保指定的集合在数据库中存在，如果不存在则创建它。
    如果集合是 RawChatMessages，则会自动添加必要的索引。
    """
    if not await asyncio.to_thread(db.has_collection, collection_name):
        logger.info(f"集合 '{collection_name}' 在数据库 '{db.name}' 中不存在，正在创建...")
        collection = await asyncio.to_thread(db.create_collection, collection_name)
        # 为 RawChatMessages 集合添加必要的索引
        if collection_name == RAW_CHAT_MESSAGES_COLLECTION_NAME:
            logger.info(
                f"为集合 '{RAW_CHAT_MESSAGES_COLLECTION_NAME}' 创建复合索引 ['conversation_id', 'timestamp']..."
            )
            await asyncio.to_thread(
                collection.add_persistent_index,  # type: ignore
                fields=["conversation_id", "timestamp"],  # 用于高效查询特定会话的最近消息
                unique=False,  # 非唯一索引
                sparse=False,  # 假设这些字段总是存在
                in_background=True,  # 后台创建索引，避免阻塞
            )
            logger.info(f"为集合 '{RAW_CHAT_MESSAGES_COLLECTION_NAME}' 创建时间戳索引 ['timestamp']...")
            await asyncio.to_thread(
                collection.add_persistent_index,  # type: ignore
                fields=["timestamp"],  # 单独的时间戳索引，可能用于其他排序或范围查询
                unique=False,
                sparse=False,
                in_background=True,
            )
        return collection  # type: ignore
    logger.debug(f"集合 '{collection_name}' 已在数据库 '{db.name}' 中存在。")
    return await asyncio.to_thread(db.collection, collection_name)


# --- 原始聊天消息数据操作 ---
async def save_raw_chat_message(db: StandardDatabase, message_data: dict[str, Any]) -> str | None:
    """
    异步将单条聊天消息文档保存到 RawChatMessages 集合。
    返回新文档的 _key。
    `message_data` 应该是一个符合预定义扁平化结构的字典。
    """
    if not message_data.get("conversation_id") or not message_data.get("timestamp"):
        logger.error("保存聊天消息失败：缺少 conversation_id 或 timestamp。")
        return None

    try:
        chat_messages_collection = await ensure_collection_exists(db, RAW_CHAT_MESSAGES_COLLECTION_NAME)

        key_to_use: str
        if "_key" in message_data and message_data["_key"]:
            key_to_use = str(message_data["_key"])
        elif "platform_message_id" in message_data and message_data["platform_message_id"]:
            candidate_key = str(message_data["platform_message_id"])
            candidate_key = re.sub(r"[^a-zA-Z0-9_:.@()+,$\!*\'=-]", "_", candidate_key)  # 替换无效字符
            if not candidate_key or candidate_key.startswith(("_", "-")) or len(candidate_key) > 254:  # 检查长度限制
                logger.debug(
                    f"平台消息ID '{message_data['platform_message_id']}' 处理后 ('{candidate_key}') 不适合作为 _key，将生成UUID。"
                )
                key_to_use = str(uuid.uuid4())
            else:
                key_to_use = candidate_key
        else:
            key_to_use = str(uuid.uuid4())

        message_data["_key"] = key_to_use  # 将确定的键设置回 message_data

        insert_result = await asyncio.to_thread(
            chat_messages_collection.insert, message_data, overwrite=False
        )  # overwrite=False 避免意外覆盖
        doc_key = insert_result.get("_key")

        if doc_key:
            logger.info(
                f"聊天消息 (文档 Key: {doc_key}, 会话: {message_data.get('conversation_id')}) 已成功保存到集合 '{RAW_CHAT_MESSAGES_COLLECTION_NAME}'。"
            )
            return str(doc_key)
        else:
            logger.error(f"错误：保存聊天消息到 ArangoDB 后未能获取文档 _key。Insert result: {insert_result}")
            return None
    except DocumentInsertError as e_insert:
        if e_insert.http_code == 409:
            logger.warning(
                f"聊天消息 (Key: {message_data.get('_key')}) 已存在于 '{RAW_CHAT_MESSAGES_COLLECTION_NAME}'。错误: {e_insert.http_code} - {e_insert.error_message}"
            )
            return message_data.get("_key")  # 如果是因为 _key 冲突，返回已存在的 key (假设这是期望行为)
        logger.error(
            f"错误：保存聊天消息到 ArangoDB 集合 '{RAW_CHAT_MESSAGES_COLLECTION_NAME}' 失败 (DocumentInsertError): {e_insert}",
            exc_info=True,
        )
        return None
    except (ArangoServerError, ArangoClientError) as e_db:
        logger.error(
            f"错误：保存聊天消息到 ArangoDB 集合 '{RAW_CHAT_MESSAGES_COLLECTION_NAME}' 时发生数据库错误: {e_db}",
            exc_info=True,
        )
        return None
    except Exception as e:  # 捕获其他所有可能的异常
        logger.error(
            f"错误：保存聊天消息到 ArangoDB 集合 '{RAW_CHAT_MESSAGES_COLLECTION_NAME}' 时发生未知错误: {e}",
            exc_info=True,
        )
        return None


async def get_recent_chat_messages(db: StandardDatabase, conversation_id: str, limit: int = 30) -> list[dict[str, Any]]:
    """
    异步从 RawChatMessages 集合获取指定会话的最近N条聊天记录。
    结果按时间正序排列 (即，最旧的消息在列表前面，最新的在后面)。
    """
    if not conversation_id:
        logger.warning("获取最近聊天记录失败：未提供 conversation_id。")
        return []

    logger.debug(
        f"正在从集合 '{RAW_CHAT_MESSAGES_COLLECTION_NAME}' 获取会话 '{conversation_id}' 的最近 {limit} 条消息..."
    )
    try:
        aql_query = """
            LET latest_messages_subquery = (
                FOR doc IN @@collection_name
                    FILTER doc.conversation_id == @conversation_id
                    SORT doc.timestamp DESC  // 按时间戳降序，获取最新的
                    LIMIT @limit             // 限制数量
                    RETURN doc
            )
            FOR message IN latest_messages_subquery
                SORT message.timestamp ASC   // 对获取到的N条消息按时间戳升序排列
                RETURN message
        """
        bind_vars = {
            "@collection_name": RAW_CHAT_MESSAGES_COLLECTION_NAME,
            "conversation_id": conversation_id,
            "limit": limit,
        }
        cursor = await asyncio.to_thread(db.aql.execute, aql_query, bind_vars=bind_vars, ttl=30)  # ttl 设置查询超时
        messages = list(cursor)  # type: ignore # 直接转换游标为列表
        logger.debug(f"为会话 '{conversation_id}' 获取到 {len(messages)} 条最近消息。")
        return messages
    except (AQLQueryExecuteError, ArangoServerError, ArangoClientError) as e_db:
        logger.error(
            f"获取会话 '{conversation_id}' 的最近聊天记录时发生数据库错误: {e_db}",
            exc_info=True,
        )
        return []
    except Exception as e:  # 捕获其他所有可能的异常
        logger.error(
            f"获取会话 '{conversation_id}' 的最近聊天记录时发生未知错误: {e}",
            exc_info=True,
        )
        return []


# --- 主思考数据操作 ---
async def get_latest_thought_document_raw(
    db: StandardDatabase, collection_name: str = THOUGHTS_COLLECTION_NAME
) -> dict[str, Any] | None:
    """异步从数据库获取最新的思考文档原文。"""
    logger.debug(
        f"在 get_latest_thought_document_raw 中：准备从 ArangoDB 集合 '{collection_name}' (数据库: '{db.name}') 获取最新思考文档..."
    )
    try:
        aql_query = """
            FOR doc IN @@collection_name
                SORT doc.timestamp DESC
                LIMIT 1
                RETURN doc
        """
        bind_vars = {"@collection_name": collection_name}
        cursor = await asyncio.to_thread(db.aql.execute, aql_query, bind_vars=bind_vars, ttl=30)
        latest_document = next(cursor, None)  # next() 是同步的
        logger.debug(
            f"在 get_latest_thought_document_raw 中：AQL 查询完成。是否找到思考文档: {latest_document is not None}"
        )
        return latest_document
    except (AQLQueryExecuteError, ArangoServerError, ArangoClientError) as e_db:
        logger.error(
            f"在 get_latest_thought_document_raw 中执行 ArangoDB 操作时发生错误 (集合: {collection_name}): {e_db}",
            exc_info=True,
        )
        return None
    except Exception as e:
        logger.error(
            f"在 get_latest_thought_document_raw 中获取最新思考文档时发生未知错误 (集合: {collection_name}): {e}",
            exc_info=True,
        )
        return None


async def save_thought_document(
    thoughts_collection: StandardCollection, document_to_save: dict[str, Any]
) -> str | None:
    """异步将一轮完整的思考结果保存到ArangoDB，并返回新文档的_key。"""
    if not document_to_save:
        logger.warning("save_thought_document 收到空的 document_to_save，不执行保存。")
        return None
    try:
        insert_result = await asyncio.to_thread(thoughts_collection.insert, document_to_save, overwrite=False)
        doc_key = insert_result.get("_key")
        if doc_key:
            logger.info(f"思考结果 (文档 Key: {doc_key}) 已成功保存到 ArangoDB 集合 '{thoughts_collection.name}'。")
            return str(doc_key)
        else:
            logger.error(f"错误：保存思考结果到 ArangoDB 后未能获取文档 _key。Insert result: {insert_result}")
            return None
    except DocumentInsertError as e_insert:
        logger.error(
            f"错误：保存思考结果到 ArangoDB 集合 '{thoughts_collection.name}' 失败 (DocumentInsertError): {e_insert}",
            exc_info=True,
        )
        return None
    except (ArangoServerError, ArangoClientError) as e_db:
        logger.error(
            f"错误：保存思考结果到 ArangoDB 集合 '{thoughts_collection.name}' 时发生数据库错误: {e_db}", exc_info=True
        )
        return None
    except Exception as e:
        logger.error(
            f"错误：保存思考结果到 ArangoDB 集合 '{thoughts_collection.name}' 时发生未知错误: {e}", exc_info=True
        )
        return None


async def mark_action_result_as_seen(
    db: StandardDatabase, collection_name: str = THOUGHTS_COLLECTION_NAME, action_id: str = ""
) -> None:
    """通过action_id，在ArangoDB中异步将对应动作的 result_seen_by_shuang 标记为 True。"""
    if not action_id:
        logger.debug("mark_action_result_as_seen 收到空的 action_id，不执行操作。")
        return

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
        "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
    }

    try:
        update_cursor = await asyncio.to_thread(db.aql.execute, aql_query, bind_vars=bind_vars)
        stats = update_cursor.statistics()
        if stats and stats.get("writes_executed", 0) > 0:
            logger.info(f"动作结果 (ID: {action_id[:8]}) 已在 ArangoDB 集合 '{collection_name}' 中成功标记为已阅。")
    except (AQLQueryExecuteError, ArangoServerError, ArangoClientError) as e_db:
        logger.error(
            f"错误: 在 ArangoDB 集合 '{collection_name}' 中标记动作 (ID: {action_id[:8]}) 结果为已阅时发生数据库错误: {e_db}",
            exc_info=True,
        )
    except Exception as e:
        logger.error(
            f"错误: 在 ArangoDB 集合 '{collection_name}' 中标记动作 (ID: {action_id[:8]}) 结果为已阅时发生未知错误: {e}",
            exc_info=True,
        )


async def update_action_status_in_document(
    db: StandardDatabase,
    collection_name: str,
    doc_key: str,
    action_id_for_log: str,
    updates: dict[str, Any],
    expected_conditions: dict[str, Any] | None = None,
) -> bool:
    if not doc_key:
        logger.error(
            f"错误: update_action_status_in_document 收到空的 doc_key (action_id: {action_id_for_log})。无法更新。"
        )
        return False

    update_object_for_merge = updates.copy()
    update_object_for_merge["updated_at"] = datetime.datetime.now(datetime.UTC).isoformat()

    merge_dict_aql_parts = []
    bind_vars_for_aql: dict[str, Any] = {
        "doc_key_to_update": doc_key,
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
        FOR doc IN {collection_name}
            FILTER {filter_aql_string}
            LIMIT 1
            LET current_action_attempted = IS_OBJECT(doc.action_attempted) ? doc.action_attempted : {{}}
            LET merged_action_attempted = MERGE(current_action_attempted, {merge_object_aql_string})
            UPDATE doc WITH {{
                action_attempted: merged_action_attempted
            }} IN {collection_name}
            OPTIONS {{ ignoreErrors: false }}
            RETURN OLD
    """
    try:
        update_cursor = await asyncio.to_thread(db.aql.execute, aql_query, bind_vars=bind_vars_for_aql)
        stats = update_cursor.statistics()
        writes_executed_count = stats.get("writes_executed", 0) if stats else 0
        old_doc_list = list(update_cursor)  # type: ignore
        filter_passed_and_doc_found = len(old_doc_list) > 0

        if writes_executed_count > 0:
            logger.info(
                f"成功更新 ArangoDB 中动作状态 (DocKey: {doc_key}, ActionID: {action_id_for_log}). "
                f"更新内容: {updates}. 条件: {expected_conditions}. Writes: {writes_executed_count}"
            )
            return True
        else:
            doc_after_attempt = await asyncio.to_thread(db.collection(collection_name).get, doc_key)
            action_attempted_after = doc_after_attempt.get("action_attempted", {}) if doc_after_attempt else None
            current_db_val_str = (
                str(action_attempted_after)[:300]
                if action_attempted_after is not None
                else "文档未找到或无action_attempted"
            )

            if conditions_were_specified and not filter_passed_and_doc_found:
                logger.warning(
                    f"警告(条件不符): 更新 ArangoDB 中动作状态时，文档 _key '{doc_key}' (action_id: {action_id_for_log}) 更新未执行，因为期望条件未满足. "
                    f"意图更新: {updates}. 期望条件: {expected_conditions}. 当前DB值 (action_attempted): {current_db_val_str}..."
                )
            else:
                # 条件满足了（或无条件），但MERGE后内容无变化
                # 将此处的 logger.info 改为 logger.debug
                logger.debug(
                    f"调试(内容无变化): 更新 ArangoDB 中动作状态时，文档 _key '{doc_key}' (action_id: {action_id_for_log}) 数据未发生实际变化. "
                    f"意图更新: {updates}. 期望条件: {expected_conditions}. 当前DB值 (action_attempted): {current_db_val_str}..."
                )
            return False

    except AQLQueryExecuteError as e_aql:
        if e_aql.http_code == 404 and e_aql.error_code == 1202:
            logger.warning(
                f"警告: 更新 ArangoDB 中动作状态时，文档 _key '{doc_key}' (action_id: {action_id_for_log}) 在集合 '{collection_name}' 中未找到 (AQL Error 1202)。"
            )
        else:
            logger.error(
                f"错误: 更新 ArangoDB 中动作 (DocKey: {doc_key}, ActionID: {action_id_for_log}, 集合: {collection_name}) 状态失败 (AQL执行错误): {e_aql}",
                exc_info=True,
            )
        return False
    except (ArangoServerError, ArangoClientError, DocumentUpdateError) as e_db:
        logger.error(
            f"错误: 更新 ArangoDB 中动作 (DocKey: {doc_key}, ActionID: {action_id_for_log}, 集合: {collection_name}) 状态失败 (数据库服务器/客户端错误): {e_db}",
            exc_info=True,
        )
        return False
    except Exception as e:
        logger.error(
            f"错误: 更新 ArangoDB 中动作 (DocKey: {doc_key}, ActionID: {action_id_for_log}, 集合: {collection_name}) 状态失败 (未知错误): {e}",
            exc_info=True,
        )
        return False


async def update_action_status_by_action_id(
    db: StandardDatabase,
    collection_name: str,
    action_id: str,
    updates: dict[str, Any],
) -> bool:
    """通过 action_attempted.action_id 查找文档并异步更新其 action_attempted 字段。"""
    if not action_id:
        logger.error("update_action_status_by_action_id: action_id 为空，无法更新。")
        return False
    update_object_for_merge = updates.copy()
    update_object_for_merge["updated_at"] = datetime.datetime.now(datetime.UTC).isoformat()

    merge_dict_aql_parts = []
    bind_vars_for_aql: dict[str, Any] = {
        "action_id_to_find": action_id,
    }
    for key, value in update_object_for_merge.items():
        bind_key_name = f"val_{re.sub(r'[^a-zA-Z0-9_]', '_', key)}"
        merge_dict_aql_parts.append(f"'{key}': @{bind_key_name}")
        bind_vars_for_aql[bind_key_name] = value

    if not merge_dict_aql_parts:
        logger.warning(f"没有有效的更新内容应用到 action_id {action_id}。")
        return False

    merge_object_aql_string = f"{{{', '.join(merge_dict_aql_parts)}}}"

    aql_query = f"""
        FOR doc IN {collection_name}
            FILTER doc.action_attempted.action_id == @action_id_to_find
            LIMIT 1
            LET current_action_attempted = IS_OBJECT(doc.action_attempted) ? doc.action_attempted : {{}}
            UPDATE doc WITH {{
                action_attempted: MERGE(current_action_attempted, {merge_object_aql_string})
            }} IN {collection_name}
            OPTIONS {{ ignoreErrors: false }}
            RETURN NEW._key
    """
    try:
        update_cursor = await asyncio.to_thread(db.aql.execute, aql_query, bind_vars=bind_vars_for_aql)
        stats = update_cursor.statistics()
        writes_executed = stats.get("writes_executed", 0) if stats else 0
        if writes_executed > 0:
            logger.info(
                f"成功通过 action_id '{action_id}' 更新 ArangoDB 中动作状态. 更新内容: {updates}. Writes: {writes_executed}"
            )
            return True
        else:
            logger.warning(
                f"通过 action_id '{action_id}' 更新 ArangoDB 动作状态时，没有文档被修改 (writes_executed: 0). "
                f"可能未找到匹配的 action_id 或 action_attempted 结构不正确. 意图更新: {updates}."
            )
            return False
    except Exception as e:
        logger.error(f"错误: 通过 action_id '{action_id}' 更新 ArangoDB 动作状态失败: {e}", exc_info=True)
        return False


async def save_intrusive_thoughts_batch(
    intrusive_thoughts_collection: StandardCollection, thoughts_to_insert: list[dict[str, Any]]
) -> None:
    """异步将一批侵入性思维保存到ArangoDB。"""
    if not thoughts_to_insert:
        logger.debug("(BackgroundIntrusive) 没有新的侵入性思维需要保存。")
        return
    try:
        await asyncio.to_thread(intrusive_thoughts_collection.insert_many, thoughts_to_insert)
        logger.info(
            f"(BackgroundIntrusive) 已向 ArangoDB 池 '{intrusive_thoughts_collection.name}' 中存入 {len(thoughts_to_insert)} 条新的侵入性思维。"
        )
    except DocumentInsertError as e_insert:
        logger.error(
            f"(BackgroundIntrusive) 存入侵入性思维到 ArangoDB 集合 '{intrusive_thoughts_collection.name}' 失败 (DocumentInsertError): {e_insert}",
            exc_info=True,
        )
    except (ArangoServerError, ArangoClientError) as e_db:
        logger.error(
            f"(BackgroundIntrusive) 存入侵入性思维到 ArangoDB 集合 '{intrusive_thoughts_collection.name}' 时发生数据库错误: {e_db}",
            exc_info=True,
        )
    except Exception as e:
        logger.error(
            f"(BackgroundIntrusive) 存入侵入性思维到 ArangoDB 集合 '{intrusive_thoughts_collection.name}' 失败 (未知错误): {e}",
            exc_info=True,
        )


async def get_random_intrusive_thought(
    db: StandardDatabase, collection_name: str = INTRUSIVE_THOUGHTS_POOL_COLLECTION_NAME
) -> dict[str, Any] | None:
    """异步从侵入性思维池中随机抽取一个未使用的思维。"""
    logger.debug(f"(CoreLogic) 尝试从 ArangoDB 集合 '{collection_name}' (数据库: '{db.name}') 随机抽取侵入性思维...")
    try:
        aql_query_sample = """
            FOR doc IN @@collection_name
                FILTER doc.used == false
                SORT RAND()
                LIMIT 1
                RETURN doc
        """
        bind_vars_sample = {"@collection_name": collection_name}
        cursor_sample = await asyncio.to_thread(db.aql.execute, aql_query_sample, bind_vars=bind_vars_sample)
        random_thought_doc = next(cursor_sample, None)
        if random_thought_doc:
            logger.debug(f"(CoreLogic) 成功抽取到侵入性思维: {str(random_thought_doc)[:100]}...")
        else:
            logger.debug(f"(CoreLogic) 未能从集合 '{collection_name}' 中抽取到未使用的侵入性思维。")
        return random_thought_doc
    except (AQLQueryExecuteError, ArangoServerError, ArangoClientError) as e_sample_db:
        logger.warning(
            f"从 ArangoDB 侵入性思维池 '{collection_name}' 取样时发生数据库错误: {e_sample_db}", exc_info=True
        )
        return None
    except Exception as e_sample_intrusive:
        logger.warning(f"从 ArangoDB 侵入性思维池 '{collection_name}' 取样时出错: {e_sample_intrusive}", exc_info=True)
        return None
