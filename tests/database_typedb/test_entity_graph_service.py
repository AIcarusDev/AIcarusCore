import pytest
from aicarus_protocols import UserInfo as ProtocolUserInfo
from src.database.services import EntityGraphService
from typedb.driver import Driver, TransactionType


@pytest.fixture(scope="function")
async def setup_self_entities(
    db_connection: Driver, entity_graph_service: EntityGraphService
) -> None:
    """一个辅助 fixture，用于预先插入'自己'的实体数据."""
    user_info_qq = ProtocolUserInfo(user_id="99999", user_nickname="AIcarus-QQ")
    await entity_graph_service.create_new_profile_with_account_entity(
        user_info_qq, "qq", is_self=True
    )

    user_info_termux = ProtocolUserInfo(user_id="termux_user", user_nickname="AIcarus-Termux")
    await entity_graph_service.create_new_profile_with_account_entity(
        user_info_termux, "termux", is_self=True
    )


@pytest.mark.asyncio
@pytest.mark.usefixtures("setup_self_entities")
async def test_get_all_self_entities(entity_graph_service: EntityGraphService) -> None:
    """测试获取“祂”自己的所有平台实体信息."""
    result = await entity_graph_service.get_all_self_entities()

    assert len(result) == 2

    # 为了测试稳定性，对结果进行排序
    sorted_result = sorted(result, key=lambda x: x["entity_uid"])

    assert sorted_result[0]["entity_uid"] == "qq_99999"
    assert sorted_result[0]["details"]["platform_id"] == "99999"
    assert sorted_result[0]["details"]["platform"] == "qq"

    assert sorted_result[1]["entity_uid"] == "termux_termux_user"
    assert sorted_result[1]["details"]["nickname"] == "AIcarus-Termux"


@pytest.mark.asyncio
async def test_find_or_create_profile_and_account_entity(
    entity_graph_service: EntityGraphService, db_connection: Driver
) -> None:
    """测试原子性地查找或创建“账户”实体及其关联的“个人”档案."""
    user_info = ProtocolUserInfo(user_id="12345", user_nickname="新用户")
    platform = "test_platform"
    db_name = entity_graph_service.conn_manager.database_name

    # 第一次调用，应该会创建
    (
        profile_id_1,
        account_uid_1,
    ) = await entity_graph_service.find_or_create_profile_and_account_entity(user_info, platform)
    assert profile_id_1 is not None and profile_id_1.startswith("profile_")
    assert account_uid_1 == f"{platform}_{user_info.user_id}"

    # 验证数据已写入
    with db_connection.transaction(db_name, TransactionType.READ) as tx:
        query = f'match $p isa person, has person-uid "{profile_id_1}";'
        assert len(list(tx.query(query).resolve())) == 1

    # 第二次调用，应该会找到已存在的实体
    (
        profile_id_2,
        account_uid_2,
    ) = await entity_graph_service.find_or_create_profile_and_account_entity(user_info, platform)
    assert profile_id_2 == profile_id_1
    assert account_uid_2 == account_uid_1
