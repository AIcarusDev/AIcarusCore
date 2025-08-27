# src/core_logic/goal_manager.py
import asyncio
import time
from dataclasses import dataclass, field

from src.common.custom_logging.logging_config import get_logger
from src.services.database import GoalDocument, GoalStorageService

logger = get_logger(__name__)


@dataclass
class Goal:
    """代表一个短期目标的数据类."""

    id: str
    goal: str
    reason: str
    created_at: int = field(default_factory=lambda: int(time.time() * 1000))


class GoalManager:
    """负责管理AI的短期目标列表 (持久化改造版)."""

    def __init__(self, goal_storage_service: GoalStorageService) -> None:
        self._goals: dict[str, Goal] = {}
        self._next_id = 1
        self._id_lock = asyncio.Lock()
        self.goal_storage = goal_storage_service  # <-- 存储服务实例
        logger.info("GoalManager 已初始化。")

    async def initialize(self) -> None:
        """从数据库加载所有活动目标来初始化内存状态."""
        logger.info("GoalManager 正在从数据库同步目标...")
        active_goals_docs = await self.goal_storage.load_all_active_goals()
        max_id_num = 0
        for doc in active_goals_docs:
            goal_id_str = doc._key
            self._goals[goal_id_str] = Goal(
                id=goal_id_str,
                goal=doc.goal_text,
                reason=doc.reason_text,
                created_at=doc.created_at,
            )
            # 从ID中解析出数字，用于确定下一个ID的起始值
            try:
                num_part = int(goal_id_str.lstrip("G"))
                if num_part > max_id_num:
                    max_id_num = num_part
            except (ValueError, IndexError):
                continue
        self._next_id = max_id_num + 1
        logger.info(f"成功同步 {len(self._goals)} 个目标。下一个ID将从 G{self._next_id} 开始。")

    def get_actions_schema(self) -> dict:
        """返回此服务提供的所有动作的 JSON Schema 定义."""
        return {
                "manage_goals": {
                "title": "目标管理",
                "type": "object",
                "description": "管理你的短期目标，对应你的`<current_goals>`块。",
                "properties": {
                    "add": {
                        "type": "object",
                        "description": "添加一个或多个新目标。",
                        "properties": {
                            "goals": {
                                "type": "array",
                                "description": "包含新增目标的列表。",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "goal": {
                                            "type": "string",
                                            "description": "目标本身的描述。"
                                        },
                                        "reason": {
                                            "type": "string",
                                            "description": "此目标的背景/原因。",
                                        },
                                    },
                                    "required": ["goal", "reason"],
                                },
                            },
                            "motivation": {"type": "string"},
                        },
                        "required": ["goals", "motivation"],
                    },
                    "remove": {
                        "type": "object",
                        "description": "移除一个或多个已完成/作废/过期的目标。",
                        "properties": {
                            "goal_ids": {
                                "type": "array",
                                "description": "要移除的目标ID列表 (例如 ['G1', 'G3'])。",
                                "items": {"type": "string"},
                            },
                            "motivation": {"type": "string"},
                        },
                        "required": ["goal_ids", "motivation"],
                    },
                },
                "oneOf": [{"required": ["add"]}, {"required": ["remove"]}],
            },
        }

    async def _generate_next_id(self) -> str:
        """生成一个格式化的、唯一的、递增的目标ID."""
        async with self._id_lock:
            goal_id = f"G{self._next_id}"
            self._next_id += 1
        return goal_id

    # +++ 改造 add_goals 方法，增加数据库写入逻辑 +++
    async def add_goals(self, goals_to_add: list[dict[str, str]]) -> list[str]:
        """添加一个或多个新目标，并持久化到数据库."""
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
        db_write_tasks = []
        for goal_data, new_id in zip(valid_goals_data, generated_ids, strict=False):
            goal_text = goal_data["goal"]
            reason_text = goal_data["reason"]
            new_goal = Goal(id=new_id, goal=goal_text, reason=reason_text)
            self._goals[new_id] = new_goal
            added_ids.append(new_id)

            # 创建 GoalDocument 并准备写入数据库
            goal_doc = GoalDocument(
                _key=new_id,
                goal_text=new_goal.goal,
                reason_text=new_goal.reason,
                created_at=new_goal.created_at,
            )
            db_write_tasks.append(self.goal_storage.add_goal(goal_doc))
            logger.info(f"新增目标 '{new_id}': {goal_text}")

        # 并发执行所有数据库写入操作
        await asyncio.gather(*db_write_tasks)
        return added_ids

    async def remove_goals(self, goal_ids: list[str]) -> list[str]:
        """根据ID移除一个或多个目标，并更新数据库状态."""
        removed_ids = []
        db_update_tasks = []
        for goal_id in goal_ids:
            if goal_id in self._goals:
                removed_goal = self._goals.pop(goal_id)
                removed_ids.append(goal_id)
                # 将数据库中的目标状态更新为 "completed"
                db_update_tasks.append(self.goal_storage.update_goal_status(goal_id, "completed"))
                logger.info(f"移除目标 '{goal_id}': {removed_goal.goal}")

        # 并发执行所有数据库更新操作
        await asyncio.gather(*db_update_tasks)
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
