# src/database/services/action_log_storage_service.py
import asyncio
import json
from typing import Any

from src.common.custom_logging.logging_config import get_logger
from typedb.driver import TransactionType

from ..core.connection_manager import TypeDBConnectionManager
from ..models import ActionLogDocument

logger = get_logger(__name__)


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
                match_query = f'match $a isa action-log, has action-id "{action_doc._key}";'
                answers = list(tx.query(match_query).resolve())
                if answers:
                    logger.warning(f"动作尝试 '{action_doc._key}' 的记录已存在，跳过插入。")
                    return True

                insert_parts = [
                    f'$a isa action-log, has action-id "{action_doc._key}"',
                    f'has action-type "{action_doc.action_type}"',
                    f"has timestamp {action_doc.timestamp}",
                    f'has bot-id "{action_doc.bot_id}"',
                    f'has status "{action_doc.status}"',
                    f'has action-platform "{action_doc.platform}"',
                ]

                full_query = f"""
                match $p isa platform, has platform-uid "{action_doc.platform}";
                insert {", ".join(insert_parts)};
                insert (source-platform: $p, sourced-action: $a) isa action-source;
                """

                tx.query(full_query).resolve()
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
                attr_map = {
                    "status": "status",
                    "response_timestamp": "response-timestamp",
                    "response_time_ms": "response-time-ms",
                    "error_info": "error-info",
                    "result_details": "result-details-json",
                }

                for key, value in updates.items():
                    if value is None or not (attr_name := attr_map.get(key)):
                        continue

                    if isinstance(value, dict):
                        safe_value = json.dumps(value, ensure_ascii=False).replace('"', '\\"')
                    elif isinstance(value, str):
                        safe_value = value.replace('"', '\\"')
                    else:
                        safe_value = value

                    quote = '"' if isinstance(value, str | dict) else ""

                    update_query = f"""
                    match $a isa action-log, has action-id "{action_id}";
                    update $a has {attr_name} {quote}{safe_value}{quote};
                    """
                    tx.query(update_query).resolve()

                tx.commit()
                return True

        try:
            success = await asyncio.to_thread(db_write)
            if success:
                logger.info(f"ActionLog 中动作 '{action_id}' 的状态已更新。")
            return success
        except Exception as e:
            logger.error(f"更新 ActionLog 中动作 '{action_id}' 时失败: {repr(e)}", exc_info=True)
            return False

    async def get_recent_action_logs(self, limit: int = 10) -> list[dict]:
        """获取最近的动作日志."""
        query = f"""
        match
            $log isa action-log, has timestamp $ts;
        sort $ts desc;
        limit {limit};
        fetch {{
            "timestamp": $ts,
            "action_type": $log.action-type,
            "status": $log.status,
            "error_info": $log.error-info
        }};
        """
        driver = self.conn_manager.get_driver()
        db_name = self.conn_manager.database_name

        def db_read() -> list[dict]:
            logs = []
            with driver.transaction(db_name, TransactionType.READ) as tx:
                answers_iterator = tx.query(query).resolve().as_concept_documents()
                for doc in answers_iterator:
                    logs.append(
                        {
                            "timestamp": doc.get("timestamp"),
                            "action_type": doc.get("action_type"),
                            "status": doc.get("status"),
                            "error_info": doc.get("error_info"),
                        }
                    )
            return logs

        try:
            return await asyncio.to_thread(db_read)
        except Exception as e:
            logger.error(f"获取最近动作日志失败: {repr(e)}", exc_info=True)
            return []
