import time

import pytest
from src.services.database.services import EntityGraphService, EventStorageService
from typedb.driver import Driver, TransactionType


@pytest.mark.asyncio
async def test_save_and_find_event(
    event_storage_service: EventStorageService,
    entity_graph_service: EntityGraphService,
    db_connection: Driver,
) -> None:
    """Test saving an event document and finding it by image hash.

    Parameters
    ----------
    event_storage_service : EventStorageService
        The event storage service instance for testing.
    entity_graph_service : EntityGraphService
        The entity graph service instance for testing.
    db_connection : Driver
        The database connection driver for testing.
    """
    service, db_name = event_storage_service, event_storage_service.conn_manager.database_name
    await entity_graph_service.get_or_create_platform_entity("qq", "QQ")
    image_hash = "unique_hash_12345"
    event_doc = {
        "_key": "test_event_123",
        "event_id": "test_event_123",
        "event_type": "message.qq.group",
        "timestamp": int(time.time() * 1000),
        "platform": "qq",
        "bot_id": "bot1",
        "status": "unread",
        "content": [{"type": "image", "data": {"hash": image_hash}}],
        "user_info": {"user_id": "user1"},
        "conversation_info": {"conversation_id": "conv1"},
    }
    assert await service.save_event_document(event_doc) is True
    with db_connection.transaction(db_name, TransactionType.READ) as tx:
        query = 'match $e isa event, has event-id "test_event_123";'
        assert len(list(tx.query(query).resolve())) == 1
    found_event = await service.find_event_by_image_hash(image_hash)
    assert found_event is not None and found_event["_key"] == "test_event_123"
    assert await service.update_events_status(["test_event_123"], "read") is True
    with db_connection.transaction(db_name, TransactionType.READ) as tx:
        query = 'match $e isa event, has event-id "test_event_123", has status $s; select $s;'
        answers = list(tx.query(query).resolve().as_concept_rows())
        assert len(answers) == 1 and answers[0].get("s").as_attribute().get_value() == "read"
