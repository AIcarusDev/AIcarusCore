import pytest
from src.database.models import ActionLogDocument
from src.database.services import ActionLogStorageService, EntityGraphService
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
