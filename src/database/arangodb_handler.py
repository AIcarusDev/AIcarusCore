import asyncio
import datetime
import os
import re  # 导入 re 模块
from typing import Any

from arango import ArangoClient  # type: ignore
from arango.collection import StandardCollection  # type: ignore
from arango.database import StandardDatabase  # type: ignore
from arango.exceptions import (  # type: ignore
    AQLQueryExecuteError,
    ArangoClientError,
    ArangoServerError,
    DocumentInsertError,
    DocumentUpdateError,
)

from src.common.custom_logging.logger_manager import get_logger
from src.config.alcarus_configs import DatabaseSettings  # 用于类型提示

logger = get_logger("AIcarusCore.database")  # 获取日志记录器


# --- 数据库连接与集合管理 ---
async def connect_to_arangodb(db_config: DatabaseSettings | None = None) -> tuple[ArangoClient, StandardDatabase]:
    """异步连接到ArangoDB并返回客户端和数据库对象。"""
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
        # ArangoClient 的初始化是同步的
        client: ArangoClient = await asyncio.to_thread(ArangoClient, hosts=host)
        # client: ArangoClient = ArangoClient(hosts=host) # 如果ArangoClient本身支持异步上下文管理则更好

        # 数据库操作也应该是异步的或在线程中运行
        sys_db: StandardDatabase = await asyncio.to_thread(client.db, "_system", username=user, password=password)

        if not await asyncio.to_thread(sys_db.has_database, db_name_from_env):
            logger.info(f"数据库 '{db_name_from_env}' 不存在，正在尝试创建...")
            await asyncio.to_thread(sys_db.create_database, db_name_from_env)
            logger.info(f"数据库 '{db_name_from_env}' 创建成功。")

        db: StandardDatabase = await asyncio.to_thread(client.db, db_name_from_env, username=user, password=password)
        await asyncio.to_thread(db.properties)  # 验证连接和权限
        logger.info(f"成功连接到 ArangoDB！主机: {host}, 数据库: {db_name_from_env}")
        return client, db
    except (ArangoServerError, ArangoClientError) as e:
        message = f"连接 ArangoDB 时发生错误 (Host: {host}, DB: {db_name_from_env}): {e}"
        logger.critical(message, exc_info=True)
        raise RuntimeError(message) from e
    except Exception as e:
        message = f"连接 ArangoDB 时发生未知或权限错误 (Host: {host}, DB: {db_name_from_env}, User: {user}): {e}"
        logger.critical(message, exc_info=True)
        raise RuntimeError(message) from e


async def ensure_collection_exists(db: StandardDatabase, collection_name: str) -> StandardCollection:
    """异步确保指定的集合在数据库中存在，如果不存在则创建它。"""
    if not await asyncio.to_thread(db.has_collection, collection_name):
        logger.info(f"集合 '{collection_name}' 在数据库 '{db.name}' 中不存在，正在创建...")
        return await asyncio.to_thread(db.create_collection, collection_name)
    logger.debug(f"集合 '{collection_name}' 已在数据库 '{db.name}' 中存在。")
    return await asyncio.to_thread(db.collection, collection_name)


# --- 主思考数据操作 ---
async def get_latest_thought_document_raw(db: StandardDatabase, collection_name: str) -> dict[str, Any] | None:
    """异步从数据库获取最新的思考文档原文。"""
    logger.debug(
        f"在 get_latest_thought_document_raw 中：准备从 ArangoDB 集合 '{collection_name}' (数据库: '{db.name}') 获取最新文档..."
    )
    try:
        aql_query = """
            FOR doc IN @@collection_name
                SORT doc.timestamp DESC
                LIMIT 1
                RETURN doc
        """
        bind_vars = {"@collection_name": collection_name}
        # AQL 执行应在线程中运行以避免阻塞事件循环
        cursor = await asyncio.to_thread(db.aql.execute, aql_query, bind_vars=bind_vars, ttl=30)
        latest_document = next(cursor, None)  # cursor 的迭代是同步的
        logger.debug(
            f"在 get_latest_thought_document_raw 中：AQL 查询完成。是否找到文档: {latest_document is not None}"
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
            f"在 get_latest_thought_document_raw 中获取最新文档时发生未知错误 (集合: {collection_name}): {e}",
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
        # 集合操作也应在线程中运行
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


async def mark_action_result_as_seen(db: StandardDatabase, collection_name: str, action_id: str) -> None:
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
        stats = update_cursor.statistics()  # statistics() 是同步的
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


# --- 行动状态更新 (原在 action_handler.py) ---
async def update_action_status_in_document(
    db: StandardDatabase,
    collection_name: str,
    doc_key: str,
    action_id_for_log: str,
    updates: dict[str, Any],
) -> None:
    """在ArangoDB中异步更新指定文档键(_key)的记录中的action_attempted字段。"""
    if not doc_key:
        logger.error(
            f"错误: update_action_status_in_document 收到空的 doc_key (action_id: {action_id_for_log})。无法更新。"
        )
        return

    update_object_for_merge = updates.copy()
    update_object_for_merge["updated_at"] = datetime.datetime.now(datetime.UTC).isoformat()

    merge_dict_aql_parts = []
    bind_vars_for_aql: dict[str, Any] = {
        "doc_key_to_update": doc_key,
    }

    for key, value in update_object_for_merge.items():
        bind_key_name = f"val_{re.sub(r'[^a-zA-Z0-9_]', '_', key)}"
        merge_dict_aql_parts.append(f"'{key}': @{bind_key_name}")
        bind_vars_for_aql[bind_key_name] = value

    if not merge_dict_aql_parts:
        logger.warning(f"没有有效的更新内容应用到 action {action_id_for_log} (文档 {doc_key})。")
        return

    merge_object_aql_string = f"{{{', '.join(merge_dict_aql_parts)}}}"

    aql_query = f"""
        FOR doc IN {collection_name}
            FILTER doc._key == @doc_key_to_update
            LIMIT 1
            LET current_action_attempted = IS_OBJECT(doc.action_attempted) ? doc.action_attempted : {{}}
            UPDATE doc WITH {{
                action_attempted: MERGE(current_action_attempted, {merge_object_aql_string} )
            }} IN {collection_name}
            OPTIONS {{ ignoreErrors: false }}
    """
    try:
        update_cursor = await asyncio.to_thread(db.aql.execute, aql_query, bind_vars=bind_vars_for_aql)
        stats = update_cursor.statistics()
        writes_executed_count = stats.get("writes_executed", 0) if stats else 0

        if writes_executed_count > 0:
            logger.info(
                f"成功更新 ArangoDB 中动作状态 (DocKey: {doc_key}, ActionID: {action_id_for_log}, 集合: {collection_name}). 更新内容: {updates}. Writes: {writes_executed_count}"
            )
        else:
            doc_after_update_attempt = await asyncio.to_thread(db.collection(collection_name).get, doc_key)
            if not doc_after_update_attempt:
                logger.warning(
                    f"警告(更新后检查): 文档 _key '{doc_key}' (action_id: {action_id_for_log}) 在集合 '{collection_name}' 中未找到。"
                )
            else:
                current_action_attempted_in_db = doc_after_update_attempt.get("action_attempted", {})
                logger.warning(
                    f"警告: 更新 ArangoDB 中动作状态时，文档 _key '{doc_key}' (action_id: {action_id_for_log}) 在集合 '{collection_name}' 中数据未被修改 (writes_executed: 0)."
                    f" 意图更新: {updates}. 当前DB值 (action_attempted): {str(current_action_attempted_in_db)[:300]}..."
                )

    except AQLQueryExecuteError as e_aql:
        if e_aql.http_code == 404 and e_aql.error_code == 1202:  # Document not found
            logger.warning(
                f"警告: 更新 ArangoDB 中动作状态时，文档 _key '{doc_key}' (action_id: {action_id_for_log}) 在集合 '{collection_name}' 中未找到 (AQL Error 1202)。"
            )
        else:
            logger.error(
                f"错误: 更新 ArangoDB 中动作 (DocKey: {doc_key}, ActionID: {action_id_for_log}, 集合: {collection_name}) 状态失败 (AQL执行错误): {e_aql}",
                exc_info=True,
            )
    except (ArangoServerError, ArangoClientError, DocumentUpdateError) as e_db:
        logger.error(
            f"错误: 更新 ArangoDB 中动作 (DocKey: {doc_key}, ActionID: {action_id_for_log}, 集合: {collection_name}) 状态失败 (数据库服务器/客户端错误): {e_db}",
            exc_info=True,
        )
    except Exception as e:
        logger.error(
            f"错误: 更新 ArangoDB 中动作 (DocKey: {doc_key}, ActionID: {action_id_for_log}, 集合: {collection_name}) 状态失败 (未知错误): {e}",
            exc_info=True,
        )


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


# --- 新增函数：将适配器消息追加到最新思考文档 ---
async def append_to_adapter_messages_in_latest_thought(
    db: StandardDatabase, collection_name: str, message_entry: dict[str, Any]
) -> bool:
    """
    异步将新的消息条目追加到最新思考文档的 'adapter_messages' 数组中。
    如果 'adapter_messages' 字段不存在，则会创建它。
    如果集合为空，则插入一个初始文档。
    """
    aql_query = """
        FOR doc IN @@collection_name
            SORT doc.timestamp DESC
            LIMIT 1
            LET current_messages = IS_ARRAY(doc.adapter_messages) ? doc.adapter_messages : []
            LET new_adapter_messages = PUSH(current_messages, @message_entry)
            UPDATE doc WITH {
                adapter_messages: new_adapter_messages,
                updated_at_adapter_msg: @now
            } IN @@collection_name
            RETURN NEW
    """
    bind_vars = {
        "@collection_name": collection_name,
        "message_entry": message_entry,
        "now": datetime.datetime.now(datetime.UTC).isoformat(),
    }
    try:
        # 确保集合存在
        if not await asyncio.to_thread(db.has_collection, collection_name):
            logger.info(f"集合 {collection_name} 不存在，正在创建...")
            await asyncio.to_thread(db.create_collection, collection_name)

        # 检查集合是否为空
        collection = await asyncio.to_thread(db.collection, collection_name)
        if await asyncio.to_thread(collection.count) == 0:
            logger.warning(f"集合 {collection_name} 为空，插入初始文档...")
            initial_doc = {
                "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
                "adapter_messages": []
            }
            await asyncio.to_thread(collection.insert, initial_doc)

            # 验证初始文档插入
            inserted_docs = await asyncio.to_thread(collection.all)
            logger.debug(f"初始文档插入后集合内容: {list(inserted_docs)}")

        # 执行 AQL 查询
        logger.debug(f"执行 AQL 查询以追加消息到最新文档 (集合: {collection_name})，绑定变量: {bind_vars}...")
        cursor = await asyncio.to_thread(db.aql.execute, aql_query, bind_vars=bind_vars, count=True)  # 启用统计功能
        if cursor is None:
            logger.warning(f"AQL 查询未返回任何结果 (集合: {collection_name})。可能集合为空或查询条件不匹配。")
            return False

        # 打印查询结果以调试
        result = []
        for doc in cursor:
            result.append(doc)

        if not result:
            logger.warning(f"AQL 查询未找到任何文档 (集合: {collection_name})。可能集合为空或查询条件不匹配。")
            return False

        logger.debug(f"AQL 查询结果: {result}")

        # 使用 statistics() 验证写入操作
        stats = cursor.statistics() if cursor else None
        writes_executed = stats.get("writes_executed", 0) if stats else 0

        if writes_executed > 0 or result:
            logger.info(f"成功将消息追加到最新事件文档的 'adapter_messages' 字段 (集合: {collection_name})。")
            return True
        else:
            logger.warning(f"未能找到最新的事件文档来追加适配器消息 (集合: {collection_name})。可能集合为空或查询条件不匹配。")
            return False
    except Exception as e:
        logger.error(f"在 ArangoDB 中追加 'adapter_messages' 时发生错误: {e}", exc_info=True)
        return False


# --- 侵入性思维数据操作 ---
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
        )  # 添加 exc_info
    except (ArangoServerError, ArangoClientError) as e_db:
        logger.error(
            f"(BackgroundIntrusive) 存入侵入性思维到 ArangoDB 集合 '{intrusive_thoughts_collection.name}' 时发生数据库错误: {e_db}",
            exc_info=True,
        )  # 添加 exc_info
    except Exception as e:
        logger.error(
            f"(BackgroundIntrusive) 存入侵入性思维到 ArangoDB 集合 '{intrusive_thoughts_collection.name}' 失败 (未知错误): {e}",
            exc_info=True,
        )  # 添加 exc_info


async def get_random_intrusive_thought(db: StandardDatabase, collection_name: str) -> dict[str, Any] | None:
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
            # 可选：如果需要，在此处标记为已使用。
            # 例如:
            # await asyncio.to_thread(
            #     db.collection(collection_name).update_match,
            #     {'_key': random_thought_doc['_key']},
            #     {'used': True, 'used_at': datetime.datetime.now(datetime.UTC).isoformat()}
            # )
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
