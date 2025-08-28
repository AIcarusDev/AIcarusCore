# tests/core_logic/test_thought_persistor.py

import pytest
from pytest_mock import MockerFixture
from src.mind.thought_persistor import ThoughtPersistor
from src.services.database.models import ThoughtChainDocument

# 标记整个模块的所有测试都需要异步环境
pytestmark = pytest.mark.asyncio


@pytest.fixture
def mock_thought_storage(mocker: MockerFixture) -> MockerFixture:
    """模拟 ThoughtStorageService."""
    mock = mocker.AsyncMock()
    # 让 save_thought_and_link 返回一个固定的 key，表示成功
    mock.save_thought_and_link.return_value = "saved_key_123"
    return mock


@pytest.fixture
def thought_persistor(mock_thought_storage: MockerFixture) -> ThoughtPersistor:
    """创建 ThoughtPersistor 实例并注入模拟的存储服务."""
    return ThoughtPersistor(thought_storage=mock_thought_storage)


async def test_store_thought_creates_correct_document(
    thought_persistor: ThoughtPersistor, mock_thought_storage: MockerFixture
) -> None:
    """测试 store_thought 是否能正确地将 JSON 转换为 ThoughtChainDocument."""
    # 准备
    thought_json = {
        "internal_state": {
            "mood": "愉快",
            "think": "这是一个测试想法。",
            "intent": "验证持久化逻辑",
        },
        "action": {"core": {"do_nothing": {"motivation": "测试中"}}},
    }

    # 执行
    saved_key, new_pearl = await thought_persistor.store_thought(
        thought_json=thought_json, source_type="test_source", source_id="test_id_1"
    )

    # 断言
    assert saved_key == "saved_key_123"
    assert new_pearl is not None

    # 验证 mock 的 save_thought_and_link 方法被调用
    mock_thought_storage.save_thought_and_link.assert_awaited_once()

    # 验证传递给 mock 的参数 (即打包好的 ThoughtChainDocument) 是否正确
    call_args = mock_thought_storage.save_thought_and_link.call_args
    passed_document = call_args.args[0]

    assert isinstance(passed_document, ThoughtChainDocument)
    assert passed_document.mood == "愉快"
    assert passed_document.think == "这是一个测试想法。"
    assert passed_document.intent == "验证持久化逻辑"
    assert passed_document.source_type == "test_source"
    assert passed_document.source_id == "test_id_1"
    # 验证 action_payload 存储了完整的原始 JSON
    assert passed_document.action_payload == thought_json
    # 验证 action_id 被正确生成
    assert passed_document.action_id is not None
