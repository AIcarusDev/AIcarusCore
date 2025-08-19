# tests/core_logic/test_decision_dispatcher.py (已修复并包含初始化 Fixture)

import pytest
from pytest_mock import MockerFixture
from src import platform_builders  # 导入 platform_builders 包
from src.core_logic.decision_dispatcher import normalize_action_payload, process_llm_decision
from src.platform_builders.registry import platform_builder_registry


# --- 关键修复：添加 Module 级别的 Fixture 来初始化注册中心 ---
@pytest.fixture(scope="module", autouse=True)
def setup_registry_fixture() -> None:
    """这个 Fixture 会在当前测试模块的所有测试运行前自动执行一次.

    它的作用是发现并注册所有的 Platform Builders，确保
    normalize_action_payload 函数可以正确工作。
    """
    if not platform_builder_registry.get_all_builders():
        print("\n--- Initializing Platform Builder Registry for tests ---")
        platform_builder_registry.discover_and_register_builders(platform_builders)


# ----------------------------------------------------------------


@pytest.fixture
def mock_dispatcher_dependencies(mocker: MockerFixture) -> dict:
    """为 process_llm_decision 模拟所有依赖."""
    return {
        "focus_manager": mocker.AsyncMock(),
        "action_handler": mocker.AsyncMock(),
        "core_logic": mocker.MagicMock(),
        "session": mocker.MagicMock(),
    }


# --- 单元测试现在会因为 setup_registry_fixture 的存在而正确运行 ---


def test_normalize_action_payload_core_action() -> None:
    """测试扁平的核心动作能被正确规范化."""
    payload = {"web_search": {"query": "test"}}
    normalized = normalize_action_payload(payload, "qq")
    assert normalized == {"core": {"web_search": {"query": "test"}}}


def test_normalize_action_payload_platform_action() -> None:
    """测试扁平的平台动作能被正确规范化."""
    # 假设 send_message 不是核心动作
    payload = {"send_message": {"content": "hello"}}
    normalized = normalize_action_payload(payload, "qq")
    assert normalized == {"qq": {"send_message": {"content": "hello"}}}


def test_normalize_action_payload_already_normalized() -> None:
    """测试已经规范的 payload 不会被改变."""
    payload = {"qq": {"send_message": {"content": "hello"}}}
    normalized = normalize_action_payload(payload, "qq")
    assert normalized == payload


# --- 集成风格测试 ---


async def test_dispatch_only_action(mock_dispatcher_dependencies: dict) -> None:
    """测试当决策只有 action 时，只调用 ActionHandler."""
    # 准备
    decision = {
        "internal_state": {"mood": "acting", "think": "...", "intent": "do stuff"},
        "action": {"core": {"web_search": {"query": "test"}}},
    }

    # 执行
    await process_llm_decision(decision, **mock_dispatcher_dependencies)

    # 断言
    mock_dispatcher_dependencies["action_handler"].process_action_flow.assert_awaited_once()
    mock_dispatcher_dependencies["focus_manager"].handle_consciousness_control.assert_not_called()


async def test_dispatch_only_consciousness_control(mock_dispatcher_dependencies: dict) -> None:
    """测试当决策只有 consciousness_control 时，只调用 FocusManager."""
    # 准备
    decision = {
        "internal_state": {"mood": "moving", "think": "...", "intent": "change focus"},
        "consciousness_control": {"focus": {"target_id": "qq"}},
    }

    # 执行
    await process_llm_decision(decision, **mock_dispatcher_dependencies)

    # 断言
    mock_dispatcher_dependencies["action_handler"].process_action_flow.assert_not_called()
    mock_dispatcher_dependencies["focus_manager"].handle_consciousness_control.assert_awaited_once()


async def test_dispatch_both_action_and_control(mock_dispatcher_dependencies: dict) -> None:
    """测试当决策同时包含 action 和 control 时，两者都被调用."""
    # 准备
    decision = {
        "internal_state": {"mood": "busy", "think": "...", "intent": "do and move"},
        "action": {"core": {"web_search": {"query": "test"}}},
        "consciousness_control": {"focus": {"target_id": "qq"}},
    }

    # 执行
    await process_llm_decision(decision, **mock_dispatcher_dependencies)

    # 断言
    mock_dispatcher_dependencies["action_handler"].process_action_flow.assert_awaited_once()
    mock_dispatcher_dependencies["focus_manager"].handle_consciousness_control.assert_awaited_once()


async def test_dispatch_special_send_message_action(
    mocker: MockerFixture, mock_dispatcher_dependencies: dict
) -> None:
    """测试 send_message 动作会走特殊的处理路径 (调用 MessageBuilder)."""
    # 准备
    decision = {
        "internal_state": {"mood": "chatting", "think": "...", "intent": "reply"},
        "action": {
            "qq": {"send_message": {"steps": [{"command": "text", "params": {"content": "Hi"}}]}}
        },
    }

    # Mock MessageBuilder
    mock_message_builder_instance = mocker.AsyncMock()
    mock_message_builder_instance.process_steps.return_value = True  # 模拟消息发送成功
    mocker.patch(
        "src.core_logic.decision_dispatcher.MessageBuilder",
        return_value=mock_message_builder_instance,
    )

    # 关键修复：需要为 mock_dispatcher_dependencies 提供 current_focus_path
    # 否则在 _handle_external_action 中 parse_focus_path 会失败
    mock_dispatcher_dependencies["current_focus_path"] = "qq.group.12345"

    # 执行
    await process_llm_decision(decision, **mock_dispatcher_dependencies)

    # 断言
    # 验证没有调用通用的 action_handler.process_action_flow
    mock_dispatcher_dependencies["action_handler"].process_action_flow.assert_not_called()
    # 验证 MessageBuilder 的 process_steps 被调用
    mock_message_builder_instance.process_steps.assert_awaited_once()
    # 验证成功发送消息后，触发了下一轮思考
    mock_dispatcher_dependencies["core_logic"].trigger_immediate_thought_cycle.assert_called_once()
