# src/core_logic/goal_manager.py
import time
from dataclasses import dataclass, field

from src.common.custom_logging.logging_config import get_logger
from src.database.models import GoalDocument
from src.database.services.goal_storage_service import GoalStorageService

logger = get_logger(__name__)


@dataclass
class Goal:
    """代表一个短期目标的数据类."""

    id: str
    goal: str
    reason: str
    created_at: int = field(default_factory=lambda: int(time.time() * 1000))
    status: str = "active"


class GoalManager:
    """负责管理AI的短期目标列表."""

    def __init__(self, goal_storage_service: GoalStorageService) -> None:
        self.storage_service = goal_storage_service
        self._goals: dict[str, Goal] = {}
        self._next_id = 1
        logger.info("GoalManager 已初始化。")

    async def initialize(self) -> None:
        """从数据库加载所有活动目标来初始化内存状态."""
        logger.info("GoalManager 正在从数据库加载活动目标...")
        active_goal_docs = await self.storage_service.load_all_active_goals()
        max_id_num = 0
        for doc in active_goal_docs:
            goal = Goal(
                id=doc._key,
                goal=doc.goal,
                reason=doc.reason,
                created_at=doc.created_at,
                status=doc.status,
            )
            self._goals[goal.id] = goal
            # 从加载的目标ID中恢复_next_id计数器
            try:
                id_num = int(doc._key.lstrip('G'))
                if id_num > max_id_num:
                    max_id_num = id_num
            except (ValueError, TypeError):
                continue

        self._next_id = max_id_num + 1
        logger.info(
            f"GoalManager 初始化完成，加载了 {len(self._goals)} 个活动目标。"
            f"下一个ID将是 G{self._next_id}。"
        )

    def _generate_next_id(self) -> str:
        """生成一个格式化的、唯一的、递增的目标ID."""
        goal_id = f"G{self._next_id}"
        self._next_id += 1
        return goal_id

    async def add_goals(self, goals_to_add: list[dict[str, str]]) -> list[str]:
        """添加一个或多个新目标，并持久化到数据库."""
        added_ids = []
        for goal_data in goals_to_add:
            goal_text = goal_data.get("goal")
            reason_text = goal_data.get("reason")
            if goal_text and reason_text:
                new_id = self._generate_next_id()
                # 1. 创建内存中的 Goal 对象
                new_goal = Goal(id=new_id, goal=goal_text, reason=reason_text)
                self._goals[new_id] = new_goal

                # 2. 创建用于数据库的 GoalDocument 对象
                goal_doc = GoalDocument(
                    _key=new_id,
                    goal=goal_text,
                    reason=reason_text
                )

                # 3. 持久化
                if await self.storage_service.add_goal(goal_doc):
                    added_ids.append(new_id)
                    logger.info(f"新增目标 '{new_id}': {goal_text}")
                else:
                    # 如果持久化失败，也从内存中移除
                    self._goals.pop(new_id, None)
                    logger.error(f"持久化新目标 '{new_id}' 失败，操作已回滚。")
        return added_ids

    async def remove_goals(self, goal_ids: list[str]) -> list[str]:
        """根据ID移除一个或多个目标 (逻辑上标记为'completed')."""
        removed_ids = []
        for goal_id in goal_ids:
            if goal_id in self._goals:
                # 1. 从内存中移除
                removed_goal = self._goals.pop(goal_id)

                # 2. 在数据库中更新状态
                if await self.storage_service.update_goal_status(goal_id, "completed"):
                    removed_ids.append(goal_id)
                    logger.info(f"移除目标 '{goal_id}': {removed_goal.goal}")
                else:
                    # 如果更新DB失败，把内存中的目标加回去
                    self._goals[goal_id] = removed_goal
                    logger.error(f"更新目标 '{goal_id}' 状态失败，内存状态已回滚。")

        return removed_ids

    def get_formatted_goals(self) -> str:
        """获取格式化为字符串的目标列表，用于注入Prompt."""
        if not self._goals:
            return "None"

        header = "<!-- 这是你为自己设定的短期目标。你的思考和行动可以围绕它们展开。 -->"
        goal_lines = [
            f"- [{goal.id}] 目标: {goal.goal} (原因: {goal.reason})"
            for goal in self._goals.values()
        ]
        return f"{header}\n" + "\n".join(goal_lines)
