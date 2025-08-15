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
                match_query = f'match $a isa action-log; $a has action-id "{action_doc._key}";'
                answers = list(tx.query(match_query).resolve())
                if answers:
                    logger.warning(f"动作尝试 '{action_doc._key}' 的记录已存在，跳过插入。")
                    return True

                insert_parts = [
                    f'$a isa action-log, has action-id "{action_doc._key}"',
                    f', has action-type "{action_doc.action_type}"',
                    f", has timestamp {action_doc.timestamp}",
                    f', has bot-id "{action_doc.bot_id}"',
                    f', has status "{action_doc.status}"',
                    f', has action-platform "{action_doc.platform}"',  # 确保 platform 被添加
                ]

                full_query = f"""
                match $p isa platform; $p has platform-uid "{action_doc.platform}";
                insert {" ".join(insert_parts)};
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
                # --- [核心修复] ---
                # 1. 构建一个单一的查询来删除所有旧属性
                match_parts = [f'match $a isa action-log, has action-id "{action_id}";']
                delete_clauses = []
                attr_map = {
                    "status": "status",
                    "response_timestamp": "response-timestamp",
                    "response_time_ms": "response-time-ms",
                    "error_info": "error-info",
                    "result_details": "result-details-json",
                }

                # 动态构建 match 和 delete 子句
                for key in updates:
                    if attr_name := attr_map.get(key):
                        # 为每个要删除的属性添加 match 子句
                        match_parts.append(f"$a has {attr_name} ${key};")
                        # 为每个要删除的属性添加 delete 子句
                        delete_clauses.append(f"$a has ${key}")

                # 只有当有东西要删除时才执行删除查询
                if delete_clauses:
                    # 组合成一个大的 match-delete 查询
                    delete_query = (
                        " ".join(match_parts) + " delete " + ", ".join(delete_clauses) + ";"
                    )
                    tx.query(delete_query).resolve()

                # 2. 构建一个单一的查询来插入所有新属性
                insert_parts = [f'match $a isa action-log, has action-id "{action_id}"; insert']
                for key, value in updates.items():
                    if value is None or not (attr_name := attr_map.get(key)):
                        continue

                    if isinstance(value, dict):
                        # 对JSON字符串中的双引号进行转义
                        safe_value = json.dumps(value, ensure_ascii=False).replace('"', '\\"')
                    elif isinstance(value, str):
                        safe_value = value.replace('"', '\\"')
                    else:
                        safe_value = value

                    # 字符串值需要用双引号包裹
                    quote = '"' if isinstance(safe_value, str | dict) else ""
                    insert_parts.append(f"$a has {attr_name} {quote}{safe_value}{quote}")

                # 只有当有东西要插入时才执行插入查询
                if len(insert_parts) > 1:
                    # 用逗号连接所有 'has' 子句
                    insert_query = insert_parts[0] + " " + ", ".join(insert_parts[1:]) + ";"
                    tx.query(insert_query).resolve()

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

    async def get_recent_action_logs(self, limit: int = 10) -> list[dict]:
        """获取最近的动作日志."""
        query = f"""
        match $a isa action-log, has timestamp $ts;
        $a has action-type $type;
        $a has status $status;
        $a has error-info $err;
        sort $ts desc; limit {limit};
        """
        driver = self.conn_manager.get_driver()
        db_name = self.conn_manager.database_name

        def db_read() -> list[dict]:
            logs = []
            with driver.transaction(db_name, TransactionType.READ) as tx:
                answers = list(tx.query(query).resolve())
                for ans in answers:
                    logs.append(
                        {
                            "timestamp": ans.get("ts").as_attribute().get_value().as_integer(),
                            "action_type": ans.get("type").as_attribute().get_value().as_string(),
                            "status": ans.get("status").as_attribute().get_value().as_string(),
                            "error_info": (
                                ans.get("err").as_attribute().get_value().as_string()
                                if ans.get("err")
                                else None
                            ),
                        }
                    )
            return logs

        try:
            return await asyncio.to_thread(db_read)
        except Exception as e:
            logger.error(f"获取最近动作日志失败: {e}", exc_info=True)
            return []
