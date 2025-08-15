import pytest
from pytest_mock import MockerFixture
from src.database.core.connection_manager import TypeDBConnectionManager
from src.database.models import SummaryDocument
from src.database.services.summary_storage_service import (
    SummaryStorageService,
)


@pytest.fixture
def service(mocker: MockerFixture) -> SummaryStorageService:
    """创建 SummaryStorageService 的实例."""
    mock_conn_manager = mocker.MagicMock(spec=TypeDBConnectionManager)
    mock_conn_manager.get_driver.return_value = mocker.MagicMock()
    type(mock_conn_manager).database_name = mocker.PropertyMock(return_value="test_db")
    return SummaryStorageService(mock_conn_manager)


@pytest.fixture(autouse=True)
def mock_to_thread(mocker: MockerFixture) -> None:
    """自动为所有测试模拟 asyncio.to_thread."""

    async def mock_async_wrapper(func: callable, *args: any, **kwargs: any) -> any:
        """模拟 asyncio.to_thread 的行为."""
        return func(*args, **kwargs)

    mocker.patch("asyncio.to_thread", side_effect=mock_async_wrapper)


@pytest.mark.asyncio
async def test_save_summary(service: SummaryStorageService, mocker: MockerFixture) -> None:
    """测试保存摘要文档."""
    mock_tx = (
        service.conn_manager.get_driver.return_value.transaction.return_value.__enter__.return_value
    )
    mock_query_manager = mock_tx.query
    mock_insert_future = mocker.MagicMock()
    mock_insert_future.resolve.return_value = None
    mock_query_manager.insert.return_value = mock_insert_future

    doc = SummaryDocument(
        _key="sum1",
        conversation_uid="conv1",
        timestamp=1,
        summary_text="Test summary",
        event_ids_covered=["e1"],
    )
    success = await service.save_summary(doc)

    assert success is True
    mock_query_manager.insert.assert_called_once()
    mock_tx.commit.assert_called_once()
