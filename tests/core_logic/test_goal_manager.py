# tests/core_logic/test_goal_manager.py

import pytest
from src.core_logic.goal_manager import GoalManager

# 标记整个模块的所有测试都需要异步环境
pytestmark = pytest.mark.asyncio


@pytest.fixture
def goal_manager() -> GoalManager:
    """提供一个全新的 GoalManager 实例。"""
    return GoalManager()


async def test_add_single_goal(goal_manager: GoalManager):
    """测试添加单个目标。"""
    # 准备
    goal_to_add = [{"goal": "学习 pytest", "reason": "为了写好测试"}]

    # 执行
    added_ids = await goal_manager.add_goals(goal_to_add)

    # 断言
    assert len(added_ids) == 1
    assert added_ids[0] == "G1"

    formatted_goals = goal_manager.get_formatted_goals()
    assert "[G1] 目标: 学习 pytest (原因: 为了写好测试)" in formatted_goals


async def test_add_multiple_goals(goal_manager: GoalManager):
    """测试并发添加多个目标。"""
    # 准备
    goals_to_add = [
        {"goal": "目标A", "reason": "原因A"},
        {"goal": "目标B", "reason": "原因B"},
    ]

    # 执行
    added_ids = await goal_manager.add_goals(goals_to_add)

    # 断言
    assert len(added_ids) == 2
    assert "G1" in added_ids
    assert "G2" in added_ids

    formatted_goals = goal_manager.get_formatted_goals()
    assert "[G1]" in formatted_goals
    assert "[G2]" in formatted_goals


async def test_remove_goals(goal_manager: GoalManager):
    """测试移除目标。"""
    # 准备
    await goal_manager.add_goals([
        {"goal": "目标A", "reason": "原因A"},
        {"goal": "目标B", "reason": "原因B"},
        {"goal": "目标C", "reason": "原因C"},
    ])

    # 执行
    removed_ids = await goal_manager.remove_goals(["G1", "G3"])

    # 断言
    assert removed_ids == ["G1", "G3"]

    formatted_goals = goal_manager.get_formatted_goals()
    assert "[G1]" not in formatted_goals
    assert "[G2]" in formatted_goals
    assert "[G3]" not in formatted_goals


async def test_get_formatted_goals_when_empty(goal_manager: GoalManager):
    """测试目标列表为空时，格式化输出为 'None'。"""
    assert goal_manager.get_formatted_goals() == "None"
