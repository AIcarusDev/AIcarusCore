import pytest
from pytest_mock import MockerFixture
from src.database_typedb.connection_manager import TypeDBConnectionManager
from src.database_typedb.models import GoalDocument
from src.database_typedb.services.goal_storage_service import GoalStorageService


@pytest.fixture
def service(mocker: MockerFixture) -> GoalStorageService:
    """创建一个带有模拟连接管理器的服务实例."""
    mock_conn_manager = mocker.MagicMock(spec=TypeDBConnectionManager)
    mock_conn_manager.get_driver.return_value = mocker.MagicMock()
    type(mock_conn_manager).database_name = mocker.PropertyMock(return_value="test_db")
    return GoalStorageService(mock_conn_manager)


def mock_to_thread(mocker: MockerFixture) -> None:
    """自动为所有测试模拟 asyncio.to_thread."""

    async def mock_async_wrapper(func: callable, *args: any, **kwargs: any) -> any:
        return func(*args, **kwargs)

    mocker.patch("asyncio.to_thread", side_effect=mock_async_wrapper)


@pytest.mark.asyncio
async def test_add_goal(service: GoalStorageService, mocker: MockerFixture) -> None:
    """测试添加目标文档."""
    mock_tx = (
        service.conn_manager.get_driver.return_value.transaction.return_value.__enter__.return_value
    )
    mock_query_manager = mock_tx.query
    mock_insert_future = mocker.MagicMock()
    mock_insert_future.resolve.return_value = None
    mock_query_manager.insert.return_value = mock_insert_future

    doc = GoalDocument(_key="G1", goal_text="Test", reason_text="For test")
    success = await service.add_goal(doc)

    assert success is True
    mock_query_manager.insert.assert_called_once()
    mock_tx.commit.assert_called_once()
