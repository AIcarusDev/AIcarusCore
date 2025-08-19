# tests/core_logic/test_prompt_builder.py

import asyncio
from unittest.mock import MagicMock

import pytest
from pytest_mock import MockerFixture
from src.database.models import ConversationDetails, EntityDocument
from src.prompt_builder import PromptBuilderError, ThoughtPromptBuilder

# 标记整个模块的所有测试都需要异步环境
pytestmark = pytest.mark.asyncio


@pytest.fixture
def mock_chat_session_manager(mocker: MockerFixture) -> MagicMock:
    """模拟 ChatSessionManager，它负责管理所有活跃的会话。"""
    mock = mocker.MagicMock()
    # 模拟 sessions 字典，测试用例将在这里填充模拟的会话
    mock.sessions = {}
    return mock


@pytest.fixture
def mock_entity_graph_service(mocker: MockerFixture) -> MagicMock:
    """模拟 EntityGraphService，它负责从数据库获取实体信息。"""
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
    """创建一个 ThoughtPromptBuilder 实例，并注入所有必要的模拟依赖。"""
    # 其他依赖项对于这个特定测试不重要，所以也用 MagicMock 简单模拟
    return ThoughtPromptBuilder(
        unread_info_service=mocker.MagicMock(),
        internal_info_builder=mocker.MagicMock(),
        event_storage_service=mocker.MagicMock(),
        thought_storage_service=mocker.MagicMock(),
        entity_graph_service=mock_entity_graph_service,
        action_handler=mocker.MagicMock(),
        state_manager=mocker.MagicMock(),
        chat_session_manager=mock_chat_session_manager,
        core_ws_server=mocker.MagicMock(),
    )


class TestPromptBuilderCurrentState:
    """专门测试 `_get_current_state_block` 方法的测试类。"""

    async def test_get_current_state_group_chat_with_name(
        self, prompt_builder: ThoughtPromptBuilder, mock_chat_session_manager: MagicMock
    ):
        """测试场景：当在一个有名称的群聊中时，应正确显示群聊名称。"""
        # 1. 准备 (Arrange)
        mock_session = MagicMock()
        mock_session.conversation_name = "AIcarus 核心开发群"
        mock_session.conversation_type = "group"
        mock_session.membership_status = "active"
        # get_bot_profile 是一个 async 方法，需要用 AsyncMock 模拟
        mock_session.get_bot_profile = MagicMock(return_value=asyncio.Future())
        mock_session.get_bot_profile.return_value.set_result({"card": "测试机器人"})

        mock_chat_session_manager.sessions = {"qq_group_12345": mock_session}

        # 2. 执行 (Act)
        result = await prompt_builder.system_prompt_parts_builder._get_current_state_block(
            level="cellular", platform_id="qq", conv_id="group.12345"
        )

        # 3. 断言 (Assert)
        assert '你当前正在 qq 群"AIcarus 核心开发群"中参与 qq 群聊' in result
        assert '你在该群的群名片是"测试机器人"' in result

    async def test_get_current_state_group_chat_without_name(
        self, prompt_builder: ThoughtPromptBuilder, mock_chat_session_manager: MagicMock
    ):
        """测试场景：当群聊名称为 None 时，应使用 "未知群聊" 作为回退。"""
        # 1. 准备 (Arrange)
        mock_session = MagicMock()
        mock_session.conversation_name = None  # 关键测试点
        mock_session.conversation_type = "group"
        mock_session.membership_status = "active"
        mock_session.get_bot_profile = MagicMock(return_value=asyncio.Future())
        mock_session.get_bot_profile.return_value.set_result({"card": "测试机器人"})

        mock_chat_session_manager.sessions = {"qq_group_12345": mock_session}

        # 2. 执行 (Act)
        result = await prompt_builder.system_prompt_parts_builder._get_current_state_block(
            level="cellular", platform_id="qq", conv_id="group.12345"
        )

        # 3. 断言 (Assert)
        assert '你当前正在 qq 群"未知群聊"中参与 qq 群聊' in result

    async def test_get_current_state_temporary_chat_from_known_group(
        self,
        prompt_builder: ThoughtPromptBuilder,
        mock_chat_session_manager: MagicMock,
        mock_entity_graph_service: MagicMock,
    ):
        """测试场景：当在一个来自已知群聊的临时会话中，应正确显示源群聊的名称。"""
        # 1. 准备 (Arrange)
        mock_session = MagicMock()
        mock_session.conversation_name = "张三"
        mock_session.conversation_type = "private"
        mock_session.membership_status = "active"

        # --- [FIX START] ---
        # 修复点：为 mock_session 明确设置 platform 属性
        mock_session.platform = "qq"
        # --- [FIX END] ---

        mock_session.conversation_info.extra = {
            "is_temporary": True,
            "source_group_id": "group-abc",
        }

        mock_session.get_bot_profile = MagicMock(return_value=asyncio.Future())
        mock_session.get_bot_profile.return_value.set_result(
            {"user_id": "bot_id", "nickname": "AIcarus"}
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
                name="源群聊-聊天室",  # 关键测试点
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
        self, prompt_builder: ThoughtPromptBuilder, mock_chat_session_manager: MagicMock
    ):
        """测试场景：当观察一个已退出的群聊时，应显示正确的状态描述。"""
        # 1. 准备 (Arrange)
        mock_session = MagicMock()
        mock_session.conversation_name = "一个已经退出的群"
        mock_session.conversation_type = "group"
        mock_session.membership_status = "left"  # 关键测试点
        mock_session.get_bot_profile = MagicMock(return_value=asyncio.Future())
        mock_session.get_bot_profile.return_value.set_result({})  # 在已退出的群里没有群名片

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
    ):
        """测试场景：当在细胞层级但找不到对应的会话实例时，应抛出异常。"""
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
