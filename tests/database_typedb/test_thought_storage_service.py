import datetime
from unittest.mock import MagicMock

import pytest
from pytest_mock import MockerFixture
from src.database.core.connection_manager import TypeDBConnectionManager

# 修正导入路径为绝对路径，这是测试的最佳实践
from src.database.models import ThoughtChainDocument
from src.database.services.thought_storage_service import (
    ThoughtStorageService,
)


@pytest.fixture
def mock_conn_manager(mocker: MockerFixture) -> MagicMock:
    """模拟 TypeDBConnectionManager."""
    mock = mocker.MagicMock(spec=TypeDBConnectionManager)
    mock.get_driver.return_value = mocker.MagicMock()
    type(mock).database_name = mocker.PropertyMock(return_value="test_db")
    return mock


@pytest.fixture
def service(mock_conn_manager: MagicMock) -> ThoughtStorageService:
    """创建一个带有模拟连接管理器的服务实例."""
    return ThoughtStorageService(mock_conn_manager)


@pytest.fixture(autouse=True)
def mock_to_thread(mocker: MockerFixture) -> None:
    """自动为所有测试模拟 asyncio.to_thread."""

    async def mock_async_wrapper(func: callable, *args: any, **kwargs: any) -> any:
        """模拟 asyncio.to_thread 的行为."""
        return func(*args, **kwargs)

    mocker.patch("asyncio.to_thread", side_effect=mock_async_wrapper)


@pytest.mark.asyncio
async def test_save_thought_and_link_first_thought(
    service: ThoughtStorageService, mocker: MockerFixture
) -> None:
    """测试保存第一个思想点."""
    mock_driver = service.conn_manager.get_driver.return_value
    mock_tx = mock_driver.transaction.return_value.__enter__.return_value
    mock_query_manager = mock_tx.query

    mock_get_future_empty = mocker.MagicMock()
    mock_get_future_empty.resolve.return_value = []
    mock_query_manager.get.return_value = mock_get_future_empty

    mock_insert_future = mocker.MagicMock()
    mock_insert_future.resolve.return_value = None
    mock_query_manager.insert.return_value = mock_insert_future

    # 修正：update 也需要被模拟
    mock_update_future = mocker.MagicMock()
    mock_update_future.resolve.return_value = None
    mock_query_manager.update.return_value = mock_update_future

    thought_doc = ThoughtChainDocument(
        _key="thought_1",
        timestamp=datetime.datetime.now(datetime.UTC).isoformat(),
        mood="初始",
        think="开始思考",
        intent="初始化",
        source_type="core",
    )

    saved_key = await service.save_thought_and_link(thought_doc)

    assert saved_key == "thought_1"
    assert mock_query_manager.insert.call_count == 2
    mock_tx.commit.assert_called_once()


@pytest.mark.asyncio
async def test_save_action_result_to_thought(
    service: ThoughtStorageService, mocker: MockerFixture
) -> None:
    """测试将动作结果保存到思想点."""
    mock_driver = service.conn_manager.get_driver.return_value
    mock_tx = mock_driver.transaction.return_value.__enter__.return_value
    mock_query_manager = mock_tx.query

    mock_insert_future = mocker.MagicMock()
    mock_insert_future.resolve.return_value = None
    mock_query_manager.insert.return_value = mock_insert_future

    success = await service.save_action_result_to_thought("thought_1", "Action successful")

    assert success is True
    mock_query_manager.insert.assert_called_once()
    mock_tx.commit.assert_called_once()


@pytest.mark.asyncio
async def test_save_intrusive_thoughts_batch(
    service: ThoughtStorageService, mocker: MockerFixture
) -> None:
    """测试批量保存侵入性思维."""
    mock_driver = service.conn_manager.get_driver.return_value
    mock_tx = mock_driver.transaction.return_value.__enter__.return_value
    mock_query_manager = mock_tx.query

    mock_insert_future = mocker.MagicMock()
    mock_insert_future.resolve.return_value = None
    mock_query_manager.insert.return_value = mock_insert_future

    thoughts = [{"text": "thought 1"}, {"text": "thought 2"}]
    success = await service.save_intrusive_thoughts_batch(thoughts)

    assert success is True
    assert mock_query_manager.insert.call_count == 2
    mock_tx.commit.assert_called_once()
