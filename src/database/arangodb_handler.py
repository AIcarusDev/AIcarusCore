import asyncio
import datetime
import logging
import os
from typing import Any  # Python 3.9+ can use tuple, Optional directly

from arango import ArangoClient
from arango.collection import StandardCollection
from arango.database import StandardDatabase
from arango.exceptions import (
    AQLQueryExecuteError,
    ArangoClientError,
    ArangoServerError,
    DocumentInsertError,  # For update operations
)

from src.config.alcarus_configs import DatabaseSettings  # 用于类型提示

logger = logging.getLogger(__name__)

# --- 数据库连接与集合管理 ---


def connect_to_arangodb(db_config: DatabaseSettings) -> tuple[ArangoClient, StandardDatabase]:
    """
    连接到ArangoDB并返回客户端和数据库对象。
    如果连接失败，则打印错误并引发 RuntimeError。
    """
    host = os.getenv(db_config.arangodb_host_env_var)
    user = os.getenv(db_config.arangodb_user_env_var)
    password = os.getenv(db_config.arangodb_password_env_var)
    db_name_from_env = os.getenv(db_config.arangodb_database_env_var)

    if not all([host, user, password, db_name_from_env]):
        message = (
            f"错误：ArangoDB 连接所需的环境变量 "
            f"('{db_config.arangodb_host_env_var}', "
            f"'{db_config.arangodb_user_env_var}', "
            f"'{db_config.arangodb_password_env_var}', "
            f"'{db_config.arangodb_database_env_var}') 未完全设置。"
        )
        logger.critical(message)
        raise ValueError(message)

    try:
        client: ArangoClient = ArangoClient(hosts=host)
        sys_db: StandardDatabase = client.db("_system", username=user, password=password)

        if not sys_db.has_database(db_name_from_env):
            logger.info(f"数据库 '{db_name_from_env}' 不存在，正在尝试创建...")
            sys_db.create_database(db_name_from_env)
            logger.info(f"数据库 '{db_name_from_env}' 创建成功。")

        db: StandardDatabase = client.db(db_name_from_env, username=user, password=password)
        db.properties()
        logger.info(f"成功连接到 ArangoDB！主机: {host}, 数据库: {db_name_from_env}")
        return client, db
    except (ArangoServerError, ArangoClientError) as e:
        message = f"连接 ArangoDB 时发生错误 (Host: {host}, DB: {db_name_from_env}): {e}"
        logger.critical(message, exc_info=True)
        raise RuntimeError(message) from e
    except Exception as e:
        message = f"连接 ArangoDB 时发生未知错误 (Host: {host}, DB: {db_name_from_env}): {e}"
        logger.critical(message, exc_info=True)
        raise RuntimeError(message) from e


def ensure_collection_exists(db: StandardDatabase, collection_name: str) -> StandardCollection:
    """确保指定的集合在数据库中存在，如果不存在则创建它。"""
    if not db.has_collection(collection_name):
        logger.info(f"集合 '{collection_name}' 在数据库 '{db.name}' 中不存在，正在创建...")
        return db.create_collection(collection_name)
    logger.debug(f"集合 '{collection_name}' 已在数据库 '{db.name}' 中存在。")
    return db.collection(collection_name)


# --- 主思考数据操作 ---


async def get_latest_thought_document_raw(db: StandardDatabase, collection_name: str) -> dict[str, Any] | None:
    """
    从数据库获取最新的思考文档原文。
    返回文档字典，如果找不到或出错则返回 None。
    """
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

        cursor = await asyncio.to_thread(db.aql.execute, aql_query, bind_vars=bind_vars)
        latest_document = next(cursor, None)
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
    except Exception as e:  # 其他未知错误
        logger.error(
            f"在 get_latest_thought_document_raw 中获取最新文档时发生未知错误 (集合: {collection_name}): {e}",
            exc_info=True,
        )
        return None


async def save_thought_document(
    thoughts_collection: StandardCollection, document_to_save: dict[str, Any]
) -> str | None:
    """
    将一轮完整的思考结果保存到ArangoDB，并返回新文档的_key。
    """
    if not document_to_save:  # 额外的检查
        logger.warning("save_thought_document 收到空的 document_to_save，不执行保存。")
        return None
    try:
        insert_result = await asyncio.to_thread(thoughts_collection.insert, document_to_save)
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
    """
    通过action_id，在ArangoDB中将对应动作的 result_seen_by_shuang 标记为 True。
    """
    if not action_id:
        logger.debug("mark_action_result_as_seen 收到空的 action_id，不执行操作。")
        return

    aql_query = """
        FOR doc IN @@collection_name
            FILTER doc.action_attempted.action_id == @action_id
               AND doc.action_attempted.status IN ["COMPLETED_SUCCESS", "COMPLETED_FAILURE"]
               AND doc.action_attempted.result_seen_by_shuang == false
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
        # else: # 如果没有更新，可能是因为条件不满足（例如已经标记过了）
        # logger.debug(f"标记动作 (ID: {action_id[:8]}) 已阅时，没有文档被修改 (集合: {collection_name})。")
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
    updates: dict,
) -> None:
    """
    在ArangoDB中异步更新指定文档键(_key)的记录中的action_attempted字段。
    """
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
        bind_key_name = f"val_{key.replace('.', '_')}"
        merge_dict_aql_parts.append(f"'{key}': @{bind_key_name}")
        bind_vars_for_aql[bind_key_name] = value

    merge_object_aql_string = f"{{{', '.join(merge_dict_aql_parts)}}}"

    aql_query = f"""
        FOR doc IN {collection_name}
            FILTER doc._key == @doc_key_to_update
            LIMIT 1
            UPDATE doc WITH {{
                action_attempted: MERGE( (IS_OBJECT(doc.action_attempted) ? doc.action_attempted : {{}}), {merge_object_aql_string} )
            }} IN {collection_name}
            OPTIONS {{ ignoreErrors: false }}
    """
    try:
        update_cursor = await asyncio.to_thread(db.aql.execute, aql_query, bind_vars=bind_vars_for_aql)
        stats = update_cursor.statistics()
        writes_executed_count = stats.get("writes_executed", 0) if stats else 0

        if writes_executed_count > 0:
            logger.info(
                f"成功更新 ArangoDB 中动作状态 (DocKey: {doc_key}, ActionID: {action_id_for_log}, 集合: {collection_name}). Writes: {writes_executed_count}"
            )
        else:
            doc_after_update_attempt = await asyncio.to_thread(db.collection(collection_name).get, doc_key)
            if not doc_after_update_attempt:
                logger.warning(
                    f"警告(更新后检查): 文档 _key '{doc_key}' (action_id: {action_id_for_log}) 在集合 '{collection_name}' 中未找到。"
                )
            else:
                current_action_attempted_in_db = doc_after_update_attempt.get("action_attempted", {})
                all_updates_reflected = True
                for key_to_check, expected_value in updates.items():
                    if current_action_attempted_in_db.get(key_to_check) != expected_value:
                        all_updates_reflected = False
                        logger.warning(
                            f"更新差异: DocKey '{doc_key}', ActionID '{action_id_for_log}'. "
                            f"字段 '{key_to_check}': 期望值 '{expected_value}', 数据库实际值 '{current_action_attempted_in_db.get(key_to_check)}'. "
                            f"Writes executed: 0."
                        )
                        break

                if all_updates_reflected:
                    logger.info(
                        f"信息: ArangoDB 动作状态更新已在文档中反映 (DocKey: {doc_key}, ActionID: {action_id_for_log}). "
                        f"Writes_executed: 0, 但内容已是最新。DB status: '{current_action_attempted_in_db.get('status')}'. "
                        f"DB updated_at: '{current_action_attempted_in_db.get('updated_at')}'."
                    )
                else:
                    logger.warning(
                        f"警告: 更新 ArangoDB 中动作状态时，文档 _key '{doc_key}' (action_id: {action_id_for_log}) 在集合 '{collection_name}' 中数据未完全反映预期更新。"
                        f" Writes executed: 0. Intended updates: {updates}. "
                        f"DB content (action_attempted): {str(current_action_attempted_in_db)[:300]}..."
                    )

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
    except (ArangoServerError, ArangoClientError) as e_db:
        logger.error(
            f"错误: 更新 ArangoDB 中动作 (DocKey: {doc_key}, ActionID: {action_id_for_log}, 集合: {collection_name}) 状态失败 (数据库服务器/客户端错误): {e_db}",
            exc_info=True,
        )
    except Exception as e:
        logger.error(
            f"错误: 更新 ArangoDB 中动作 (DocKey: {doc_key}, ActionID: {action_id_for_log}, 集合: {collection_name}) 状态失败 (未知错误): {e}",
            exc_info=True,
        )


# --- 侵入性思维数据操作 (原在 intrusive_thoughts.py) ---


async def save_intrusive_thoughts_batch(
    intrusive_thoughts_collection: StandardCollection, thoughts_to_insert: list[dict[str, Any]]
) -> None:
    """
    将一批侵入性思维保存到ArangoDB。
    """
    if not thoughts_to_insert:
        logger.debug("(BackgroundIntrusive) 没有新的侵入性思维需要保存。")
        return

    try:
        # python-arango 的 insert_many 是同步的，但在后台线程中直接调用是安全的
        # 如果此函数在 asyncio 事件循环中被调用，则需要 to_thread
        # 假设此函数可能在不同上下文中被调用，统一使用 to_thread
        await asyncio.to_thread(intrusive_thoughts_collection.insert_many, thoughts_to_insert)
        logger.info(
            f"(BackgroundIntrusive) 已向 ArangoDB 池 '{intrusive_thoughts_collection.name}' 中存入 {len(thoughts_to_insert)} 条新的侵入性思维。"
        )
    except DocumentInsertError as e_insert:
        logger.error(
            f"(BackgroundIntrusive) 存入侵入性思维到 ArangoDB 集合 '{intrusive_thoughts_collection.name}' 失败 (DocumentInsertError): {e_insert}"
        )
    except (ArangoServerError, ArangoClientError) as e_db:
        logger.error(
            f"(BackgroundIntrusive) 存入侵入性思维到 ArangoDB 集合 '{intrusive_thoughts_collection.name}' 时发生数据库错误: {e_db}"
        )
    except Exception as e:
        logger.error(
            f"(BackgroundIntrusive) 存入侵入性思维到 ArangoDB 集合 '{intrusive_thoughts_collection.name}' 失败 (未知错误): {e}"
        )


async def get_random_intrusive_thought(db: StandardDatabase, collection_name: str) -> dict[str, Any] | None:
    """
    从侵入性思维池中随机抽取一个未使用的思维。
    """
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
            # 可选：标记为已使用 (如果需要，但当前逻辑似乎是在主循环中直接使用文本)
            # await asyncio.to_thread(
            #     db.collection(collection_name).update_match,
            #     {'_key': random_thought_doc['_key']},
            #     {'used': True}
            # )
        else:
            logger.debug(f"(CoreLogic) 未能从集合 '{collection_name}' 中抽取到未使用的侵入性思维。")
        return random_thought_doc
    except (AQLQueryExecuteError, ArangoServerError, ArangoClientError) as e_sample_db:
        logger.warning(f"从 ArangoDB 侵入性思维池 '{collection_name}' 取样时发生数据库错误: {e_sample_db}")
        return None
    except Exception as e_sample_intrusive:
        logger.warning(f"从 ArangoDB 侵入性思维池 '{collection_name}' 取样时出错: {e_sample_intrusive}")
        return None
