import asyncio
import time

from loguru import logger
from typedb.driver import TransactionType

from ..connection_manager import TypeDBConnectionManager
from ..models import GoalDocument


class GoalStorageService:
    """服务类，负责处理 AI 目标的持久化存储和检索 (TypeDB 版本)."""

    def __init__(self, conn_manager: TypeDBConnectionManager) -> None:
        self.conn_manager = conn_manager
        logger.info("GoalStorageService (TypeDB) 初始化完成。")

    async def load_all_active_goals(self) -> list[GoalDocument]:
        """从数据库加载所有状态为 'active' 的目标."""
        query = """
        match $g isa goal, has status "active";
        $g has goal-id $id;
        $g has goal-text $goal;
        $g has reason-text $reason;
        $g has created-at $created;
        $g has updated-at $updated;
        get $id, $goal, $reason, $created, $updated;
        sort $created asc;
        """
        driver = self.conn_manager.get_driver()
        db_name = self.conn_manager.database_name

        def db_read() -> list[GoalDocument]:
            with driver.transaction(db_name, TransactionType.READ) as tx:
                answers = list(tx.query.get(query).resolve())
                goals = []
                for ans in answers:
                    goals.append(
                        GoalDocument(
                            _key=ans.get("id").as_attribute().get_value().get_string(),
                            goal_text=ans.get("goal").as_attribute().get_value().get_string(),
                            reason_text=ans.get("reason").as_attribute().get_value().get_string(),
                            status="active",
                            created_at=ans.get("created").as_attribute().get_value().get_integer(),
                            updated_at=ans.get("updated").as_attribute().get_value().get_integer(),
                        )
                    )
                return goals

        try:
            return await asyncio.to_thread(db_read)
        except Exception as e:
            logger.error(f"加载所有活动目标失败: {e}", exc_info=True)
            return []

    async def add_goal(self, goal_doc: GoalDocument) -> bool:
        """向数据库中添加一个新的目标文档."""
        driver = self.conn_manager.get_driver()
        db_name = self.conn_manager.database_name

        def db_write() -> bool:
            with driver.transaction(db_name, TransactionType.WRITE) as tx:
                insert_query = f"""
                insert $g isa goal,
                    has goal-id "{goal_doc._key}",
                    has goal-text "{goal_doc.goal_text.replace('"', '\\"')}",
                    has reason-text "{goal_doc.reason_text.replace('"', '\\"')}",
                    has status "{goal_doc.status}",
                    has created-at {goal_doc.created_at},
                    has updated-at {goal_doc.updated_at};
                """
                tx.query.insert(insert_query).resolve()
                tx.commit()
                return True

        try:
            success = await asyncio.to_thread(db_write)
            if success:
                logger.info(f"新目标 '{goal_doc._key}' 已成功存入数据库。")
            return success
        except Exception as e:
            logger.error(f"添加目标 '{goal_doc._key}' 到数据库失败: {e}", exc_info=True)
            return False

    async def update_goal_status(self, goal_id: str, status: str) -> bool:
        """更新数据库中一个目标的状态."""
        driver = self.conn_manager.get_driver()
        db_name = self.conn_manager.database_name

        def db_write() -> bool:
            with driver.transaction(db_name, TransactionType.WRITE) as tx:
                # Delete old status and updated_at
                delete_query = f"""
                match $g isa goal, has goal-id "{goal_id}";
                $g has status $s;
                $g has updated-at $ts;
                delete $g has $s; $g has $ts;
                """
                tx.query.delete(delete_query).resolve()

                # Insert new status and updated_at
                insert_query = f"""
                match $g isa goal, has goal-id "{goal_id}";
                insert $g has status "{status}", has updated-at {int(time.time() * 1000)};
                """
                tx.query.insert(insert_query).resolve()
                tx.commit()
                return True

        try:
            success = await asyncio.to_thread(db_write)
            if success:
                logger.info(f"目标 '{goal_id}' 的状态已在数据库中更新为 '{status}'。")
            return success
        except Exception as e:
            logger.error(f"更新目标 '{goal_id}' 状态失败: {e}", exc_info=True)
            return False
