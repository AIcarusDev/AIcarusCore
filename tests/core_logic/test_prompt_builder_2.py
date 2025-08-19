# tests/core_logic/test_prompt_builder.py

from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_mock import MockerFixture
from src.focus_chat_mode.components import PromptComponents
from src.prompt_builder import ThoughtPromptBuilder

# 标记整个模块的所有测试都需要异步环境
pytestmark = pytest.mark.asyncio


@pytest.fixture
def mock_dependencies(mocker: MockerFixture) -> dict:
    """一个集中的 Fixture，用于模拟 ThoughtPromptBuilder 的所有依赖项。"""
    return {
        "unread_info_service": mocker.AsyncMock(),
        "internal_info_builder": mocker.AsyncMock(),
        "event_storage_service": mocker.AsyncMock(),
        "thought_storage_service": mocker.AsyncMock(),
        "entity_graph_service": mocker.AsyncMock(),
        "action_handler": mocker.MagicMock(),
        "state_manager": mocker.MagicMock(),
        "chat_session_manager": mocker.MagicMock(),
        "core_ws_server": mocker.MagicMock(),
    }


def test_thought_prompt_builder_instantiation_with_none(mock_dependencies: dict):
    """
    测试核心修复：验证 ThoughtPromptBuilder 可以在 chat_session_manager
    和 core_ws_server 为 None 的情况下被成功实例化。
    """
    try:
        deps_for_test = mock_dependencies.copy()
        deps_for_test["chat_session_manager"] = None
        deps_for_test["core_ws_server"] = None
        
        builder = ThoughtPromptBuilder(**deps_for_test)

        # 验证实例自身的属性被正确设置为 None
        assert builder.chat_session_manager is None
        # 验证子构建器的属性也被正确设置为 None
        assert builder.schema_builder.chat_session_manager is None
        assert builder.external_info_builder.chat_session_manager is None
    except (ValueError, TypeError) as e:
        pytest.fail(f"ThoughtPromptBuilder instantiation failed unexpectedly: {e}")


@pytest.fixture
def wired_prompt_builder(mock_dependencies: dict) -> ThoughtPromptBuilder:
    """
    创建一个 ThoughtPromptBuilder 实例，并模拟“后期绑定/注入”的过程。
    """
    deps_for_init = mock_dependencies.copy()
    deps_for_init["chat_session_manager"] = None
    deps_for_init["core_ws_server"] = None
    
    builder = ThoughtPromptBuilder(**deps_for_init)

    # --- [ 核心修复 ] ---
    # 模拟后续的依赖注入过程，现在需要同时更新 builder 自身和其子构建器
    builder.chat_session_manager = mock_dependencies["chat_session_manager"]
    builder.schema_builder.chat_session_manager = mock_dependencies["chat_session_manager"]
    builder.external_info_builder.chat_session_manager = mock_dependencies["chat_session_manager"]
    builder.system_prompt_parts_builder.chat_session_manager = mock_dependencies["chat_session_manager"]
    builder.user_prompt_parts_builder.chat_session_manager = mock_dependencies["chat_session_manager"]
    
    # 虽然 core_ws_server 不是本次 bug 的原因，但为了完整性，也一并注入
    builder.system_prompt_parts_builder.core_ws_server = mock_dependencies["core_ws_server"]
    builder.schema_builder.core_ws_server = mock_dependencies["core_ws_server"]
    # --- [ 修复结束 ] ---
    
    return builder


async def test_build_prompts_components_after_wiring(
    wired_prompt_builder: ThoughtPromptBuilder, mocker: MockerFixture
):
    """
    测试功能性：在依赖被注入后，build_prompts_components 方法应该能成功执行。
    """
    # 准备：模拟子构建器和 chat_session_manager 的行为
    mocker.patch.object(
        wired_prompt_builder.external_info_builder,
        'build',
        new_callable=AsyncMock,
        return_value=("external_info", "meta_info", PromptComponents(), [])
    )
    mocker.patch.object(
        wired_prompt_builder.system_prompt_parts_builder,
        'build',
        new_callable=AsyncMock,
        return_value={"system_key": "system_value"}
    )
    mocker.patch.object(
        wired_prompt_builder.user_prompt_parts_builder,
        'build',
        new_callable=AsyncMock,
        return_value={"user_key": "user_value"}
    )
    mocker.patch.object(
        wired_prompt_builder.schema_builder,
        'build_response_schema',
        return_value={"schema_key": "schema_value"}
    )
    
    # 模拟 chat_session_manager 的 focus_manager 属性，使其可以被访问
    wired_prompt_builder.chat_session_manager.focus_manager.focus_history = [1, 2] # 模拟历史记录大于1

    # 执行
    try:
        components, stimuli = await wired_prompt_builder.build_prompts_components(
            level="core", focus_path="core", session=None, handover_result=None
        )
        # 断言
        assert isinstance(components, PromptComponents)
        assert stimuli == []
        wired_prompt_builder.external_info_builder.build.assert_awaited_once()

    except Exception as e:
        pytest.fail(f"build_prompts_components failed after wiring dependencies: {e}")
