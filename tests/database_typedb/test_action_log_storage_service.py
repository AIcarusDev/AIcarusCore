import pytest
from src.services.database.models import ActionLogDocument
from src.services.database.services import ActionLogStorageService, EntityGraphService
from typedb.driver import Driver, TransactionType


@pytest.mark.asyncio
async def test_save_and_update_action_log(
    action_log_storage_service: ActionLogStorageService,
    entity_graph_service: EntityGraphService,
    db_connection: Driver,
) -> None:
    """测试保存和更新动作日志."""
    service = action_log_storage_service
    db_name = service.conn_manager.database_name

    platform_id = "qq"
    await entity_graph_service.get_or_create_platform_entity(platform_id, "QQ")

    doc = ActionLogDocument(
        _key="action1",
        action_type="send",
        timestamp=123,
        platform=platform_id,
        bot_id="bot1",
        status="pending",
    )
    success_save = await service.save_action_attempt(doc)
    assert success_save is True, "保存初始动作失败"

    with db_connection.transaction(db_name, TransactionType.READ) as tx:
        query = 'match $a isa action-log, has action-id "action1"; $a has status $s; select $s;'
        answers = list(tx.query(query).resolve().as_concept_rows())
        assert len(answers) == 1, "初始动作日志未能成功保存"
        assert answers[0].get("s").as_attribute().get_value() == "pending"

    updates = {
        "status": "completed",
        "response_timestamp": 456,
        "result_details": {"info": "ok"},
    }
    success_update = await service.update_action_log_with_response("action1", updates)
    assert success_update is True, "更新动作日志失败"

    with db_connection.transaction(db_name, TransactionType.READ) as tx:
        query = """
        match $a isa action-log, has action-id "action1";
        $a has status $s;
        $a has response-timestamp $ts;
        $a has result-details-json $rd;
        select $s, $ts, $rd;
        """
        answers = list(tx.query(query).resolve().as_concept_rows())
        assert len(answers) == 1, "更新后的动作日志未能找到"
        assert answers[0].get("s").as_attribute().get_value() == "completed"
        assert answers[0].get("ts").as_attribute().get_value() == 456
        assert answers[0].get("rd").as_attribute().get_value() == '{"info": "ok"}'


@pytest.mark.asyncio
async def test_get_recent_action_logs_handles_optional_error_info(
    action_log_storage_service: ActionLogStorageService,
    db_connection: Driver,
) -> None:
    """测试 get_recent_action_logs 方法能否正确处理可选的 error-info 属性."""
    service = action_log_storage_service
    db_name = service.conn_manager.database_name

    # 准备：插入三条日志，两条没有 error-info，一条有
    with db_connection.transaction(db_name, TransactionType.WRITE) as tx:
        # 必须先有一个 platform 实体才能创建关系
        tx.query('insert $p isa platform, has platform-uid "test";').resolve()

        # 日志1：时间戳最早，无错误信息
        tx.query("""
            match $p isa platform, has platform-uid "test";
            insert $a isa action-log, has action-id "log1", has action-type "type1",
                   has timestamp 1000, has status "ok", has bot-id "bot", has action-platform "test";
            insert (source-platform: $p, sourced-action: $a) isa action-source;
        """).resolve()  # noqa: E501

        # 日志2：时间戳居中，有错误信息
        tx.query("""
            match $p isa platform, has platform-uid "test";
            insert $a isa action-log, has action-id "log2", has action-type "type2",
                   has timestamp 2000, has status "error", has error-info "something bad happened",
                   has bot-id "bot", has action-platform "test";
            insert (source-platform: $p, sourced-action: $a) isa action-source;
        """).resolve()

        # 日志3：时间戳最新，无错误信息
        tx.query("""
            match $p isa platform, has platform-uid "test";
            insert $a isa action-log, has action-id "log3", has action-type "type3",
                   has timestamp 3000, has status "ok", has bot-id "bot", has action-platform "test";
            insert (source-platform: $p, sourced-action: $a) isa action-source;
        """).resolve()  # noqa: E501
        tx.commit()

    # 执行：获取最近的2条日志
    recent_logs = await service.get_recent_action_logs(limit=2)

    # 断言：
    assert len(recent_logs) == 2, "limit 参数未能正确生效"

    # 验证第一条（最新的）日志，它没有 error-info
    log_3 = recent_logs[0]
    assert log_3["timestamp"] == 3000
    assert log_3["action_type"] == "type3"
    assert log_3["status"] == "ok"
    assert log_3["error_info"] is None, "不含 error-info 的日志应返回 None"

    # 验证第二条日志，它有 error-info
    log_2 = recent_logs[1]
    assert log_2["timestamp"] == 2000
    assert log_2["action_type"] == "type2"
    assert log_2["status"] == "error"
    assert log_2["error_info"] == "something bad happened", "含 error-info 的日志未能正确返回值"
