# src/core_logic/goal_manager.py
import asyncio
import time
from dataclasses import dataclass, field

from src.common.custom_logging.logging_config import get_logger

logger = get_logger(__name__)


@dataclass
class Goal:
    """代表一个短期目标的数据类."""

    id: str
    goal: str
    reason: str
    created_at: int = field(default_factory=lambda: int(time.time() * 1000))


class GoalManager:
    """负责管理AI的短期目标列表."""

    def __init__(self) -> None:
        self._goals: dict[str, Goal] = {}
        self._next_id = 1
        # 使用 asyncio.Lock 来处理异步环境中的并发访问
        self._id_lock = asyncio.Lock()
        logger.info("GoalManager 已初始化。")

    async def _generate_next_id(self) -> str:
        """(异步) 生成一个格式化的、唯一的、递增的目标ID."""
        async with self._id_lock:
            goal_id = f"G{self._next_id}"
            self._next_id += 1
        return goal_id

    async def add_goals(self, goals_to_add: list[dict[str, str]]) -> list[str]:
        """(异步) 添加一个或多个新目标，使用 asyncio.gather 并发执行以提高性能."""
        # 1. 首先，筛选出所有有效的目标数据
        valid_goals_data = [
            data for data in goals_to_add if data.get("goal") and data.get("reason")
        ]

        if not valid_goals_data:
            return []

        # 2. 为每个有效目标创建一个 ID 生成任务
        id_generation_tasks = [self._generate_next_id() for _ in valid_goals_data]

        # 3. 使用 asyncio.gather 并发执行所有 ID 生成任务
        generated_ids = await asyncio.gather(*id_generation_tasks)

        # 4. 在所有 ID 都生成后，统一进行同步的添加操作
        added_ids = []
        for goal_data, new_id in zip(valid_goals_data, generated_ids, strict=False):
            goal_text = goal_data["goal"]
            reason_text = goal_data["reason"]
            new_goal = Goal(id=new_id, goal=goal_text, reason=reason_text)
            self._goals[new_id] = new_goal
            added_ids.append(new_id)
            logger.info(f"新增目标 '{new_id}': {goal_text}")

        return added_ids

    async def remove_goals(self, goal_ids: list[str]) -> list[str]:
        """(异步) 根据ID移除一个或多个目标."""
        removed_ids = []
        for goal_id in goal_ids:
            if goal_id in self._goals:
                removed_goal = self._goals.pop(goal_id)
                removed_ids.append(goal_id)
                logger.info(f"移除目标 '{goal_id}': {removed_goal.goal}")
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
