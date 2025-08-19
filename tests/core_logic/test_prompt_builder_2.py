# tests/core_logic/test_prompt_builder.py


import pytest
from pytest_mock import MockerFixture
from src.focus_chat_mode.components import PromptComponents
from src.prompt_builder import ThoughtPromptBuilder

# 标记整个模块的所有测试都需要异步环境
pytestmark = pytest.mark.asyncio


@pytest.fixture
def mock_dependencies(mocker: MockerFixture) -> dict:
    """一个集中的 Fixture，用于模拟 ThoughtPromptBuilder 的所有依赖项."""
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


def test_thought_prompt_builder_instantiation_with_none(mock_dependencies: dict) -> None:
    """测试核心修复：验证 ThoughtPromptBuilder 可以在 chat_session_manager和 core_ws_server.

    为 None 的情况下被成功实例化。在修复前，这个测试会抛出 ValueError.
    """
    try:
        builder = ThoughtPromptBuilder(
            **mock_dependencies,
            chat_session_manager=None,
            core_ws_server=None,
        )
        # 验证实例的属性被正确设置为 None
        assert builder.schema_builder.chat_session_manager is None
        assert builder.external_info_builder.chat_session_manager is None
        assert builder.system_prompt_parts_builder.chat_session_manager is None
    except ValueError:
        pytest.fail(
            "ThoughtPromptBuilder failed to instantiate with None for "
            "chat_session_manager and core_ws_server. The fix is not applied."
        )


@pytest.fixture
def wired_prompt_builder(mock_dependencies: dict) -> ThoughtPromptBuilder:
    """创建一个 ThoughtPromptBuilder 实例，并模拟“后期绑定/注入”的过程."""
    # 1. 初始创建时传入 None
    builder = ThoughtPromptBuilder(
        **mock_dependencies,
        chat_session_manager=None,
        core_ws_server=None,
    )

    # 2. 模拟后续的依赖注入过程
    builder.chat_session_manager = mock_dependencies["chat_session_manager"]
    builder.core_ws_server = mock_dependencies["core_ws_server"]
    # 同样更新其子构建器中的依赖
    builder.schema_builder.chat_session_manager = mock_dependencies["chat_session_manager"]
    builder.external_info_builder.chat_session_manager = mock_dependencies["chat_session_manager"]
    builder.system_prompt_parts_builder.chat_session_manager = mock_dependencies[
        "chat_session_manager"
    ]
    builder.user_prompt_parts_builder.chat_session_manager = mock_dependencies[
        "chat_session_manager"
    ]
    builder.system_prompt_parts_builder.core_ws_server = mock_dependencies["core_ws_server"]

    return builder


async def test_build_prompts_components_after_wiring(
    wired_prompt_builder: ThoughtPromptBuilder, mock_dependencies: dict
) -> None:
    """测试功能性：在依赖被注入后，build_prompts_components 方法应该能成功执行."""
    # 准备：为子构建器的 build 方法配置返回值，以隔离测试范围
    mock_dependencies["external_info_builder"].build.return_value = (
        "external_info",
        "meta_info",
        PromptComponents(),  # 返回一个空的 PromptComponents 实例
        [],
    )
    mock_dependencies["internal_info_builder"].build_internal_info_block.return_value = (
        "internal_info"
    )
    mock_dependencies[
        "state_manager"
    ].goal_manager.get_formatted_goals.return_value = "goals"

    # 执行
    try:
        components, stimuli = await wired_prompt_builder.build_prompts_components(
            level="core", focus_path="core", session=None, handover_result=None
        )
        # 断言
        assert isinstance(components, PromptComponents)
        assert stimuli == []
        # 验证其子构建器的方法是否被调用
        mock_dependencies["external_info_builder"].build.assert_awaited_once()

    except Exception as e:
        pytest.fail(f"build_prompts_components failed after wiring dependencies: {e}")
