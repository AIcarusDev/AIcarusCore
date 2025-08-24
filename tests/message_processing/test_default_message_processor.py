# tests/message_processing/test_default_message_processor.py

from unittest.mock import MagicMock, call

import pytest
from aicarus_protocols import ConversationInfo, Event, UserInfo
from pytest_mock import MockerFixture
from src.config import config
from src.database.models import ConversationDetails, EntityDocument
from src.message_processing.default_message_processor import DefaultMessageProcessor

# 标记整个模块的所有测试都需要异步环境
pytestmark = pytest.mark.asyncio


@pytest.fixture
def mock_entity_graph_service(mocker: MockerFixture) -> MagicMock:
    """模拟 EntityGraphService，并预设其方法的返回值."""
    mock = mocker.MagicMock()
    mock.find_or_create_profile_and_account_entity = mocker.AsyncMock(
        return_value=("profile_123", "qq_user_12345")
    )

    # 让 mock 返回一个真实的 EntityDocument 实例
    mock_entity_doc = EntityDocument(
        _key="qq_group_654321",
        entity_uid="qq_group_654321",
        entity_type="conversation",
        details=ConversationDetails(
            platform="qq", conversation_id="654321", type="group", name="一个在事件中出现的群名"
        ),
    )
    mock.get_or_create_conversation_entity = mocker.AsyncMock(return_value=mock_entity_doc)

    mock.update_presence_in_conversation = mocker.AsyncMock()

    mock.get_entity_by_key = mocker.AsyncMock()
    mock.get_entity_by_key.return_value = MagicMock(details=MagicMock(friend_remark="备注名"))

    return mock


@pytest.fixture
def message_processor(
    mocker: MockerFixture, mock_entity_graph_service: MagicMock
) -> DefaultMessageProcessor:
    """创建一个 DefaultMessageProcessor 实例，并注入所有模拟依赖."""
    return DefaultMessageProcessor(
        event_service=mocker.MagicMock(),
        entity_service=mock_entity_graph_service,
        action_log_service=mocker.MagicMock(),
        image_analysis_service=mocker.MagicMock(),
        semantic_model=mocker.MagicMock(),
        interruption_broker=mocker.MagicMock(),
        narrative_vectorizer=mocker.MagicMock(),
    )


async def test_process_event_updates_conversation_name(
    message_processor: DefaultMessageProcessor,
    mock_entity_graph_service: MagicMock,
) -> None:
    """测试: 当处理一个带有群名的事件时,应调用 update_presence_in_conversation 但不再传递群名."""
    # 1. 准备 (Arrange)
    group_name = "一个在事件中出现的群名"
    test_event = Event(
        event_id="test-event-with-name",
        event_type="message.qq.group",
        time=1234567890,
        bot_id="bot-999",
        user_info=UserInfo(user_id="user_12345", user_nickname="测试用户"),
        conversation_info=ConversationInfo(conversation_id="654321", type="group", name=group_name),
        content=[],
    )

    # 2. 执行 (Act)
    await message_processor._associate_person_and_update_membership(
        event=test_event, platform_id="qq"
    )

    # 3. 断言 (Assert)
    # 移除 call 中的 conversation_name 参数
    sender_call = call(
        account_entity_uid="qq_user_12345",
        conversation_entity_uid="qq_group_654321",
        user_info=test_event.user_info,
    )

    bot_call = call(
        account_entity_uid="qq_bot-999",
        conversation_entity_uid="qq_group_654321",
        user_info=UserInfo(user_id="bot-999", user_nickname=config.persona.bot_name),
    )

    mock_entity_graph_service.update_presence_in_conversation.assert_has_calls(
        [sender_call, bot_call], any_order=True
    )


async def test_private_chat_event_does_not_cause_double_name_update(
    message_processor: DefaultMessageProcessor,
    mock_entity_graph_service: MagicMock,
) -> None:
    """测试场景.

    处理一个私聊事件时，会话名称应该只被
    get_or_create_conversation_entity 设置一次，而后续的
    update_presence_in_conversation 调用不应再修改它。
    """
    # 1. 准备 (Arrange)
    sender_nickname = "未來星織"
    test_event = Event(
        event_id="private-chat-event",
        event_type="message.qq.private.friend",
        time=1234567890,
        bot_id="99999",
        user_info=UserInfo(user_id="1321807442", user_nickname=sender_nickname),
        conversation_info=ConversationInfo(
            conversation_id="1321807442",
            type="private",
            name=sender_nickname,  # 事件中自带的名称
        ),
        content=[],
    )

    # 2. 执行 (Act)
    # 调用被测试的核心方法
    await message_processor._associate_person_and_update_membership(
        event=test_event, platform_id="qq"
    )

    # 3. 断言 (Assert)
    # 验证 get_or_create_conversation_entity 被调用，并且传入了正确的名称
    mock_entity_graph_service.get_or_create_conversation_entity.assert_awaited_once_with(
        conversation_id="1321807442", platform="qq", conv_type="private", name=sender_nickname
    )

    # 验证 update_presence_in_conversation 被调用了两次 (一次为发送者，一次为机器人)
    assert mock_entity_graph_service.update_presence_in_conversation.await_count == 2

    # 核心验证：检查所有对 update_presence_in_conversation 的调用，
    # 确保它们的关键字参数中【没有】'conversation_name'。
    # 这证明了我们已经将更新会话名称的职责从这个函数中移除了。
    for call_args in mock_entity_graph_service.update_presence_in_conversation.call_args_list:
        kwargs = call_args.kwargs
        assert "conversation_name" not in kwargs, (
            f"不应在 update_presence_in_conversation 中传递 conversation_name，但却传入了: {kwargs}"
        )
