# tests/message_processing/test_default_message_processor.py

import pytest
from unittest.mock import MagicMock, call
from pytest_mock import MockerFixture

from aicarus_protocols import ConversationInfo, Event, UserInfo
from src.message_processing.default_message_processor import DefaultMessageProcessor
from src.config import config
# --- [FIX START] ---
# 导入我们需要的真实数据模型，以便创建更逼真的模拟对象
from src.database.models import ConversationDetails, EntityDocument
# --- [FIX END] ---

# 标记整个模块的所有测试都需要异步环境
pytestmark = pytest.mark.asyncio


@pytest.fixture
def mock_entity_graph_service(mocker: MockerFixture) -> MagicMock:
    """模拟 EntityGraphService，并预设其方法的返回值。"""
    mock = mocker.MagicMock()
    mock.find_or_create_profile_and_account_entity = mocker.AsyncMock(
        return_value=("profile_123", "qq_user_12345")
    )
    
    # --- [FIX START] ---
    # 修复点：让 mock 返回一个真实的 EntityDocument 实例，而不是一个 dict
    mock_entity_doc = EntityDocument(
        _key="qq_group_654321",
        entity_uid="qq_group_654321",
        entity_type="conversation",
        details=ConversationDetails(
            platform="qq",
            conversation_id="654321",
            type="group",
            name="一个在事件中出现的群名"
        )
    )
    mock.get_or_create_conversation_entity = mocker.AsyncMock(
        return_value=mock_entity_doc
    )
    # --- [FIX END] ---

    mock.update_presence_in_conversation = mocker.AsyncMock()
    
    mock.get_entity_by_key = mocker.AsyncMock()
    mock.get_entity_by_key.return_value = MagicMock(details=MagicMock(friend_remark="备注名"))
    
    return mock


@pytest.fixture
def message_processor(
    mocker: MockerFixture, mock_entity_graph_service: MagicMock
) -> DefaultMessageProcessor:
    """创建一个 DefaultMessageProcessor 实例，并注入所有模拟依赖。"""
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
):
    """
    测试场景 (最终验证): 当处理一个带有群名的事件时，
    应调用 update_presence_in_conversation 并正确传递群名。
    """
    # 1. 准备 (Arrange)
    group_name = "一个在事件中出现的群名"
    test_event = Event(
        event_id="test-event-with-name",
        event_type="message.qq.group",
        time=1234567890,
        bot_id="bot-999",
        user_info=UserInfo(user_id="user_12345", user_nickname="测试用户"),
        conversation_info=ConversationInfo(
            conversation_id="654321",
            type="group",
            name=group_name
        ),
        content=[]
    )

    # 2. 执行 (Act)
    await message_processor._handle_event_persistence(
        event=test_event, platform_id="qq", needs_persistence=False
    )

    # 3. 断言 (Assert)
    sender_call = call(
        account_entity_uid="qq_user_12345",
        conversation_entity_uid="qq_group_654321",
        user_info=test_event.user_info,
        conversation_name=group_name
    )
    
    bot_call = call(
        account_entity_uid="qq_bot-999",
        conversation_entity_uid="qq_group_654321",
        user_info=UserInfo(user_id='bot-999', user_nickname=config.persona.bot_name),
        conversation_name=group_name
    )

    mock_entity_graph_service.update_presence_in_conversation.assert_has_calls(
        [sender_call, bot_call],
        any_order=True
    )