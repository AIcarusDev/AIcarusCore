import datetime

import pytest
from src.database.models import ThoughtChainDocument
from src.database.services import ThoughtStorageService


@pytest.mark.asyncio
async def test_thought_chain_linking(thought_storage_service: ThoughtStorageService) -> None:
    """测试思想链的保存和链接功能."""
    service = thought_storage_service
    thought1 = ThoughtChainDocument(
        _key="thought_1",
        timestamp=datetime.datetime.now(datetime.UTC).isoformat(),
        mood="初始",
        think="第一个想法",
        intent="开始",
        source_type="core",
        source_id=None,
        action_id=None,
        action_payload=None,
        action_result=None,
    )
    assert await service.save_thought_and_link(thought1) == "thought_1"
    latest_thought = await service.get_latest_thought_document()
    assert latest_thought is not None and latest_thought["_key"] == "thought_1"
    thought2 = ThoughtChainDocument(
        _key="thought_2",
        timestamp=datetime.datetime.now(datetime.UTC).isoformat(),
        mood="进展",
        think="第二个想法",
        intent="继续",
        source_type="event",
        source_id=None,
        action_id=None,
        action_payload=None,
        action_result=None,
    )
    assert await service.save_thought_and_link(thought2) == "thought_2"
    latest_thought_2 = await service.get_latest_thought_document()
    assert latest_thought_2 is not None and latest_thought_2["_key"] == "thought_2"


@pytest.mark.asyncio
async def test_save_action_result_to_thought(
    thought_storage_service: ThoughtStorageService,
) -> None:
    """测试保存动作结果到思想文档."""
    service = thought_storage_service
    thought_doc = ThoughtChainDocument(
        _key="thought_for_action",
        timestamp=datetime.datetime.now(datetime.UTC).isoformat(),
        mood="行动",
        think="执行动作",
        intent="执行",
        source_type="core",
        source_id=None,
        action_id=None,
        action_payload=None,
        action_result=None,
    )
    await service.save_thought_and_link(thought_doc)
    result_text = "动作成功完成"
    assert await service.save_action_result_to_thought("thought_for_action", result_text) is True
    latest_thought = await service.get_latest_thought_document()
    assert latest_thought is not None and latest_thought["action_result"] == result_text
