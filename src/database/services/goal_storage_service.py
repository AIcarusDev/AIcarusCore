# src/database/services/goal_storage_service.py
import time

from src.common.custom_logging.logging_config import get_logger
from src.database import ArangoDBConnectionManager, CoreDBCollections
from src.database.models import GoalDocument

logger = get_logger(__name__)


class GoalStorageService:
    """服务类，负责处理 AI 目标的持久化存储和检索."""

    def __init__(self, conn_manager: ArangoDBConnectionManager) -> None:
        self.conn_manager = conn_manager
        self.collection_name = CoreDBCollections.GOALS
        logger.info(f"GoalStorageService 初始化，操作集合 '{self.collection_name}'。")

    async def load_all_active_goals(self) -> list[GoalDocument]:
        """从数据库加载所有状态为 'active' 的目标."""
        query = """
            FOR goal IN @@collection
                FILTER goal.status == 'active'
                SORT goal.created_at ASC
                RETURN goal
        """
        bind_vars = {"@collection": self.collection_name}
        results = await self.conn_manager.execute_query(query, bind_vars)
        if results:
            return [GoalDocument(**doc) for doc in results]
        return []

    async def add_goal(self, goal_doc: GoalDocument) -> bool:
        """向数据库中添加一个新的目标文档."""
        try:
            collection = await self.conn_manager.get_collection(self.collection_name)
            await collection.insert(goal_doc.to_dict())
            logger.info(f"新目标 '{goal_doc._key}' 已成功存入数据库。")
            return True
        except Exception as e:
            logger.error(f"添加目标 '{goal_doc._key}' 到数据库失败: {e}", exc_info=True)
            return False

    async def update_goal_status(self, goal_id: str, status: str) -> bool:
        """更新数据库中一个目标的状态."""
        try:
            collection = await self.conn_manager.get_collection(self.collection_name)
            patch = {
                "status": status,
                "updated_at": int(time.time() * 1000)
            }
            await collection.update({"_key": goal_id, **patch})
            logger.info(f"目标 '{goal_id}' 的状态已在数据库中更新为 '{status}'。")
            return True
        except Exception as e:
            logger.error(f"更新目标 '{goal_id}' 状态失败: {e}", exc_info=True)
            return False
