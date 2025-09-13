# tests/common/narrative_vectorizer/test_narrative_vectorizer.py

import numpy as np
import pytest

# --- [核心修正] 导入 SegBuilder ---
from aicarus_protocols import ConversationInfo, Event, SegBuilder, UserInfo

# --- [结束修正] ---
from pytest_mock import MockerFixture
from src.common.narrative_vectorizer.narrative_vectorizer import NarrativeVectorizer


@pytest.fixture
def mock_entity_service(mocker: MockerFixture) -> MockerFixture:
    """模拟 EntityGraphService."""
    mock = mocker.MagicMock()
    return mock


@pytest.fixture
def mock_image_analysis_service(mocker: MockerFixture) -> MockerFixture:
    """模拟 ImageAnalysisService."""
    mock = mocker.MagicMock()
    mock.get_analysis_result = mocker.AsyncMock(
        return_value={"details": {"description": "一只戴着墨镜的柴犬"}}
    )
    mock._calculate_image_hash.return_value = "mock_hash_123"
    return mock


@pytest.fixture
def mock_semantic_model(mocker: MockerFixture) -> MockerFixture:
    """模拟 SemanticModel."""
    mock = mocker.MagicMock()
    mock.encode.return_value = [np.array([0.1, 0.2, 0.3, 0.4])]
    return mock


@pytest.fixture
def vectorizer(
    mock_entity_service: MockerFixture,
    mock_image_analysis_service: MockerFixture,
    mock_semantic_model: MockerFixture,
) -> NarrativeVectorizer:
    """创建一个带有模拟依赖的 NarrativeVectorizer 实例."""
    return NarrativeVectorizer(
        entity_service=mock_entity_service,
        image_analysis_service=mock_image_analysis_service,
        semantic_model=mock_semantic_model,
    )


def create_test_event(
    user_id: str = "12345",
    nickname: str = "测试用户",
    text: str | None = None,
    image_b64: str | None = None,
    conv_name: str = "测试群",
) -> Event:
    """辅助函数，用于创建测试用的 Event 对象."""
    content = []
    if text:
        # --- [核心修正] 使用 SegBuilder.text() ---
        content.append(SegBuilder.text(text))
        # --- [结束修正] ---
    if image_b64:
        # --- [核心修正] 使用 SegBuilder.image() ---
        content.append(SegBuilder.image(base64=image_b64))
        # --- [结束修正] ---

    return Event(
        event_id="test-event-id",
        event_type="message.qq.group",
        time=1234567890,
        bot_id="bot-999",
        user_info=UserInfo(user_id=user_id, user_nickname=nickname, permission_level="群主"),
        conversation_info=ConversationInfo(
            conversation_id="group-abc", type="group", name=conv_name
        ),
        content=content,
    )


@pytest.mark.asyncio
async def test_vectorize_text_only_event(vectorizer: NarrativeVectorizer) -> None:
    """测试场景1: 纯文本事件."""
    event = create_test_event(text="你好啊")
    sentence, vector = await vectorizer.build_and_vectorize(event)

    expected_sentence = "群主，对话参与者，'12345'，在'测试群'的场景下，并附言：“你好啊”。"
    assert sentence == expected_sentence
    assert vector == [0.1, 0.2, 0.3, 0.4]
    vectorizer.semantic_model.encode.assert_called_once_with([expected_sentence])


@pytest.mark.asyncio
async def test_vectorize_image_and_text_event(vectorizer: NarrativeVectorizer) -> None:
    """测试场景2: 图文混合事件."""
    event = create_test_event(text="看这张图", image_b64="fake_base64_string")
    sentence, vector = await vectorizer.build_and_vectorize(event)

    expected_sentence = "群主，对话参与者，'12345'，在'测试群'的场景下，发送了image，内容为“一只戴着墨镜的柴犬”，并附言：“看这张图”。"  # noqa: E501
    assert sentence == expected_sentence
    assert vector == [0.1, 0.2, 0.3, 0.4]
    vectorizer.image_analysis_service.get_analysis_result.assert_awaited_once()


@pytest.mark.asyncio
async def test_vectorize_image_only_event(vectorizer: NarrativeVectorizer) -> None:
    """测试场景3: 纯图片事件."""
    event = create_test_event(image_b64="fake_base64_string")
    sentence, vector = await vectorizer.build_and_vectorize(event)

    expected_sentence = (
        "群主，对话参与者，'12345'，在'测试群'的场景下，发送了image，内容为“一只戴着墨镜的柴犬”。"
    )
    assert sentence == expected_sentence
    assert vector is not None


@pytest.mark.asyncio
async def test_vectorize_event_with_no_user_info(vectorizer: NarrativeVectorizer) -> None:
    """测试场景4: 缺少 user_info 的异常事件."""
    event = create_test_event(text="你好")
    event.user_info = None
    sentence, vector = await vectorizer.build_and_vectorize(event)

    assert sentence is None
    assert vector is None
