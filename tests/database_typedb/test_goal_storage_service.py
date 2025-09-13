import pytest
from src.services.database.models import GoalDocument
from src.services.database.services import GoalStorageService


@pytest.mark.asyncio
async def test_add_and_load_goals(goal_storage_service: GoalStorageService) -> None:
    """Test adding goals and loading active goals from the storage service.

    Parameters
    ----------
    goal_storage_service : GoalStorageService
        The goal storage service instance for testing.
    """
    service = goal_storage_service
    doc1 = GoalDocument(_key="G1", goal_text="目标一", reason_text="原因一", status="active")
    doc2 = GoalDocument(_key="G2", goal_text="目标二", reason_text="原因二", status="inactive")
    assert await service.add_goal(doc1) is True
    assert await service.add_goal(doc2) is True
    active_goals = await service.load_all_active_goals()
    assert len(active_goals) == 1
    assert active_goals[0]._key == "G1"
    assert await service.update_goal_status("G1", "completed") is True
    active_goals_after_update = await service.load_all_active_goals()
    assert len(active_goals_after_update) == 0
