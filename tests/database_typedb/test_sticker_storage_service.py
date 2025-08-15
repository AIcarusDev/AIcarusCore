# tests/database_typedb/test_sticker_storage_service.py
import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest
from pytest_mock import MockerFixture
from src.database.core.connection_manager import TypeDBConnectionManager
from src.database.services.sticker_storage_service import StickerStorageService
from typedb.api.answer.concept_row import ConceptRow
from typedb.api.concept.instance.attribute import Attribute
from typedb.api.concept.value.value import Value

# --- 辅助函数，用于创建模拟的 Attribute ---
def create_mock_attribute(mocker: MockerFixture, value: Any, value_type: str) -> MagicMock:
    """辅助函数，用于创建模拟的 Attribute -> Value -> Concept 链."""
    mock_value = mocker.MagicMock(spec=Value)
    if value_type == "string":
        mock_value.get_string.return_value = value
    elif value_type == "long":
        mock_value.get_integer.return_value = value

    mock_attribute = mocker.MagicMock(spec=Attribute)
    mock_attribute.as_attribute.return_value = mock_attribute
    mock_attribute.get_value.return_value = mock_value

    mock_concept = mocker.MagicMock()
    mock_concept.as_attribute.return_value = mock_attribute
    return mock_concept


@pytest.fixture
def mock_conn_manager(mocker: MockerFixture) -> MagicMock:
    """模拟 TypeDBConnectionManager."""
    mock = mocker.MagicMock(spec=TypeDBConnectionManager)
    mock.get_driver.return_value = mocker.MagicMock()
    type(mock).database_name = mocker.PropertyMock(return_value="test_db")
    return mock


@pytest.fixture
def service(mock_conn_manager: MagicMock) -> StickerStorageService:
    """创建一个带有模拟连接管理器的服务实例."""
    return StickerStorageService(mock_conn_manager)


@pytest.fixture(autouse=True)
def mock_to_thread(mocker: MockerFixture) -> None:
    """自动为所有测试模拟 asyncio.to_thread."""
    async def mock_async_wrapper(func: callable, *args: any, **kwargs: any) -> any:
        if asyncio.iscoroutinefunction(func):
            return await func(*args, **kwargs)
        return func(*args, **kwargs)
    mocker.patch("asyncio.to_thread", side_effect=mock_async_wrapper)


@pytest.mark.asyncio
async def test_add_sticker(service: StickerStorageService, mocker: MockerFixture) -> None:
    """测试添加新表情包的功能."""
    mock_tx = (
        service.conn_manager.get_driver.return_value.transaction.return_value.__enter__.return_value
    )
    # [修正]: tx.query 是一个方法，需要模拟它
    mock_promise = mocker.MagicMock()
    # 第一次调用 (在 _get_next_sticker_id 中) 返回空列表
    # 第二次调用 (insert) 返回 None
    mock_promise.resolve.side_effect = [[], None]
    mock_tx.query.return_value = mock_promise

    result = await service.add_sticker(
        platform_id="qq",
        filename="test.gif",
        impression="一个测试表情",
        source_image_hash="hash123",
        perceptual_hash="phash456",
    )

    assert result is not None
    assert result["sticker_id"] == "001"
    assert result["filename"] == "test.gif"
    assert mock_tx.query.call_count == 2
    mock_tx.commit.assert_called_once()


@pytest.mark.asyncio
async def test_find_similar_sticker_by_phash_found(
    service: StickerStorageService, mocker: MockerFixture
) -> None:
    """测试当找到相似表情包时的情况."""
    mock_tx = (
        service.conn_manager.get_driver.return_value.transaction.return_value.__enter__.return_value
    )
    mock_row = mocker.MagicMock(spec=ConceptRow)
    mock_row.get.side_effect = lambda key: {
        "uid": create_mock_attribute(mocker, "qq_sticker_001", "string"),
        "phash": create_mock_attribute(mocker, "phash_existing", "string"),
    }[key]
    mock_promise = mocker.MagicMock()
    mock_promise.resolve.return_value = [mock_row]
    mock_tx.query.return_value = mock_promise

    # [修正]: 修正 mocker.patch 的路径
    mocker.patch("src.database.utils.compare_phashes", return_value=True)

    result = await service.find_similar_sticker_by_phash("qq", "phash_new", tolerance=5)

    assert result is not None
    assert result["sticker_uid"] == "qq_sticker_001"


@pytest.mark.asyncio
async def test_remove_sticker(service: StickerStorageService, mocker: MockerFixture) -> None:
    """测试移除表情包的功能."""
    mock_tx = (
        service.conn_manager.get_driver.return_value.transaction.return_value.__enter__.return_value
    )
    mock_promise = mocker.MagicMock()
    mock_promise.resolve.return_value = None
    mock_tx.query.return_value = mock_promise

    success = await service.remove_sticker("qq", "001")

    assert success is True
    mock_tx.query.assert_called_once()
    mock_tx.commit.assert_called_once()