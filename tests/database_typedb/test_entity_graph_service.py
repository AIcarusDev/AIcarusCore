from typing import Any
from unittest.mock import MagicMock

import pytest
from aicarus_protocols import UserInfo as ProtocolUserInfo
from pytest_mock import MockerFixture
from src.database_typedb.connection_manager import TypeDBConnectionManager
from src.database_typedb.services.entity_graph_service import (
    SELF_PROFILE_ID,
    EntityGraphService,
)
from typedb.api.answer.concept_row import ConceptRow
from typedb.api.concept.concept import Concept
from typedb.api.concept.instance.attribute import Attribute
from typedb.api.concept.value.value import Value


@pytest.fixture
def mock_conn_manager(mocker: MockerFixture) -> MagicMock:
    """模拟 TypeDBConnectionManager."""
    mock = mocker.MagicMock(spec=TypeDBConnectionManager)
    mock.get_driver.return_value = mocker.MagicMock()
    type(mock).database_name = mocker.PropertyMock(return_value="test_db")
    return mock


@pytest.fixture
def service(mock_conn_manager: MagicMock) -> EntityGraphService:
    """创建一个带有模拟连接管理器的服务实例."""
    return EntityGraphService(mock_conn_manager)


@pytest.fixture(autouse=True)
def mock_to_thread(mocker: MockerFixture) -> None:
    """自动为所有测试模拟 asyncio.to_thread."""

    async def mock_async_wrapper(func: callable, *args: any, **kwargs: any) -> any:
        """模拟 asyncio.to_thread 的行为."""
        return func(*args, **kwargs)

    mocker.patch("asyncio.to_thread", side_effect=mock_async_wrapper)


# --- 辅助函数，用于创建模拟的 Attribute ---
def create_mock_attribute(mocker: MockerFixture, value: Any, value_type: str) -> MagicMock:
    """辅助函数，用于创建模拟的 Attribute -> Value 链."""
    mock_value = mocker.MagicMock(spec=Value)
    if value_type == "string":
        mock_value.get_string.return_value = value
    elif value_type == "long":
        mock_value.get_integer.return_value = value

    mock_attribute = mocker.MagicMock(spec=Attribute)
    mock_attribute.as_attribute.return_value = mock_attribute
    mock_attribute.get_value.return_value = mock_value
    return mock_attribute


# --- 测试 get_all_self_entities ---


@pytest.mark.asyncio
async def test_get_all_self_entities(service: EntityGraphService, mocker: MockerFixture) -> None:
    """测试获取“祂”自己的所有平台实体信息."""
    mock_driver = service.conn_manager.get_driver.return_value
    mock_tx = mock_driver.transaction.return_value.__enter__.return_value
    mock_query_manager = mock_tx.query

    mock_row1 = mocker.MagicMock(spec=ConceptRow)
    mock_row1.get.side_effect = lambda key: {
        "uid": create_mock_attribute(mocker, "qq_99999", "string"),
        "platform": create_mock_attribute(mocker, "qq", "string"),
        "pid": create_mock_attribute(mocker, "99999", "string"),
        "nick": create_mock_attribute(mocker, "AIcarus-QQ", "string"),
    }[key]

    mock_row2 = mocker.MagicMock(spec=ConceptRow)
    mock_row2.get.side_effect = lambda key: {
        "uid": create_mock_attribute(mocker, "termux_termux_user", "string"),
        "platform": create_mock_attribute(mocker, "termux", "string"),
        "pid": create_mock_attribute(mocker, "termux_user", "string"),
        "nick": create_mock_attribute(mocker, "AIcarus-Termux", "string"),
    }[key]

    mock_get_future = mocker.MagicMock()
    mock_get_future.resolve.return_value = [mock_row1, mock_row2]
    mock_query_manager.get.return_value = mock_get_future

    result = await service.get_all_self_entities()

    assert len(result) == 2
    assert result[0]["entity_uid"] == "qq_99999"
    assert result[0]["details"]["platform_id"] == "99999"
    assert result[1]["entity_uid"] == "termux_termux_user"
    assert result[1]["details"]["nickname"] == "AIcarus-Termux"
    mock_query_manager.get.assert_called_once()


# --- 测试 get_pending_friend_requests ---


@pytest.mark.asyncio
async def test_get_pending_friend_requests(
    service: EntityGraphService, mocker: MockerFixture
) -> None:
    """测试获取待处理的好友请求."""
    mock_driver = service.conn_manager.get_driver.return_value
    mock_tx = mock_driver.transaction.return_value.__enter__.return_value
    mock_query_manager = mock_tx.query

    mock_row1 = mocker.MagicMock(spec=ConceptRow)
    mock_row1.get.side_effect = lambda key: {
        "pid": create_mock_attribute(mocker, "11111", "string"),
        "nick": create_mock_attribute(mocker, "Requester One", "string"),
        "f": create_mock_attribute(mocker, "flag_abc", "string"),
        "c": create_mock_attribute(mocker, "Hello", "string"),
        "ts": create_mock_attribute(mocker, 1234567890, "long"),
    }[key]

    mock_get_future = mocker.MagicMock()
    mock_get_future.resolve.return_value = [mock_row1]
    mock_query_manager.get.return_value = mock_get_future

    result = await service.get_pending_friend_requests("qq")

    assert len(result) == 1
    assert result[0]["user_id"] == "11111"
    assert result[0]["comment"] == "Hello"
    assert result[0]["timestamp"] == 1234567890
    mock_query_manager.get.assert_called_once()


# --- 测试 get_or_create_platform_entity ---


@pytest.mark.asyncio
async def test_get_or_create_platform_entity_when_exists(
    service: EntityGraphService, mocker: MockerFixture
) -> None:
    """测试当平台实体已存在时，函数能正确返回."""
    mock_driver = service.conn_manager.get_driver.return_value
    mock_tx = mock_driver.transaction.return_value.__enter__.return_value
    mock_query_manager = mock_tx.query

    mock_answer_future = mocker.MagicMock()
    mock_answer_future.resolve.return_value = [mocker.MagicMock(spec=Concept)]
    mock_query_manager.get.return_value = mock_answer_future

    result = await service.get_or_create_platform_entity("qq", "QQ")

    assert result is not None
    assert result["entity_uid"] == "qq"


@pytest.mark.asyncio
async def test_get_or_create_platform_entity_when_not_exists(
    service: EntityGraphService, mocker: MockerFixture
) -> None:
    """测试当平台实体不存在时，函数能正确创建并返回."""
    mock_driver = service.conn_manager.get_driver.return_value
    mock_tx = mock_driver.transaction.return_value.__enter__.return_value
    mock_query_manager = mock_tx.query

    mock_answer_future_get = mocker.MagicMock()
    mock_answer_future_get.resolve.return_value = []
    mock_query_manager.get.return_value = mock_answer_future_get

    mock_answer_future_insert = mocker.MagicMock()
    mock_answer_future_insert.resolve.return_value = None
    mock_query_manager.insert.return_value = mock_answer_future_insert

    result = await service.get_or_create_platform_entity("termux", "Termux")

    assert result is not None
    assert result["entity_uid"] == "termux"


# --- 测试 create_new_profile_with_account_entity ---


@pytest.mark.asyncio
async def test_create_new_profile_for_external_user(
    service: EntityGraphService, mocker: MockerFixture
) -> None:
    """测试为外部用户创建新的 Profile 和 Account."""
    mock_driver = service.conn_manager.get_driver.return_value
    mock_tx = mock_driver.transaction.return_value.__enter__.return_value
    mock_query_manager = mock_tx.query

    mock_insert_future = mocker.MagicMock()
    mock_insert_future.resolve.return_value = None
    mock_query_manager.insert.return_value = mock_insert_future

    user_info = ProtocolUserInfo(user_id="12345", user_nickname="测试用户A")

    profile_id, account_uid = await service.create_new_profile_with_account_entity(
        user_info, "qq", is_self=False
    )

    assert profile_id is not None and profile_id.startswith("profile_")
    assert account_uid == "qq_12345"


@pytest.mark.asyncio
async def test_create_new_profile_for_self_when_self_not_exists(
    service: EntityGraphService, mocker: MockerFixture
) -> None:
    """测试当 aic_self 不存在时，为“祂”自己创建 Profile 和 Account."""
    mock_driver = service.conn_manager.get_driver.return_value
    mock_tx = mock_driver.transaction.return_value.__enter__.return_value
    mock_query_manager = mock_tx.query

    mock_get_future = mocker.MagicMock()
    mock_get_future.resolve.return_value = []
    mock_query_manager.get.return_value = mock_get_future

    mock_insert_future = mocker.MagicMock()
    mock_insert_future.resolve.return_value = None
    mock_query_manager.insert.return_value = mock_insert_future

    user_info = ProtocolUserInfo(user_id="99999", user_nickname="AIcarus")

    profile_id, account_uid = await service.create_new_profile_with_account_entity(
        user_info, "qq", is_self=True
    )

    assert profile_id == SELF_PROFILE_ID
    assert account_uid == "qq_99999"


# --- 测试 find_or_create_profile_and_account_entity ---


@pytest.mark.asyncio
async def test_find_or_create_when_entity_exists(
    service: EntityGraphService, mocker: MockerFixture
) -> None:
    """测试当实体已存在时，find_or_create 方法能正确找到并返回."""
    mock_driver = service.conn_manager.get_driver.return_value
    mock_tx = mock_driver.transaction.return_value.__enter__.return_value
    mock_query_manager = mock_tx.query

    mock_concept_row = mocker.MagicMock()
    mock_concept_row.get.return_value = create_mock_attribute(mocker, "profile_existing", "string")

    mock_get_future = mocker.MagicMock()
    mock_get_future.resolve.return_value = [mock_concept_row]
    mock_query_manager.get.return_value = mock_get_future

    spy_create = mocker.spy(service, "create_new_profile_with_account_entity")

    user_info = ProtocolUserInfo(user_id="12345", user_nickname="已存在用户")
    profile_id, account_uid = await service.find_or_create_profile_and_account_entity(
        user_info, "qq"
    )

    assert profile_id == "profile_existing"
    assert account_uid == "qq_12345"
    spy_create.assert_not_called()


@pytest.mark.asyncio
async def test_find_or_create_when_entity_not_exists(
    service: EntityGraphService, mocker: MockerFixture
) -> None:
    """测试当实体不存在时，find_or_create 方法能正确触发创建流程."""
    mock_driver = service.conn_manager.get_driver.return_value
    mock_tx = mock_driver.transaction.return_value.__enter__.return_value
    mock_query_manager = mock_tx.query

    mock_get_future = mocker.MagicMock()
    mock_get_future.resolve.return_value = []
    mock_query_manager.get.return_value = mock_get_future

    mocker.patch.object(
        service,
        "create_new_profile_with_account_entity",
        new_callable=mocker.AsyncMock,
        return_value=("profile_newly_created", "qq_54321"),
    )

    user_info = ProtocolUserInfo(user_id="54321", user_nickname="新用户")
    profile_id, account_uid = await service.find_or_create_profile_and_account_entity(
        user_info, "qq"
    )

    assert profile_id == "profile_newly_created"
    assert account_uid == "qq_54321"
    service.create_new_profile_with_account_entity.assert_awaited_once()


# --- 测试 update_presence_in_conversation ---


@pytest.mark.asyncio
async def test_update_presence_in_conversation(
    service: EntityGraphService, mocker: MockerFixture
) -> None:
    """测试更新账户在会话中的存在关系."""
    mock_driver = service.conn_manager.get_driver.return_value
    mock_tx = mock_driver.transaction.return_value.__enter__.return_value
    mock_query_manager = mock_tx.query

    mock_delete_future = mocker.MagicMock()
    mock_delete_future.resolve.return_value = None
    mock_query_manager.delete.return_value = mock_delete_future

    mock_insert_future = mocker.MagicMock()
    mock_insert_future.resolve.return_value = None
    mock_query_manager.insert.return_value = mock_insert_future

    user_info = ProtocolUserInfo(user_cardname="天行者", permission_level="admin")

    success = await service.update_presence_in_conversation(
        account_entity_uid="qq_123",
        conversation_entity_uid="qq_group_456",
        user_info=user_info,
    )

    assert success is True
    mock_tx.commit.assert_called_once()
    assert mock_query_manager.delete.call_count == 1
    assert mock_query_manager.insert.call_count == 1


# --- 新增的测试用例 ---


@pytest.mark.asyncio
async def test_update_friend_request_status(
    service: EntityGraphService, mocker: MockerFixture
) -> None:
    """测试更新好友请求状态."""
    mock_driver = service.conn_manager.get_driver.return_value
    mock_tx = mock_driver.transaction.return_value.__enter__.return_value
    mock_query_manager = mock_tx.query

    mock_delete_future = mocker.MagicMock()
    mock_delete_future.resolve.return_value = None
    mock_query_manager.delete.return_value = mock_delete_future

    mock_insert_future = mocker.MagicMock()
    mock_insert_future.resolve.return_value = None
    mock_query_manager.insert.return_value = mock_insert_future

    success = await service.update_friend_request_status(
        entity_uid="qq_11111", flag="flag_xyz", comment="New request", timestamp=12345
    )

    assert success is True
    assert mock_query_manager.delete.call_count == 1
    assert mock_query_manager.insert.call_count == 1
    mock_tx.commit.assert_called_once()


@pytest.mark.asyncio
async def test_finalize_friend_request_approved_with_remark(
    service: EntityGraphService, mocker: MockerFixture
) -> None:
    """测试同意好友请求并设置备注."""
    mock_driver = service.conn_manager.get_driver.return_value
    mock_tx = mock_driver.transaction.return_value.__enter__.return_value
    mock_query_manager = mock_tx.query

    mock_delete_future = mocker.MagicMock()
    mock_delete_future.resolve.return_value = None
    mock_query_manager.delete.return_value = mock_delete_future

    mock_insert_future = mocker.MagicMock()
    mock_insert_future.resolve.return_value = None
    mock_query_manager.insert.return_value = mock_insert_future

    success = await service.finalize_friend_request(
        entity_uid="qq_11111", approved=True, remark="新朋友"
    )

    assert success is True
    # 第一次 delete 清除请求属性，第二次 delete 清除旧备注
    assert mock_query_manager.delete.call_count == 2
    # 插入新备注
    assert mock_query_manager.insert.call_count == 1
    mock_tx.commit.assert_called_once()


@pytest.mark.asyncio
async def test_finalize_friend_request_rejected(
    service: EntityGraphService, mocker: MockerFixture
) -> None:
    """测试拒绝好友请求."""
    mock_driver = service.conn_manager.get_driver.return_value
    mock_tx = mock_driver.transaction.return_value.__enter__.return_value
    mock_query_manager = mock_tx.query

    mock_delete_future = mocker.MagicMock()
    mock_delete_future.resolve.return_value = None
    mock_query_manager.delete.return_value = mock_delete_future

    mock_insert_future = mocker.MagicMock()
    mock_insert_future.resolve.return_value = None
    mock_query_manager.insert.return_value = mock_insert_future

    success = await service.finalize_friend_request(
        entity_uid="qq_11111", approved=False, remark=None
    )

    assert success is True
    # 只调用一次 delete 来清除请求属性
    mock_query_manager.delete.assert_called_once()
    # 不应该有任何 insert 操作
    mock_query_manager.insert.assert_not_called()
    mock_tx.commit.assert_called_once()
