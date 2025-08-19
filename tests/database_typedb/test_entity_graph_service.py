import pytest
from aicarus_protocols import UserInfo as ProtocolUserInfo
from src.database.services import EntityGraphService

# --- [核心修复] ---
# 导入缺失的 SELF_PROFILE_ID 常量
from src.database.services.entity_graph_service import SELF_PROFILE_ID

# --- [修复结束] ---
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


class TestConversationEntity:
    """专门测试会话实体（特别是群聊名称）的创建和更新逻辑."""

    async def test_creates_group_with_name(
        self, entity_graph_service: EntityGraphService, db_connection: Driver
    ) -> None:
        """测试场景1 (Happy Path): 创建一个带有名称的群聊实体."""
        # 1. 准备 (Arrange)
        conv_id = "group1"
        platform = "qq"
        conv_type = "group"
        name = "测试群组"
        db_name = entity_graph_service.conn_manager.database_name

        # 2. 执行 (Act)
        entity = await entity_graph_service.get_or_create_conversation_entity(
            conversation_id=conv_id, platform=platform, conv_type=conv_type, name=name
        )

        # 3. 断言 (Assert)
        assert entity is not None
        # 直接查询数据库，验证 display-name 属性是否被正确写入
        with db_connection.transaction(db_name, TransactionType.READ) as tx:
            query = f'match $c isa conversation, has conversation-id "{conv_id}", has display-name $n; select $n;'  # noqa: E501
            answers = list(tx.query(query).resolve().as_concept_rows())
            assert len(answers) == 1
            assert answers[0].get("n").as_attribute().get_value() == name

    async def test_creates_group_without_name(
        self, entity_graph_service: EntityGraphService, db_connection: Driver
    ) -> None:
        """测试场景2 (Bug复现): 创建一个 name=None 的群聊实体."""
        # 1. 准备 (Arrange)
        conv_id = "group2"
        platform = "qq"
        conv_type = "group"
        db_name = entity_graph_service.conn_manager.database_name

        # 2. 执行 (Act)
        entity = await entity_graph_service.get_or_create_conversation_entity(
            conversation_id=conv_id, platform=platform, conv_type=conv_type, name=None
        )

        # 3. 断言 (Assert)
        assert entity is not None
        # 直接查询数据库，验证 display-name 属性是否 *不存在*
        with db_connection.transaction(db_name, TransactionType.READ) as tx:
            # 这个查询会查找有 conv_id 但没有 display-name 的实体
            query = f'match $c isa conversation, has conversation-id "{conv_id}"; not {{ $c has display-name $any_name; }}; select $c;'  # noqa: E501
            answers = list(tx.query(query).resolve().as_concept_rows())
            # 我们期望能找到这样一个实体，证明它被创建了但是是“无名”的
            assert len(answers) == 1

    async def test_updates_group_name_on_subsequent_call(
        self, entity_graph_service: EntityGraphService, db_connection: Driver
    ) -> None:
        """测试场景3 (更新路径): 先创建一个无名群聊，再用有名称的数据调用，验证其名称被更新."""
        # 1. 准备 (Arrange) - 第一次调用，无名称
        conv_id = "group3"
        platform = "qq"
        conv_type = "group"
        new_name = "后来补上的群名"
        db_name = entity_graph_service.conn_manager.database_name

        await entity_graph_service.get_or_create_conversation_entity(
            conversation_id=conv_id, platform=platform, conv_type=conv_type, name=None
        )

        # 2. 执行 (Act) - 第二次调用，有名称
        await entity_graph_service.get_or_create_conversation_entity(
            conversation_id=conv_id, platform=platform, conv_type=conv_type, name=new_name
        )

        # 3. 断言 (Assert)
        # 直接查询数据库，验证 display-name 是否已成功更新
        with db_connection.transaction(db_name, TransactionType.READ) as tx:
            query = f'match $c isa conversation, has conversation-id "{conv_id}", has display-name $n; select $n;'  # noqa: E501
            answers = list(tx.query(query).resolve().as_concept_rows())
            assert len(answers) == 1
            assert answers[0].get("n").as_attribute().get_value() == new_name


class TestEntityGraphServiceFixes:
    """测试 EntityGraphService 中被修复的 TypeQL 查询."""

    async def test_update_conversation_last_read_timestamp_upserts_correctly(
        self, entity_graph_service: EntityGraphService, db_connection: Driver
    ) -> None:
        """测试 update_conversation_last_read_timestamp 方法能否正确地创建和更新时间戳."""
        # 1. 准备 (Arrange)
        conv_id = "group_ts_test"
        platform = "qq"
        conv_type = "group"
        conv_uid = f"{platform}_{conv_type}_{conv_id}"
        db_name = entity_graph_service.conn_manager.database_name

        # 在数据库中创建必要的 person 和 conversation 实体
        with db_connection.transaction(db_name, TransactionType.WRITE) as tx:
            tx.query(f'insert $p isa aic_self, has person-uid "{SELF_PROFILE_ID}";').resolve()
            tx.query(f'insert $c isa conversation, has conversation-uid "{conv_uid}";').resolve()
            tx.commit()

        def get_timestamp() -> int | None:
            """辅助函数，用于从数据库查询当前的时间戳."""
            with db_connection.transaction(db_name, TransactionType.READ) as tx:
                query = f"""
                match
                    $p isa person, has person-uid "{SELF_PROFILE_ID}";
                    $c isa conversation, has conversation-uid "{conv_uid}";
                    (reader: $p, readable: $c) isa read-status, has timestamp $ts;
                select $ts;
                """
                answers = list(tx.query(query).resolve().as_concept_rows())
                if answers:
                    return answers[0].get("ts").as_attribute().get_value()
                return None

        # 2. 执行与断言 (Act & Assert) - 阶段一：创建
        # 此时关系不存在，应该会创建
        success_create = await entity_graph_service.update_conversation_last_read_timestamp(
            conv_uid, 1000.0
        )
        assert success_create is True
        timestamp_after_create = get_timestamp()
        assert timestamp_after_create == 1000

        # 3. 执行与断言 (Act & Assert) - 阶段二：更新
        # 此时关系已存在，应该会更新
        success_update = await entity_graph_service.update_conversation_last_read_timestamp(
            conv_uid, 5000.0
        )
        assert success_update is True
        timestamp_after_update = get_timestamp()
        assert timestamp_after_update == 5000

        # 验证关系只有一个
        with db_connection.transaction(db_name, TransactionType.READ) as tx:
            query_count = f"""
            match
                $p isa person, has person-uid "{SELF_PROFILE_ID}";
                $c isa conversation, has conversation-uid "{conv_uid}";
                (reader: $p, readable: $c) isa read-status;
            reduce $count = count;
            """
            answers_count = list(tx.query(query_count).resolve().as_concept_rows())
            assert answers_count[0].get("count").as_value().get_integer() == 1
