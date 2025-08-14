import asyncio
import json
from typing import Any

from loguru import logger
from typedb.driver import TransactionType

from ..connection_manager import TypeDBConnectionManager
from ..models import ActionLogDocument


class ActionLogStorageService:
    """服务类，负责处理动作日志的存储和管理 (TypeDB 版本)."""

    def __init__(self, conn_manager: TypeDBConnectionManager) -> None:
        self.conn_manager = conn_manager
        logger.info("ActionLogStorageService (TypeDB) 初始化完成。")

    async def save_action_attempt(self, action_doc: ActionLogDocument) -> bool:
        """保存一个动作尝试到数据库."""
        driver = self.conn_manager.get_driver()
        db_name = self.conn_manager.database_name

        def db_write() -> bool:
            with driver.transaction(db_name, TransactionType.WRITE) as tx:
                match_query = f'match $a isa action-log, has action-id "{action_doc._key}"; get $a;'
                if list(tx.query.get(match_query).resolve()):
                    logger.warning(f"动作尝试 '{action_doc._key}' 的记录已存在，跳过插入。")
                    return True

                insert_parts = [
                    f'$a isa action-log, has action-id "{action_doc._key}"',
                    f'has action-type "{action_doc.action_type}"',
                    f"has timestamp {action_doc.timestamp}",
                    f'has platform "{action_doc.platform}"',
                    f'has bot-id "{action_doc.bot_id}"',
                    f'has status "{action_doc.status}"',
                ]
                insert_query = "insert " + ",\n".join(insert_parts) + ";"
                tx.query.insert(insert_query).resolve()
                tx.commit()
                return True

        try:
            success = await asyncio.to_thread(db_write)
            if success:
                logger.info(f"动作尝试 '{action_doc._key}' ({action_doc.action_type}) 已记录。")
            return success
        except Exception as e:
            logger.error(f"保存动作尝试 '{action_doc._key}' 失败: {e}", exc_info=True)
            return False

    async def update_action_log_with_response(
        self, action_id: str, updates: dict[str, Any]
    ) -> bool:
        """更新动作日志的状态和响应信息."""
        driver = self.conn_manager.get_driver()
        db_name = self.conn_manager.database_name

        def db_write() -> bool:
            with driver.transaction(db_name, TransactionType.WRITE) as tx:
                # 1. First, delete all potentially existing attributes that will be updated.
                # This makes the operation idempotent.
                delete_parts = []
                attr_map_for_delete = {
                    "status": "status",
                    "response_timestamp": "response-timestamp",
                    "response_time_ms": "response-time-ms",
                    "error_info": "error-info",
                    "result_details": "result-details-json",
                }
                for key in updates:
                    if attr_name := attr_map_for_delete.get(key):
                        delete_parts.append(f"$a has {attr_name} ${key};")

                if delete_parts:
                    # We use 'get' here as a way to ensure the variable $a is bound,
                    # the delete happens on attributes found.
                    delete_query = f"""
                    match $a isa action-log, has action-id "{action_id}";
                    {" ".join(delete_parts)}
                    delete $a has $status;, $a has $response_timestamp;, $a has $response_time_ms;, $a has $error_info;, $a has $result_details;;
                    """  # noqa: E501
                    # We resolve delete but don't need the result
                    tx.query.delete(delete_query).resolve()

                # 2. Now, insert the new or updated values.
                insert_parts = [f'match $a isa action-log, has action-id "{action_id}"; insert']

                for key, value in updates.items():
                    if value is None:
                        continue

                    attr_map_for_insert = {
                        "status": "status",
                        "response_timestamp": "response-timestamp",
                        "response_time_ms": "response-time-ms",
                        "error_info": "error-info",
                        "result_details": "result-details-json",
                    }
                    attr_name = attr_map_for_insert.get(key)
                    if not attr_name:
                        continue

                    safe_value: Any
                    if isinstance(value, dict):
                        safe_value = json.dumps(value, ensure_ascii=False).replace('"', '\\"')
                    elif isinstance(value, str):
                        safe_value = value.replace('"', '\\"')
                    else:
                        safe_value = value

                    # *** THIS IS THE CORRECTED PART ***
                    # Use a simple quote variable for clarity and correctness
                    quote = '"' if not isinstance(value, int | float | bool) else ""
                    insert_parts.append(f"$a has {attr_name} {quote}{safe_value}{quote}")

                if len(insert_parts) > 1:
                    insert_query = " ".join(insert_parts) + ";"
                    tx.query.insert(insert_query).resolve()

                tx.commit()
                return True

        try:
            success = await asyncio.to_thread(db_write)
            if success:
                logger.info(f"ActionLog 中动作 '{action_id}' 的状态已更新。")
            return success
        except Exception as e:
            logger.error(f"更新 ActionLog 中动作 '{action_id}' 时失败: {e}", exc_info=True)
            return False
