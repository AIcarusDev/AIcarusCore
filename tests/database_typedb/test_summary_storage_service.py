import time

import pytest
from src.database.models import SummaryDocument
from src.database.services import SummaryStorageService
from typedb.driver import Driver, TransactionType


@pytest.mark.asyncio
async def test_save_summary(
    summary_storage_service: SummaryStorageService, db_connection: Driver
) -> None:
    """测试保存总结文档的功能."""
    service = summary_storage_service
    db_name = service.conn_manager.database_name
    doc = SummaryDocument(
        _key="sum1",
        conversation_uid="conv1",
        timestamp=int(time.time()),
        summary_text="这是一个测试总结",
        event_ids_covered=["e1", "e2"],
    )
    assert await service.save_summary(doc) is True
    with db_connection.transaction(db_name, TransactionType.READ) as tx:
        query = 'match $s isa summary, has summary-id "sum1", has summary-text $st; select $st;'
        answers = list(tx.query(query).resolve().as_concept_rows())
        assert len(answers) == 1
        assert answers[0].get("st").as_attribute().get_value() == "这是一个测试总结"
