import pytest
from pytest_mock import MockerFixture
from src.database_typedb.connection_manager import TypeDBConnectionManager
from src.database_typedb.models import ActionLogDocument
from src.database_typedb.services.action_log_storage_service import (
    ActionLogStorageService,
)


@pytest.fixture
def service(mocker: MockerFixture) -> ActionLogStorageService:
    """创建一个带有模拟连接管理器的服务实例."""
    mock_conn_manager = mocker.MagicMock(spec=TypeDBConnectionManager)
    mock_conn_manager.get_driver.return_value = mocker.MagicMock()
    type(mock_conn_manager).database_name = mocker.PropertyMock(return_value="test_db")
    return ActionLogStorageService(mock_conn_manager)


@pytest.fixture(autouse=True)
def mock_to_thread(mocker: MockerFixture) -> None:
    """自动为所有测试模拟 asyncio.to_thread."""

    async def mock_async_wrapper(func: callable, *args: any, **kwargs: any) -> any:
        """模拟 asyncio.to_thread 的行为."""
        return func(*args, **kwargs)

    mocker.patch("asyncio.to_thread", side_effect=mock_async_wrapper)


@pytest.mark.asyncio
async def test_save_action_attempt(service: ActionLogStorageService, mocker: MockerFixture) -> None:
    """测试保存操作尝试."""
    mock_tx = (
        service.conn_manager.get_driver.return_value.transaction.return_value.__enter__.return_value
    )
    mock_query_manager = mock_tx.query
    mock_get_future = mocker.MagicMock()
    mock_get_future.resolve.return_value = []
    mock_query_manager.get.return_value = mock_get_future
    mock_insert_future = mocker.MagicMock()
    mock_insert_future.resolve.return_value = None
    mock_query_manager.insert.return_value = mock_insert_future

    doc = ActionLogDocument(
        _key="action1", action_type="send", timestamp=1, platform="qq", bot_id="bot1"
    )
    success = await service.save_action_attempt(doc)

    assert success is True
    mock_query_manager.insert.assert_called_once()
    mock_tx.commit.assert_called_once()
