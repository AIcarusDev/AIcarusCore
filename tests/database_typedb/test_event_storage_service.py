import time
from unittest.mock import MagicMock

import pytest
from pytest_mock import MockerFixture
from src.database.core.connection_manager import TypeDBConnectionManager
from src.database.services.event_storage_service import EventStorageService


@pytest.fixture
def mock_conn_manager(mocker: MockerFixture) -> MagicMock:
    """模拟 TypeDBConnectionManager."""
    mock = mocker.MagicMock(spec=TypeDBConnectionManager)
    mock.get_driver.return_value = mocker.MagicMock()
    type(mock).database_name = mocker.PropertyMock(return_value="test_db")
    return mock


@pytest.fixture
def service(mock_conn_manager: MagicMock) -> EventStorageService:
    """创建一个带有模拟连接管理器的服务实例."""
    return EventStorageService(mock_conn_manager)


@pytest.fixture(autouse=True)
def mock_to_thread(mocker: MockerFixture) -> None:
    """自动为所有测试模拟 asyncio.to_thread."""

    async def mock_async_wrapper(func: callable, *args: any, **kwargs: any) -> any:
        """模拟 asyncio.to_thread 的行为."""
        return func(*args, **kwargs)

    mocker.patch("asyncio.to_thread", side_effect=mock_async_wrapper)


@pytest.mark.asyncio
async def test_save_event_document_success(
    service: EventStorageService, mocker: MockerFixture
) -> None:
    """测试成功保存一个事件文档."""
    mock_driver = service.conn_manager.get_driver.return_value
    mock_tx = mock_driver.transaction.return_value.__enter__.return_value
    mock_query_manager = mock_tx.query

    # 模拟 match 查询返回空，表示事件不存在
    mock_get_future = mocker.MagicMock()
    mock_get_future.resolve.return_value = []
    mock_query_manager.get.return_value = mock_get_future

    # 模拟 insert 查询
    mock_insert_future = mocker.MagicMock()
    mock_insert_future.resolve.return_value = None
    mock_query_manager.insert.return_value = mock_insert_future

    event_doc = {
        "_key": "test_event_123",
        "event_type": "message.qq.group",
        "timestamp": int(time.time() * 1000),
        "platform": "qq",
        "bot_id": "bot1",
        "status": "unread",
        "content": [{"type": "text", "data": {"text": "hello"}}],
        "user_info": {"user_id": "user1"},
        "conversation_info": {"conversation_id": "conv1"},
    }

    success = await service.save_event_document(event_doc)

    assert success is True
    mock_query_manager.insert.assert_called_once()
    mock_tx.commit.assert_called_once()


@pytest.mark.asyncio
async def test_save_event_document_already_exists(
    service: EventStorageService, mocker: MockerFixture
) -> None:
    """测试当事件已存在时，操作应被跳过但返回成功."""
    mock_driver = service.conn_manager.get_driver.return_value
    mock_tx = mock_driver.transaction.return_value.__enter__.return_value
    mock_query_manager = mock_tx.query

    # 模拟 match 查询返回一个结果，表示事件已存在
    mock_get_future = mocker.MagicMock()
    mock_get_future.resolve.return_value = [mocker.MagicMock()]  # 返回一个模拟的 answer
    mock_query_manager.get.return_value = mock_get_future

    mock_insert_future = mocker.MagicMock()
    mock_query_manager.insert.return_value = mock_insert_future

    event_doc = {"_key": "test_event_123"}
    success = await service.save_event_document(event_doc)

    assert success is True
    mock_query_manager.insert.assert_not_called()  # 关键：不应调用 insert
    mock_tx.commit.assert_not_called()  # 事务中没有写操作，无需 commit


@pytest.mark.asyncio
async def test_update_events_status(service: EventStorageService, mocker: MockerFixture) -> None:
    """测试批量更新事件状态."""
    mock_driver = service.conn_manager.get_driver.return_value
    mock_tx = mock_driver.transaction.return_value.__enter__.return_value
    mock_query_manager = mock_tx.query

    # 模拟 match, delete, insert
    mock_get_future = mocker.MagicMock()
    mock_get_future.resolve.return_value = [mocker.MagicMock()]  # 假设事件存在
    mock_query_manager.get.return_value = mock_get_future

    mock_delete_future = mocker.MagicMock()
    mock_delete_future.resolve.return_value = None
    mock_query_manager.delete.return_value = mock_delete_future

    mock_insert_future = mocker.MagicMock()
    mock_insert_future.resolve.return_value = None
    mock_query_manager.insert.return_value = mock_insert_future

    event_ids = ["event1", "event2"]
    success = await service.update_events_status(event_ids, "read")

    assert success is True
    assert mock_query_manager.delete.call_count == len(event_ids)
    assert mock_query_manager.insert.call_count == len(event_ids)
    mock_tx.commit.assert_called_once()
