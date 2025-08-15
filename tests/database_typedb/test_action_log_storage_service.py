# tests/database_typedb/test_action_log_storage_service.py
import pytest
from pytest_mock import MockerFixture
from src.database.core.connection_manager import TypeDBConnectionManager
from src.database.models import ActionLogDocument
from src.database.services.action_log_storage_service import ActionLogStorageService

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
        return func(*args, **kwargs)
    mocker.patch("asyncio.to_thread", side_effect=mock_async_wrapper)

@pytest.mark.asyncio
async def test_save_action_attempt(service: ActionLogStorageService, mocker: MockerFixture) -> None:
    """测试保存操作尝试."""
    mock_tx = (
        service.conn_manager.get_driver.return_value.transaction.return_value.__enter__.return_value
    )
    # [修正]: tx.query 是一个方法，不是一个带有 .get/.insert 的对象
    # 我们需要模拟 tx.query() 调用本身
    mock_promise = mocker.MagicMock()
    # 第一次调用 (match) 返回空列表，第二次 (insert) 返回 None
    mock_promise.resolve.side_effect = [[], None]
    mock_tx.query.return_value = mock_promise

    doc = ActionLogDocument(
        _key="action1", action_type="send", timestamp=1, platform="qq", bot_id="bot1"
    )
    success = await service.save_action_attempt(doc)

    assert success is True
    # [修正]: 断言 tx.query 被调用了两次
    assert mock_tx.query.call_count == 2
    mock_tx.commit.assert_called_once()