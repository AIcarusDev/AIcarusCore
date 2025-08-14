import time

import pytest
from pytest_mock import MockerFixture
from src.database_typedb.connection_manager import TypeDBConnectionManager
from src.database_typedb.services.image_analysis_cache_service import (
    ImageAnalysisCacheService,
)
from tests.database_typedb.test_entity_graph_service import create_mock_attribute


@pytest.fixture
def service(mocker: MockerFixture) -> ImageAnalysisCacheService:
    """创建 ImageAnalysisCacheService 的测试实例."""
    mock_conn_manager = mocker.MagicMock(spec=TypeDBConnectionManager)
    mock_conn_manager.get_driver.return_value = mocker.MagicMock()
    type(mock_conn_manager).database_name = mocker.PropertyMock(return_value="test_db")
    return ImageAnalysisCacheService(mock_conn_manager)


@pytest.fixture(autouse=True)
def mock_to_thread(mocker: MockerFixture) -> None:
    """自动为所有测试模拟 asyncio.to_thread."""

    async def mock_async_wrapper(func: callable, *args: any, **kwargs: any) -> any:
        return func(*args, **kwargs)

    mocker.patch("asyncio.to_thread", side_effect=mock_async_wrapper)


@pytest.mark.asyncio
async def test_get_analysis_by_hash_hit(
    service: ImageAnalysisCacheService, mocker: MockerFixture
) -> None:
    """测试根据哈希值获取图像分析结果."""
    mock_tx = (
        service.conn_manager.get_driver.return_value.transaction.return_value.__enter__.return_value
    )
    mock_query_manager = mock_tx.query

    mock_row = mocker.MagicMock()
    mock_row.get.side_effect = lambda key: {
        "v": create_mock_attribute(mocker, "v1.0", "string"),
        "ts": create_mock_attribute(mocker, int(time.time() * 1000), "long"),
        "res": create_mock_attribute(mocker, '{"desc":"cat"}', "string"),
    }[key]

    mock_get_future = mocker.MagicMock()
    mock_get_future.resolve.return_value = [mock_row]
    mock_query_manager.get.return_value = mock_get_future

    result = await service.get_analysis_by_hash("hash1", "v1.0", 3600)

    assert result is not None
    assert result["desc"] == "cat"
