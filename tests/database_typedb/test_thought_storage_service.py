# tests/database_typedb/test_thought_storage_service.py

import datetime

import pytest
from src.services.database.models import ThoughtChainDocument
from src.services.database.services import ThoughtStorageService


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


# --- [新增测试用例] ---
@pytest.mark.asyncio
async def test_get_recent_thought_documents(thought_storage_service: ThoughtStorageService) -> None:
    """测试 get_recent_thought_documents 方法，验证其:
    1. 按时间倒序返回文档。
    2. 遵守 `limit` 参数。
    3. 遵守 `max_age_seconds` 参数.
    """  # noqa: D205
    service = thought_storage_service
    now = datetime.datetime.now(datetime.UTC)

    # 准备：插入一系列带有不同时间戳的思考记录
    thought_t_minus_1 = ThoughtChainDocument(
        _key="thought_now",
        timestamp=(now - datetime.timedelta(seconds=5)).isoformat(),
        mood="现在",
        think="...",
        intent=None,
        source_type="core",
    )
    thought_t_minus_2 = ThoughtChainDocument(
        _key="thought_30s_ago",
        timestamp=(now - datetime.timedelta(seconds=30)).isoformat(),
        mood="30秒前",
        think="...",
        intent=None,
        source_type="core",
    )
    thought_t_minus_3 = ThoughtChainDocument(
        _key="thought_90s_ago",
        timestamp=(now - datetime.timedelta(seconds=90)).isoformat(),
        mood="90秒前",
        think="...",
        intent=None,
        source_type="core",
    )
    await service.save_thought_and_link(thought_t_minus_3)
    await service.save_thought_and_link(thought_t_minus_2)
    await service.save_thought_and_link(thought_t_minus_1)

    # --- 测试 1: 获取最近的2条记录 (不限时间) ---
    recent_2 = await service.get_recent_thought_documents(limit=2, max_age_seconds=120)
    assert len(recent_2) == 2
    assert recent_2[0]["_key"] == "thought_now"
    assert recent_2[1]["_key"] == "thought_30s_ago"

    # --- 测试 2: 获取60秒内的记录 ---
    recent_60s = await service.get_recent_thought_documents(limit=5, max_age_seconds=60)
    assert len(recent_60s) == 2
    assert recent_60s[0]["_key"] == "thought_now"
    assert recent_60s[1]["_key"] == "thought_30s_ago"
    assert not any(d["_key"] == "thought_90s_ago" for d in recent_60s)

    # --- 测试 3: 获取100秒内的1条记录 ---
    recent_1_in_100s = await service.get_recent_thought_documents(limit=1, max_age_seconds=100)
    assert len(recent_1_in_100s) == 1
    assert recent_1_in_100s[0]["_key"] == "thought_now"
