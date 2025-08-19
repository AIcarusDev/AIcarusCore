from collections.abc import AsyncGenerator

import pytest
from pytest_mock import MockerFixture
from src.database.models import ActionLogDocument
from src.database.services import ActionLogStorageService, EntityGraphService
from typedb.driver import Driver, TransactionType

# 标记整个模块的所有测试都需要异步环境
pytestmark = pytest.mark.asyncio
DATA = 'insert $p isa platform, has platform-uid "qq", has display-name "QQ";'
QUERY_SHOULD_FAIL = (
    'match $p isa platform, has platform-uid "qq"; $p has display-name $n; $p has fake-attr $f;'
)
QUERY_SHOULD_SUCCEED = (
    'match $p isa platform, has platform-uid "qq", has display-name $n; select $n;'
)


@pytest.fixture(scope="function")
async def setup_syntax_test_data(db_connection: Driver) -> AsyncGenerator[None, None]:
    """Set up test data for syntax testing by inserting a platform record.

    Parameters
    ----------
    db_connection : Driver
        The TypeDB driver connection instance.

    Yields:
    ------
    None
        This fixture yields control back to the test after setup.
    """
    with db_connection.transaction("aicarus_test_db", TransactionType.WRITE) as tx:
        tx.query(DATA).resolve()
        tx.commit()
    yield


@pytest.mark.usefixtures("setup_syntax_test_data")
@pytest.mark.asyncio
async def test_invalid_query_fails(db_connection: Driver) -> None:
    """Test that invalid queries with non-existent attributes raise TypeDBDriverException."""
    from typedb.common.exception import TypeDBDriverException

    with (
        pytest.raises(TypeDBDriverException),
        db_connection.transaction("aicarus_test_db", TransactionType.READ) as tx,
    ):
        list(tx.query(QUERY_SHOULD_FAIL).resolve())


@pytest.mark.usefixtures("setup_syntax_test_data")
@pytest.mark.asyncio
async def test_valid_query_succeeds(db_connection: Driver) -> None:
    """测试有效查询的成功执行."""
    with db_connection.transaction("aicarus_test_db", TransactionType.READ) as tx:
        results = list(tx.query(QUERY_SHOULD_SUCCEED).resolve().as_concept_rows())
    assert len(results) == 1
    assert results[0].get("n").as_attribute().get_value() == "QQ"


class TestOptionalAttributeQueries:
    """专门测试涉及可选属性查询的 TypeQL 语句，特别是验证 `try-catch` 到 `optional` 模式的修复."""

    @pytest.mark.asyncio
    async def test_update_name_handles_optional_attribute(
        self, entity_graph_service: EntityGraphService, db_connection: Driver
    ) -> None:
        """测试 get_or_create_conversation_entity 方法能否正确处理 display-name 的添加、更新和移除.

        这个测试会间接触发已修复的 _update_conversation_name_if_changed_sync 方法。
        """
        # --- 1. 准备 (Arrange) ---
        platform = "qq"
        conv_id = "group_optional_test"
        conv_type = "group"
        conv_uid = f"{platform}_{conv_type}_{conv_id}"
        initial_name = "Initial Group Name"
        updated_name = "Updated Group Name"
        db_name = entity_graph_service.conn_manager.database_name

        # --- 2. 执行 (Act) & 断言 (Assert) ---

        # 阶段一：创建一个没有 display-name 的会话实体
        await entity_graph_service.get_or_create_conversation_entity(
            conversation_id=conv_id, platform=platform, conv_type=conv_type, name=None
        )

        # 验证：数据库中确实没有 display-name
        with db_connection.transaction(db_name, TransactionType.READ) as tx:
            query_no_name = f'match $c isa conversation, has conversation-uid "{conv_uid}"; not {{ $c has display-name $any; }}; select $c;'  # noqa: E501
            answers_no_name = list(tx.query(query_no_name).resolve().as_concept_rows())
            assert len(answers_no_name) == 1, "创建无名会话失败"

        # 阶段二：调用同样的方法来 *添加* 名称
        await entity_graph_service.get_or_create_conversation_entity(
            conversation_id=conv_id, platform=platform, conv_type=conv_type, name=initial_name
        )

        # 验证：名称已成功添加
        with db_connection.transaction(db_name, TransactionType.READ) as tx:
            query_has_name = f'match $c isa conversation, has conversation-uid "{conv_uid}", has display-name $n; select $n;'  # noqa: E501
            answers_has_name = list(tx.query(query_has_name).resolve().as_concept_rows())
            assert len(answers_has_name) == 1
            assert answers_has_name[0].get("n").as_attribute().get_value() == initial_name

        # 阶段三：再次调用来 *更新* 名称
        await entity_graph_service.get_or_create_conversation_entity(
            conversation_id=conv_id, platform=platform, conv_type=conv_type, name=updated_name
        )

        # 验证：名称已成功更新
        with db_connection.transaction(db_name, TransactionType.READ) as tx:
            query_updated_name = f'match $c isa conversation, has conversation-uid "{conv_uid}", has display-name $n; select $n;'  # noqa: E501
            answers_updated_name = list(tx.query(query_updated_name).resolve().as_concept_rows())
            assert len(answers_updated_name) == 1
            assert answers_updated_name[0].get("n").as_attribute().get_value() == updated_name

        # 阶段四：调用并传入 None 来 *移除* 名称
        await entity_graph_service.get_or_create_conversation_entity(
            conversation_id=conv_id, platform=platform, conv_type=conv_type, name=None
        )

        # 验证：名称已成功移除，回到初始状态
        with db_connection.transaction(db_name, TransactionType.READ) as tx:
            query_removed_name = f'match $c isa conversation, has conversation-uid "{conv_uid}"; not {{ $c has display-name $any; }}; select $c;'  # noqa: E501
            answers_removed_name = list(tx.query(query_removed_name).resolve().as_concept_rows())
            assert len(answers_removed_name) == 1, "移除名称失败"


# --- 辅助函数：用于在测试中设置数据库状态 ---


async def _setup_presence_in_db(
    db_connection: Driver,
    db_name: str,
    self_account_uid: str,
    conv_uid: str,
    cardname: str | None,
    permission_level: str | None,
) -> None:
    """一个辅助函数，用于在数据库中建立'账号'-'会话'的'成员'关系，并可选地添加属性."""
    with db_connection.transaction(db_name, TransactionType.WRITE) as tx:
        # 确保基础实体存在
        # [FIX] 插入具体的 aic_self 类型，而不是抽象的 person 类型
        tx.query('insert $p isa aic_self, has person-uid "aic_person_0";').resolve()
        tx.query(f'insert $acc isa account, has account-uid "{self_account_uid}";').resolve()
        tx.query(f'insert $conv isa conversation, has conversation-uid "{conv_uid}";').resolve()
        tx.query(
            f'match $p isa person, has person-uid "aic_person_0"; $acc isa account, has account-uid "{self_account_uid}"; insert (owner: $p, owned-account: $acc) isa identity-ownership;'  # noqa: E501
        ).resolve()

        # 构建关系和属性的插入语句
        insert_parts = [
            "match",
            f'    $acc isa account, has account-uid "{self_account_uid}";',
            f'    $conv isa conversation, has conversation-uid "{conv_uid}";',
            "insert",
            "    (member: $acc, group: $conv) isa membership",
        ]
        if cardname:
            insert_parts.append(f'        , has cardname "{cardname}"')
        if permission_level:
            insert_parts.append(f'        , has permission-level "{permission_level}"')
        insert_parts[-1] += ";"

        tx.query("\n".join(insert_parts)).resolve()
        tx.commit()


# --- 测试类 ---


class TestEntityGraphServiceFixes:
    """测试 EntityGraphService 中被修复的 TypeQL 查询."""

    async def test_get_self_presence_with_full_attributes(
        self,
        entity_graph_service: EntityGraphService,
        db_connection: Driver,
        mocker: MockerFixture,
    ) -> None:
        """场景1: 测试当 cardname 和 permission-level 都存在时的情况."""
        # 准备
        platform = "qq"
        self_account_uid = f"{platform}_bot123"
        conv_uid = f"{platform}_group_abc"
        db_name = entity_graph_service.conn_manager.database_name
        # [FIX] 使用 mocker.AsyncMock 来正确地 mock 异步方法
        entity_graph_service.get_self_entity_by_platform = mocker.AsyncMock(
            return_value={"entity_uid": self_account_uid}
        )

        await _setup_presence_in_db(
            db_connection, db_name, self_account_uid, conv_uid, "Aicarus-Bot", "admin"
        )

        # 执行
        presence_info = await entity_graph_service.get_self_presence_in_conversation(
            platform, conv_uid
        )

        # 断言
        assert presence_info is not None
        assert presence_info.get("cardname") == "Aicarus-Bot"
        assert presence_info.get("permission_level") == "admin"

    async def test_get_self_presence_with_partial_attributes(
        self,
        entity_graph_service: EntityGraphService,
        db_connection: Driver,
        mocker: MockerFixture,
    ) -> None:
        """场景2: 测试当只有 permission-level 存在时的情况."""
        platform = "qq"
        self_account_uid = f"{platform}_bot123"
        conv_uid = f"{platform}_group_abc"
        db_name = entity_graph_service.conn_manager.database_name
        # [FIX] 使用 mocker.AsyncMock
        entity_graph_service.get_self_entity_by_platform = mocker.AsyncMock(
            return_value={"entity_uid": self_account_uid}
        )

        await _setup_presence_in_db(
            db_connection, db_name, self_account_uid, conv_uid, None, "member"
        )

        presence_info = await entity_graph_service.get_self_presence_in_conversation(
            platform, conv_uid
        )

        assert presence_info is not None
        assert "cardname" not in presence_info  # 验证不存在的键不会出现
        assert presence_info.get("permission_level") == "member"

    async def test_get_self_presence_with_no_optional_attributes(
        self,
        entity_graph_service: EntityGraphService,
        db_connection: Driver,
        mocker: MockerFixture,
    ) -> None:
        """场景3: 测试当关系存在但没有可选属性时，返回空字典."""
        platform = "qq"
        self_account_uid = f"{platform}_bot123"
        conv_uid = f"{platform}_group_abc"
        db_name = entity_graph_service.conn_manager.database_name
        # [FIX] 使用 mocker.AsyncMock
        entity_graph_service.get_self_entity_by_platform = mocker.AsyncMock(
            return_value={"entity_uid": self_account_uid}
        )

        await _setup_presence_in_db(db_connection, db_name, self_account_uid, conv_uid, None, None)

        presence_info = await entity_graph_service.get_self_presence_in_conversation(
            platform, conv_uid
        )

        assert presence_info == {}

    async def test_get_self_presence_when_not_in_group(
        self,
        entity_graph_service: EntityGraphService,
        db_connection: Driver,
        mocker: MockerFixture,
    ) -> None:
        """场景4: 测试当机器人不在群组中时，返回 None."""
        platform = "qq"
        self_account_uid = f"{platform}_bot123"
        conv_uid = f"{platform}_group_abc"
        db_name = entity_graph_service.conn_manager.database_name
        # [FIX] 使用 mocker.AsyncMock
        entity_graph_service.get_self_entity_by_platform = mocker.AsyncMock(
            return_value={"entity_uid": self_account_uid}
        )

        # 只创建实体，不创建 membership 关系
        with db_connection.transaction(db_name, TransactionType.WRITE) as tx:
            tx.query(f'insert $acc isa account, has account-uid "{self_account_uid}";').resolve()
            tx.query(f'insert $conv isa conversation, has conversation-uid "{conv_uid}";').resolve()
            tx.commit()

        presence_info = await entity_graph_service.get_self_presence_in_conversation(
            platform, conv_uid
        )

        assert presence_info is None


class TestActionLogStorageServiceFixes:
    """测试 ActionLogStorageService 中被修复的 TypeQL 查询."""

    async def test_update_action_log_overwrites_existing_attribute(
        self,
        action_log_storage_service: ActionLogStorageService,
        entity_graph_service: EntityGraphService,
        db_connection: Driver,
    ) -> None:
        """测试 update 查询是否能正确覆盖已存在的属性值."""
        service = action_log_storage_service
        db_name = service.conn_manager.database_name
        action_id = "action_overwrite_test"
        platform_id = "qq"

        await entity_graph_service.get_or_create_platform_entity(platform_id, "QQ")
        doc = ActionLogDocument(
            _key=action_id,
            action_type="test",
            timestamp=100,
            platform=platform_id,
            bot_id="bot1",
            status="pending",
        )
        await service.save_action_attempt(doc)

        # 第一次更新
        await service.update_action_log_with_response(action_id, {"status": "running"})
        with db_connection.transaction(db_name, TransactionType.READ) as tx:
            query = f'match $a isa action-log, has action-id "{action_id}"; $a has status $s; select $s;'  # noqa: E501
            answers = list(tx.query(query).resolve().as_concept_rows())
            assert answers[0].get("s").as_attribute().get_value() == "running"

        # 第二次更新（覆盖）
        await service.update_action_log_with_response(action_id, {"status": "completed"})
        with db_connection.transaction(db_name, TransactionType.READ) as tx:
            query = f'match $a isa action-log, has action-id "{action_id}"; $a has status $s; select $s;'  # noqa
            answers = list(tx.query(query).resolve().as_concept_rows())
            assert answers[0].get("s").as_attribute().get_value() == "completed"

    # get_recent_action_logs 的测试已在 test_action_log_storage_service.py 中
    # test_get_recent_action_logs_handles_optional_error_info 得到充分验证，
    # 该测试能证明新的 fetch 语法正确处理了可选属性，故此处不再重复。
