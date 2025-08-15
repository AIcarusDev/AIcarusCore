# tests/database_typedb/test_entity_graph_service.py
from typing import Any
from unittest.mock import MagicMock

import pytest
from aicarus_protocols import UserInfo as ProtocolUserInfo
from pytest_mock import MockerFixture
from src.database.core.connection_manager import TypeDBConnectionManager
from src.database.services.entity_graph_service import (
    SELF_PROFILE_ID,
    EntityGraphService,
)
from src.database.services.event_storage_service import EventStorageService
from typedb.api.answer.concept_row import ConceptRow
from typedb.api.concept.concept import Concept
from typedb.api.concept.instance.attribute import Attribute
from typedb.api.concept.value.value import Value


@pytest.fixture
def mock_conn_manager(mocker: MockerFixture) -> MagicMock:
    """模拟 TypeDBConnectionManager."""
    mock = mocker.MagicMock(spec=TypeDBConnectionManager)
    mock_driver = mocker.MagicMock()
    mock_tx = mocker.MagicMock()
    # 链式模拟: driver.transaction().__enter__() -> tx
    mock_driver.transaction.return_value.__enter__.return_value = mock_tx
    mock_conn_manager.get_driver.return_value = mock_driver
    type(mock_conn_manager).database_name = mocker.PropertyMock(return_value="test_db")
    return mock_conn_manager


@pytest.fixture
def mock_event_storage_service(mocker: MockerFixture) -> MagicMock:
    """模拟 EventStorageService."""
    return mocker.MagicMock(spec=EventStorageService)


@pytest.fixture
def service(
    mock_conn_manager: MagicMock, mock_event_storage_service: MagicMock
) -> EntityGraphService:
    """创建一个带有模拟连接管理器的服务实例."""
    # [修复]: 注入所有依赖
    return EntityGraphService(mock_conn_manager, mock_event_storage_service)


@pytest.fixture(autouse=True)
def mock_to_thread(mocker: MockerFixture) -> None:
    """自动为所有测试模拟 asyncio.to_thread."""
    async def mock_async_wrapper(func, *args, **kwargs):
        return func(*args, **kwargs)
    mocker.patch("asyncio.to_thread", side_effect=mock_async_wrapper)


def create_mock_attribute(mocker: MockerFixture, value: Any, value_type: str) -> MagicMock:
    """[修复]: 更精确地模拟 Concept -> Attribute -> Value 的返回链."""
    mock_value = mocker.MagicMock(spec=Value)
    if value_type == "string":
        mock_value.get_string.return_value = value
    elif value_type == "integer":
        mock_value.get_integer.return_value = value

    mock_attribute = mocker.MagicMock(spec=Attribute)
    mock_attribute.get_value.return_value = mock_value

    mock_concept = mocker.MagicMock(spec=Concept)
    mock_concept.as_attribute.return_value = mock_attribute
    return mock_concept


def setup_query_result(mock_tx: MagicMock, result: list | None):
    """辅助函数，用于设置 tx.query(...).resolve().as_concept_rows() 的返回值."""
    mock_promise = MagicMock()
    mock_query_answer = MagicMock()
    # .resolve() 返回 QueryAnswer, .as_concept_rows() 返回迭代器
    mock_query_answer.as_concept_rows.return_value = iter(result if result is not None else [])
    mock_promise.resolve.return_value = mock_query_answer
    mock_tx.query.return_value = mock_promise
    return mock_promise


@pytest.mark.asyncio
async def test_get_all_self_entities(service: EntityGraphService, mocker: MockerFixture) -> None:
    """测试获取“祂”自己的所有平台实体信息."""
    mock_tx = service.conn_manager.get_driver().transaction.return_value.__enter__.return_value

    mock_row1 = mocker.MagicMock(spec=ConceptRow)
    mock_row1.get.side_effect = lambda key: {
        "uid": create_mock_attribute(mocker, "qq_99999", "string"),
        "pid": create_mock_attribute(mocker, "99999", "string"),
        "nick": create_mock_attribute(mocker, "AIcarus-QQ", "string"),
        "puid": create_mock_attribute(mocker, "qq", "string"), # [修正]: 添加 puid
    }[key]
    mock_row2 = mocker.MagicMock(spec=ConceptRow)
    mock_row2.get.side_effect = lambda key: {
        "uid": create_mock_attribute(mocker, "termux_termux_user", "string"),
        "pid": create_mock_attribute(mocker, "termux_user", "string"),
        "nick": create_mock_attribute(mocker, "AIcarus-Termux", "string"),
        "puid": create_mock_attribute(mocker, "termux", "string"), # [修正]: 添加 puid
    }[key]
    setup_query_result(mock_tx, [mock_row1, mock_row2])

    result = await service.get_all_self_entities()

    assert len(result) == 2
    assert result[0]["entity_uid"] == "qq_99999"
    assert result[0]["details"]["platform_id"] == "99999"
    assert result[0]["details"]["platform"] == "qq"
    assert result[1]["entity_uid"] == "termux_termux_user"
    assert result[1]["details"]["nickname"] == "AIcarus-Termux"
    mock_tx.query.assert_called_once()


@pytest.mark.asyncio
async def test_get_pending_friend_requests(
    service: EntityGraphService, mocker: MockerFixture
) -> None:
    """测试获取待处理的好友请求."""
    mock_tx = service.conn_manager.get_driver().transaction.return_value.__enter__.return_value

    mock_row1 = mocker.MagicMock(spec=ConceptRow)
    mock_row1.get.side_effect = lambda key: {
        "pid": create_mock_attribute(mocker, "11111", "string"),
        "nick": create_mock_attribute(mocker, "Requester One", "string"),
        "f": create_mock_attribute(mocker, "flag_abc", "string"),
        "c": create_mock_attribute(mocker, "Hello", "string"),
        "ts": create_mock_attribute(mocker, 1234567890, "integer"),
    }[key]
    setup_query_result(mock_tx, [mock_row1])

    result = await service.get_pending_friend_requests("qq")

    assert len(result) == 1
    assert result[0]["user_id"] == "11111"
    assert result[0]["comment"] == "Hello"
    assert result[0]["timestamp"] == 1234567890
    mock_tx.query.assert_called_once()


@pytest.mark.asyncio
async def test_get_or_create_platform_entity_when_exists(
    service: EntityGraphService, mocker: MockerFixture
) -> None:
    """测试当平台实体已存在时，函数能正确返回."""
    mock_tx = (
        service.conn_manager.get_driver.return_value.transaction.return_value.__enter__.return_value
    )
    mock_promise = mocker.MagicMock()
    mock_promise.resolve.return_value = [mocker.MagicMock(spec=Concept)]
    mock_tx.query.return_value = mock_promise

    result = await service.get_or_create_platform_entity("qq", "QQ")

    assert result is not None
    assert result["entity_uid"] == "qq"


@pytest.mark.asyncio
async def test_get_or_create_platform_entity_when_not_exists(
    service: EntityGraphService, mocker: MockerFixture
) -> None:
    """测试当平台实体不存在时，函数能正确创建并返回."""
    mock_tx = (
        service.conn_manager.get_driver.return_value.transaction.return_value.__enter__.return_value
    )
    mock_promise_get = mocker.MagicMock()
    mock_promise_get.resolve.return_value = []
    mock_promise_insert = mocker.MagicMock()
    mock_promise_insert.resolve.return_value = None
    mock_tx.query.side_effect = [mock_promise_get, mock_promise_insert]

    result = await service.get_or_create_platform_entity("termux", "Termux")

    assert result is not None
    assert result["entity_uid"] == "termux"
    assert mock_tx.query.call_count == 2


@pytest.mark.asyncio
async def test_create_new_profile_for_external_user(
    service: EntityGraphService, mocker: MockerFixture
) -> None:
    """测试为外部用户创建新的 Profile 和 Account."""
    mock_tx = service.conn_manager.get_driver().transaction.return_value.__enter__.return_value
    # 模拟 find_person_query 返回空, insert 返回 None
    mock_tx.query.return_value.resolve.return_value = []
    user_info = ProtocolUserInfo(user_id="12345", user_nickname="测试用户A")

    profile_id, account_uid = await service.create_new_profile_with_account_entity(
        user_info, "qq", is_self=False
    )

    assert profile_id is not None and profile_id.startswith("profile_")
    assert account_uid == "qq_12345"
    # [修正]: 逻辑是 find person -> insert person -> insert account/relation = 3 次查询
    assert mock_tx.query.call_count == 2 # find_person, insert_account_relation
    mock_tx.commit.assert_called_once()


@pytest.mark.asyncio
async def test_create_new_profile_for_self_when_self_not_exists(
    service: EntityGraphService, mocker: MockerFixture
) -> None:
    """测试当 aic_self 不存在时，为“祂”自己创建 Profile 和 Account."""
    mock_tx = (
        service.conn_manager.get_driver.return_value.transaction.return_value.__enter__.return_value
    )
    mock_tx.query.return_value.resolve.return_value = []
    user_info = ProtocolUserInfo(user_id="99999", user_nickname="AIcarus")

    profile_id, account_uid = await service.create_new_profile_with_account_entity(
        user_info, "qq", is_self=True
    )

    assert profile_id == SELF_PROFILE_ID
    assert account_uid == "qq_99999"

@pytest.mark.asyncio
async def test_find_or_create_when_entity_exists(
    service: EntityGraphService, mocker: MockerFixture
) -> None:
    """测试当实体已存在时，find_or_create 方法能正确找到并返回."""
    mock_tx = service.conn_manager.get_driver().transaction.return_value.__enter__.return_value
    mock_concept_row = mocker.MagicMock(spec=ConceptRow)
    mock_concept_row.get.return_value = create_mock_attribute(mocker, "profile_existing", "string")
    
    # 模拟 find_query 返回结果，update_nickname 返回空
    mock_promise_find = MagicMock()
    mock_query_answer_find = MagicMock()
    mock_query_answer_find.as_concept_rows.return_value = iter([mock_concept_row])
    mock_promise_find.resolve.return_value = mock_query_answer_find
    
    mock_promise_update = MagicMock()
    mock_promise_update.resolve.return_value = []
    mock_tx.query.side_effect = [mock_promise_find, mock_promise_update, mock_promise_update] # find, update(match), update(insert)

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
    mock_tx = (
        service.conn_manager.get_driver.return_value.transaction.return_value.__enter__.return_value
    )
    mock_tx.query.return_value.resolve.return_value = []
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


@pytest.mark.asyncio
async def test_update_presence_in_conversation(
    service: EntityGraphService, mocker: MockerFixture
) -> None:
    """测试更新账户在会话中的存在关系."""
    mock_tx = (
        service.conn_manager.get_driver.return_value.transaction.return_value.__enter__.return_value
    )
    mock_tx.query.return_value.resolve.return_value = None
    user_info = ProtocolUserInfo(user_cardname="天行者", permission_level="admin")

    success = await service.update_presence_in_conversation(
        account_entity_uid="qq_123",
        conversation_entity_uid="qq_group_456",
        user_info=user_info,
        conversation_name="Test Group",
    )

    assert success is True
    assert mock_tx.query.call_count == 2 # delete then insert
    mock_tx.commit.assert_called_once()


@pytest.mark.asyncio
async def test_update_friend_request_status(
    service: EntityGraphService, mocker: MockerFixture
) -> None:
    """测试更新好友请求状态."""
    mock_tx = (
        service.conn_manager.get_driver.return_value.transaction.return_value.__enter__.return_value
    )
    mock_tx.query.return_value.resolve.return_value = None

    success = await service.update_friend_request_status(
        entity_uid="qq_11111", flag="flag_xyz", comment="New request", timestamp=12345
    )

    assert success is True
    assert mock_tx.query.call_count == 2 # delete then insert
    mock_tx.commit.assert_called_once()


@pytest.mark.asyncio
async def test_finalize_friend_request_approved_with_remark(
    service: EntityGraphService, mocker: MockerFixture
) -> None:
    """测试同意好友请求并设置备注."""
    mock_tx = (
        service.conn_manager.get_driver.return_value.transaction.return_value.__enter__.return_value
    )
    mock_tx.query.return_value.resolve.return_value = None

    success = await service.finalize_friend_request(
        entity_uid="qq_11111", approved=True, remark="新朋友"
    )

    assert success is True
    assert mock_tx.query.call_count == 3 # delete request, delete old remark, insert new remark
    mock_tx.commit.assert_called_once()


@pytest.mark.asyncio
async def test_finalize_friend_request_rejected(
    service: EntityGraphService, mocker: MockerFixture
) -> None:
    """测试拒绝好友请求."""
    mock_tx = (
        service.conn_manager.get_driver.return_value.transaction.return_value.__enter__.return_value
    )
    mock_tx.query.return_value.resolve.return_value = None

    success = await service.finalize_friend_request(
        entity_uid="qq_11111", approved=False, remark=None
    )

    assert success is True
    mock_tx.query.assert_called_once() # just delete request
    mock_tx.commit.assert_called_once()