# tests/core_logic/test_prompt_builder.py

from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_mock import MockerFixture

# --- [FIX START] ---
# 导入 platform_builders 包和全局的 platform_builder_registry 实例
from src import platform_builders
from src.database.models import ConversationDetails, EntityDocument
from src.focus_chat_mode.components import PromptComponents
from src.platform_builders.registry import platform_builder_registry
from src.prompt_builder import PromptBuilderError, ThoughtPromptBuilder


# 添加一个模块级别的、自动执行的 Fixture 来初始化注册中心
# 这将确保在本文件中所有测试运行之前，'core' builder 和其他 builder 都已被注册
@pytest.fixture(scope="module", autouse=True)
def setup_registry_fixture() -> None:
    """This fixture runs once per module, ensuring that the platform builder registry is populated.

    This runs before any tests in this file and is crucial for any test that indirectly relies on
    platform builders (like 'core').
    """
    if not platform_builder_registry.get_all_builders():
        print("\n--- Initializing Platform Builder Registry for test_prompt_builder.py ---")
        platform_builder_registry.discover_and_register_builders(platform_builders)


# --- [FIX END] ---


# --- Fixtures from original test_prompt_builder.py ---


@pytest.fixture
def mock_chat_session_manager(mocker: MockerFixture) -> MagicMock:
    """模拟 ChatSessionManager，它负责管理所有活跃的会话."""
    mock = mocker.MagicMock()
    # 模拟 sessions 字典，测试用例将在这里填充模拟的会话
    mock.sessions = {}
    return mock


@pytest.fixture
def mock_entity_graph_service(mocker: MockerFixture) -> MagicMock:
    """模拟 EntityGraphService，它负责从数据库获取实体信息."""
    mock = mocker.MagicMock()
    # 我们将模拟 get_entity_by_key 方法，因为它在获取临时会话的源群聊名称时被调用
    mock.get_entity_by_key = mocker.AsyncMock()
    return mock


@pytest.fixture
def prompt_builder(
    mocker: MockerFixture,
    mock_chat_session_manager: MagicMock,
    mock_entity_graph_service: MagicMock,
) -> ThoughtPromptBuilder:
    """创建一个 ThoughtPromptBuilder 实例，并注入所有必要的模拟依赖."""
    # 其他依赖项对于这个特定测试不重要，所以也用 MagicMock 简单模拟

    # 当模拟 core_ws_server 时，必须将其中的 async 方法明确指定为 AsyncMock
    mock_ws_server = mocker.MagicMock()
    mock_ws_server.get_connected_platforms_info = mocker.AsyncMock(
        return_value="mocked platforms info"
    )

    builder = ThoughtPromptBuilder(
        unread_info_service=mocker.MagicMock(),
        internal_info_builder=mocker.MagicMock(),
        event_storage_service=mocker.MagicMock(),
        thought_storage_service=mocker.MagicMock(),
        entity_graph_service=mock_entity_graph_service,
        action_handler=mocker.MagicMock(),
        state_manager=mocker.MagicMock(),
        # 初始时传入 manager，以模拟真实场景
        chat_session_manager=mock_chat_session_manager,
        core_ws_server=mock_ws_server,  # 使用我们配置好的 mock
    )
    return builder


# --- [新增测试类] ---
class TestWorkingMemoriesAndTrajectory:
    """测试与新的工作记忆系统和被废除的注意力轨迹相关的逻辑."""

    async def test_build_working_memories_block_formats_correctly(
        self, prompt_builder: ThoughtPromptBuilder, mocker: MockerFixture
    ) -> None:
        """测试 _build_working_memories_block 是否能根据数据库返回的思考记录正确生成 XML 块.

        验证方法能够从数据库获取思考记录并正确格式化为 XML 格式的工作记忆块。
        """
        # 1. 准备 (Arrange)
        # 模拟数据库返回两条思考记录
        mock_thoughts = [
            {
                "_key": "t1",
                "action_payload": {
                    "internal_state": {"mood": "curious", "think": "...", "intent": "..."},
                    "consciousness_control": {"focus": {"target_id": "qq"}},
                },
            },
            {
                "_key": "t2",
                "action_payload": {
                    "internal_state": {"mood": "happy", "think": "...", "intent": "..."},
                    "action": {"core": {"do_nothing": {}}},
                },
            },
        ]
        # Patch state_manager 内部的 thought_service
        prompt_builder.system_prompt_parts_builder.state_manager.thought_service.get_recent_thought_documents = mocker.AsyncMock(  # noqa: E501
            return_value=mock_thoughts
        )

        # 2. 执行 (Act)
        result = await prompt_builder.system_prompt_parts_builder._build_working_memories_block()

        # 3. 断言 (Assert)
        # 修复点：实现已经移除了外层标签，所以测试应该断言其以 <desc> 开头
        assert result.startswith("  <desc>以下是你的有印象/记得的，之前自己做的事。</desc>")
        assert result.endswith("</memory>")  # 验证它以最后一个 memory 块结束
        assert '<memory cycle_ago="1">' in result
        assert '<memory cycle_ago="2">' in result
        # 验证 internal_state 已被移除
        assert '"internal_state"' not in result
        # 验证 consciousness_control 存在
        assert '"consciousness_control"' in result
        # 验证 action 存在
        assert '"action"' in result

    def test_attentional_trajectory_block_is_removed(
        self, prompt_builder: ThoughtPromptBuilder
    ) -> None:
        """测试 _build_attentional_trajectory_block 方法和相关调用已被彻底移除."""
        # 修复点：为测试方法注入 prompt_builder fixture

        # 验证方法本身不存在
        assert not hasattr(
            prompt_builder.system_prompt_parts_builder, "_build_attentional_trajectory_block"
        )

        # 验证 build 方法中不再有 trajectory_task
        import inspect

        build_source = inspect.getsource(prompt_builder.system_prompt_parts_builder.build)
        assert "trajectory_task" not in build_source
        assert "attentional_trajectory_block" not in build_source

    async def test_build_prompts_components_assembles_new_blocks(
        self, wired_prompt_builder: ThoughtPromptBuilder, mocker: MockerFixture
    ) -> None:
        """端到端测试：验证 build_prompts_components 返回的字典中包含新的记忆块，且不包含旧的轨迹块.

        包含新的记忆块，且不包含旧的轨迹块。
        """
        # 1. 准备 (Arrange)
        # 模拟所有子构建器的 build 方法
        mocker.patch.object(
            wired_prompt_builder.system_prompt_parts_builder,
            "_build_working_memories_block",
            new_callable=AsyncMock,
            return_value="<working_memories>...</working_memories>",
        )
        mocker.patch.object(
            wired_prompt_builder.system_prompt_parts_builder,
            "_get_deliberation_summary_block",
            return_value="<deliberation_summary>...</deliberation_summary>",
        )
        mocker.patch.object(
            wired_prompt_builder.external_info_builder,
            "build",
            new_callable=AsyncMock,
            return_value=("", "", PromptComponents(), []),
        )
        mocker.patch.object(
            wired_prompt_builder.system_prompt_parts_builder,
            "build",
            wraps=wired_prompt_builder.system_prompt_parts_builder.build,
        )
        mocker.patch.object(
            wired_prompt_builder.system_prompt_parts_builder, "internal_info_builder"
        )
        wired_prompt_builder.system_prompt_parts_builder.internal_info_builder.build_internal_info_block = mocker.AsyncMock()  # noqa: E501

        # --- [FIX for Failure] ---
        # 核心修复：为 UserPromptPartsBuilder 的依赖 thought_storage 配置返回值
        # 这样 _get_latest_action_context 就不会返回协程
        wired_prompt_builder.user_prompt_parts_builder.thought_storage.get_latest_thought_document.return_value = None  # noqa: E501
        # --- [FIX END] ---

        # 2. 执行 (Act)
        components, _ = await wired_prompt_builder.build_prompts_components(
            level="core", focus_path="core", session=None
        )

        # 3. 断言 (Assert)
        system_blocks = components.system_prompt_blocks
        assert "working_memories_block" in system_blocks
        assert "deliberation_summary_block" in system_blocks
        assert "attentional_trajectory_block" not in system_blocks
        assert system_blocks["working_memories_block"] == "<working_memories>...</working_memories>"
        assert (
            system_blocks["deliberation_summary_block"]
            == "<deliberation_summary>...</deliberation_summary>"
        )


# --- [以下为原有测试，保持不变] ---


@pytest.fixture
def mock_dependencies(mocker: MockerFixture) -> dict:
    """一个集中的 Fixture，用于模拟 ThoughtPromptBuilder 的所有依赖项."""
    mock_ws_server = mocker.MagicMock()
    mock_ws_server.get_connected_platforms_info = mocker.AsyncMock(
        return_value="mocked platforms from deps"
    )

    return {
        "unread_info_service": mocker.AsyncMock(),
        "internal_info_builder": mocker.AsyncMock(),
        "event_storage_service": mocker.AsyncMock(),
        "thought_storage_service": mocker.AsyncMock(),
        "entity_graph_service": mocker.AsyncMock(),
        "action_handler": mocker.MagicMock(),
        "state_manager": mocker.MagicMock(),
        "chat_session_manager": mocker.MagicMock(),
        "core_ws_server": mock_ws_server,  # 使用正确配置的 mock
    }


@pytest.fixture
def wired_prompt_builder(mock_dependencies: dict) -> ThoughtPromptBuilder:
    """创建一个 ThoughtPromptBuilder 实例，并模拟“后期绑定/注入”的过程."""
    deps_for_init = mock_dependencies.copy()
    deps_for_init["chat_session_manager"] = None
    deps_for_init["core_ws_server"] = None

    builder = ThoughtPromptBuilder(**deps_for_init)

    # 模拟后续的依赖注入过程, 现在 setter 会自动处理子构建器
    builder.chat_session_manager = mock_dependencies["chat_session_manager"]
    builder.core_ws_server = mock_dependencies["core_ws_server"]

    return builder


class TestPromptBuilderCurrentState:
    """专门测试 `_get_current_state_block` 方法的测试类."""

    async def test_get_current_state_group_chat_with_name(
        self,
        prompt_builder: ThoughtPromptBuilder,
        mock_chat_session_manager: MagicMock,
        mocker: MockerFixture,
    ) -> None:
        """测试场景：当在一个有名称的群聊中时，应正确显示群聊名称."""
        # 1. 准备 (Arrange)
        mock_session = MagicMock()
        mock_session.conversation_name = "AIcarus 核心开发群"
        mock_session.conversation_type = "group"
        mock_session.membership_status = "active"
        # get_bot_profile 是一个 async 方法，需要用 AsyncMock 模模拟
        mock_session.get_bot_profile = mocker.AsyncMock(return_value={"card": "测试机器人"})

        mock_chat_session_manager.sessions = {"qq_group_12345": mock_session}

        # 2. 执行 (Act)
        result = await prompt_builder.system_prompt_parts_builder._get_current_state_block(
            level="cellular", platform_id="qq", conv_id="group.12345"
        )

        # 3. 断言 (Assert)
        assert '你当前正在 qq 群"AIcarus 核心开发群"中参与 qq 群聊' in result
        assert '你在该群的群名片是"测试机器人"' in result

    async def test_get_current_state_group_chat_without_name(
        self,
        prompt_builder: ThoughtPromptBuilder,
        mock_chat_session_manager: MagicMock,
        mocker: MockerFixture,
    ) -> None:
        """测试场景：当群聊名称为 None 时，应使用 "未知群聊" 作为回退."""
        # 1. 准备 (Arrange)
        mock_session = MagicMock()
        mock_session.conversation_name = None
        mock_session.conversation_type = "group"
        mock_session.membership_status = "active"
        mock_session.get_bot_profile = mocker.AsyncMock(return_value={"card": "测试机器人"})
        mock_chat_session_manager.sessions = {"qq_group_12345": mock_session}

        # 2. 执行 (Act)
        result = await prompt_builder.system_prompt_parts_builder._get_current_state_block(
            level="cellular", platform_id="qq", conv_id="group.12345"
        )

        # 3. 断言 (Assert)
        assert '你当前正在 qq 群"未知群聊"中参与 qq 群聊，' in result
        assert '你在该群的群名片是"测试机器人"' in result

    async def test_get_current_state_group_chat_without_card(
        self,
        prompt_builder: ThoughtPromptBuilder,
        mock_chat_session_manager: MagicMock,
        mocker: MockerFixture,
    ) -> None:
        """测试场景：当机器人在群聊中没有群名片时，不应显示群名片部分."""
        # 1. 准备 (Arrange)
        mock_session = MagicMock()
        mock_session.conversation_name = "AIcarus 核心开发群"
        mock_session.conversation_type = "group"
        mock_session.membership_status = "active"
        # 模拟没有群名片的情况
        mock_session.get_bot_profile = mocker.AsyncMock(return_value={"card": None})
        mock_chat_session_manager.sessions = {"qq_group_12345": mock_session}

        # 2. 执行 (Act)
        result = await prompt_builder.system_prompt_parts_builder._get_current_state_block(
            level="cellular", platform_id="qq", conv_id="group.12345"
        )

        # 3. 断言 (Assert)
        assert '你当前正在 qq 群"AIcarus 核心开发群"中参与 qq 群聊。' in result
        assert "你在该群的群名片是" not in result

    async def test_get_current_state_group_chat_with_default_name_card(
        self,
        prompt_builder: ThoughtPromptBuilder,
        mock_chat_session_manager: MagicMock,
        mocker: MockerFixture,
    ) -> None:
        """测试场景：当群名片等于机器人的昵称时，不应显示群名片部分."""
        # 1. 准备 (Arrange)
        mock_session = MagicMock()
        mock_session.conversation_name = "AIcarus 核心开发群"
        mock_session.conversation_type = "group"
        mock_session.membership_status = "active"
        # 模拟群名片等于昵称的情况（说明没有特殊设置群名片）
        mock_session.get_bot_profile = mocker.AsyncMock(
            return_value={"card": "AIcarus", "nickname": "AIcarus"}
        )
        mock_chat_session_manager.sessions = {"qq_group_12345": mock_session}

        # 2. 执行 (Act)
        result = await prompt_builder.system_prompt_parts_builder._get_current_state_block(
            level="cellular", platform_id="qq", conv_id="group.12345"
        )

        # 3. 断言 (Assert)
        assert '你当前正在 qq 群"AIcarus 核心开发群"中参与 qq 群聊。' in result
        assert "你在该群的群名片是" not in result

    async def test_get_current_state_temporary_chat_from_known_group(
        self,
        prompt_builder: ThoughtPromptBuilder,
        mock_chat_session_manager: MagicMock,
        mock_entity_graph_service: MagicMock,
        mocker: MockerFixture,
    ) -> None:
        """测试场景：当在一个来自已知群聊的临时会话中，应正确显示源群聊的名称."""
        # 1. 准备 (Arrange)
        mock_session = MagicMock()
        mock_session.conversation_name = "张三"
        mock_session.conversation_type = "private"
        mock_session.membership_status = "active"
        mock_session.platform = "qq"
        mock_session.conversation_info.extra = {
            "is_temporary": True,
            "source_group_id": "group-abc",
        }
        mock_session.get_bot_profile = mocker.AsyncMock(
            return_value={"user_id": "bot_id", "nickname": "AIcarus"}
        )
        mock_chat_session_manager.sessions = {"qq_private_67890": mock_session}

        # 模拟数据库返回的源群聊实体
        mock_group_entity = EntityDocument(
            _key="qq_group_group-abc",
            entity_uid="qq_group_group-abc",
            entity_type="conversation",
            details=ConversationDetails(
                platform="qq",
                conversation_id="group-abc",
                type="group",
                name="源群聊-聊天室",
            ),
        )
        mock_entity_graph_service.get_entity_by_key.return_value = mock_group_entity

        # 2. 执行 (Act)
        result = await prompt_builder.system_prompt_parts_builder._get_current_state_block(
            level="cellular", platform_id="qq", conv_id="private.67890"
        )

        # 3. 断言 (Assert)
        assert "处理来自“源群聊-聊天室”群聊中“张三”的临时会话私聊" in result
        mock_entity_graph_service.get_entity_by_key.assert_awaited_once_with("qq_group_group-abc")

    async def test_get_current_state_exited_group(
        self,
        prompt_builder: ThoughtPromptBuilder,
        mock_chat_session_manager: MagicMock,
        mocker: MockerFixture,
    ) -> None:
        """测试场景：当观察一个已退出的群聊时，应显示正确的状态描述."""
        # 1. 准备 (Arrange)
        mock_session = MagicMock()
        mock_session.conversation_name = "一个已经退出的群"
        mock_session.conversation_type = "group"
        mock_session.membership_status = "left"
        mock_session.get_bot_profile = mocker.AsyncMock(return_value={})
        mock_chat_session_manager.sessions = {"qq_group_54321": mock_session}

        # 2. 执行 (Act)
        result = await prompt_builder.system_prompt_parts_builder._get_current_state_block(
            level="cellular", platform_id="qq", conv_id="group.54321"
        )

        # 3. 断言 (Assert)
        assert '你当前正在观察一个你【已退出】的QQ群 "一个已经退出的群"' in result
        assert "你无法在此发送消息" in result

    async def test_get_current_state_session_not_found_raises_error(
        self, prompt_builder: ThoughtPromptBuilder, mock_chat_session_manager: MagicMock
    ) -> None:
        """测试场景：当在细胞层级但找不到对应的会话实例时，应抛出异常."""
        # 1. 准备 (Arrange)
        # 确保会话字典是空的
        mock_chat_session_manager.sessions = {}

        # 2. 执行 & 断言 (Act & Assert)
        with pytest.raises(PromptBuilderError) as excinfo:
            await prompt_builder.system_prompt_parts_builder._get_current_state_block(
                level="cellular", platform_id="qq", conv_id="group.nonexistent"
            )

        # 验证异常信息是否符合预期
        assert "找不到会话实体UID为 'qq_group_nonexistent' 的活跃会话档案" in str(excinfo.value)


# --- Fixtures and Tests from test_prompt_builder_2.py / test_prompt_builder_3.py ---


class TestPromptBuilderInstantiationAndWiring:
    """测试 ThoughtPromptBuilder 的实例化和依赖注入逻辑."""

    def test_thought_prompt_builder_instantiation_with_none(self, mock_dependencies: dict) -> None:
        """测试核心修复：验证 ThoughtPromptBuilder 可以在 chat_session_manager
        和 core_ws_server 为 None 的情况下被成功实例化.
        """  # noqa: D205
        try:
            deps_for_test = mock_dependencies.copy()
            deps_for_test["chat_session_manager"] = None
            deps_for_test["core_ws_server"] = None

            builder = ThoughtPromptBuilder(**deps_for_test)

            # 验证实例自身的属性被正确设置为 None
            assert builder.chat_session_manager is None
            assert builder.core_ws_server is None
            # 验证子构建器的属性也被正确设置为 None
            assert builder.schema_builder.chat_session_manager is None
            assert builder.external_info_builder.chat_session_manager is None
        except (ValueError, TypeError) as e:
            pytest.fail(f"ThoughtPromptBuilder instantiation failed unexpectedly: {e}")

    async def test_build_prompts_components_after_wiring(
        self, wired_prompt_builder: ThoughtPromptBuilder, mocker: MockerFixture
    ) -> None:
        """测试功能性：在依赖被注入后，build_prompts_components 方法应该能成功执行."""
        # 准备：模拟子构建器和 chat_session_manager 的行为
        mocker.patch.object(
            wired_prompt_builder.external_info_builder,
            "build",
            new_callable=AsyncMock,
            return_value=("external_info", "meta_info", PromptComponents(), []),
        )
        mocker.patch.object(
            wired_prompt_builder.system_prompt_parts_builder,
            "build",
            new_callable=AsyncMock,
            return_value={"system_key": "system_value"},
        )
        mocker.patch.object(
            wired_prompt_builder.user_prompt_parts_builder,
            "build",
            new_callable=AsyncMock,
            return_value={"user_key": "user_value"},
        )
        mocker.patch.object(
            wired_prompt_builder.schema_builder,
            "build_response_schema",
            return_value={"schema_key": "schema_value"},
        )

        # 模拟 chat_session_manager 的 focus_manager 属性，使其可以被访问
        wired_prompt_builder.chat_session_manager.focus_manager.focus_history = [
            1,
            2,
        ]  # 模拟历史记录大于1

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
