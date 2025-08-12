# tests/core_logic/test_consciousness_flow_interrupt.py

import asyncio
from unittest.mock import MagicMock

import pytest
from pytest_mock import MockerFixture
from src.core_logic.consciousness_flow import CoreLogic
from src.domain.models import Stimulus


# --- 辅助函数和Fixture (修正后) ---
def create_real_stimulus(
    event_id: str, text: str, sender_id: str, embedding: list[float] | None
) -> Stimulus:
    """创建一个真实的 Stimulus 对象用于测试。"""
    return Stimulus(
        event_id=event_id,
        timestamp=int(asyncio.get_event_loop().time() * 1000),
        platform="test_platform",
        bot_id="bot_id",
        text_content=text,
        sender_id=sender_id,
        embedding=embedding,
        image_urls=[],
        raw_content=[{"type": "text", "data": {"text": text}}],
    )


@pytest.fixture
def mock_session(mocker: MockerFixture) -> MagicMock:
    """创建一个模拟的 ChatSession。"""
    session = mocker.MagicMock()
    session.conversation_id = "test_conv_123"
    session.intelligent_interrupter = mocker.MagicMock()
    session.get_bot_profile = mocker.AsyncMock(return_value={"user_id": "bot_id"})
    return session


@pytest.fixture
def mock_interruption_broker(mocker: MockerFixture) -> MagicMock:
    broker = mocker.MagicMock()
    broker.subscribe = mocker.AsyncMock(return_value=asyncio.Queue())
    broker.unsubscribe = mocker.AsyncMock()
    return broker


@pytest.fixture
def core_logic(mocker: MockerFixture, mock_interruption_broker: MagicMock) -> CoreLogic:
    """创建一个带有模拟依赖的 CoreLogic 实例。"""
    logic = CoreLogic(
        core_comm_layer=mocker.MagicMock(),
        action_handler_instance=mocker.MagicMock(),
        state_manager=mocker.MagicMock(),
        chat_session_manager=mocker.MagicMock(),
        thought_storage_service=mocker.MagicMock(),
        thought_generator=mocker.MagicMock(),
        thought_persistor=mocker.MagicMock(),
        prompt_builder=mocker.MagicMock(),
        stop_event=mocker.MagicMock(),
        interruption_broker=mock_interruption_broker,
        immediate_thought_trigger=mocker.MagicMock(),
    )
    return logic


# --- 测试用例 (现在应该可以正常工作) ---

@pytest.mark.asyncio
async def test_sentry_returns_stimulus_on_interrupt(
    core_logic: CoreLogic, mock_session: MagicMock, mock_interruption_broker: MagicMock
):
    """测试当 should_interrupt 返回 True 时，哨兵能正确返回 Stimulus。"""
    queue = await mock_interruption_broker.subscribe.coro()
    interrupting_stimulus = create_real_stimulus("interrupt-001", "紧急！", "user_1", [0.1])
    mock_session.intelligent_interrupter.should_interrupt.return_value = True

    sentry_task = asyncio.create_task(core_logic._listen_for_interruptions(mock_session, 0))
    await asyncio.sleep(0.01)
    await queue.put(interrupting_stimulus)

    result_stimulus = await asyncio.wait_for(sentry_task, timeout=1.0)

    assert result_stimulus is interrupting_stimulus
    mock_session.intelligent_interrupter.should_interrupt.assert_called_once_with(
        new_stimulus=interrupting_stimulus, context_stimulus=None
    )


@pytest.mark.asyncio
async def test_sentry_continues_on_no_interrupt(
    core_logic: CoreLogic, mock_session: MagicMock, mock_interruption_broker: MagicMock
):
    """测试当 should_interrupt 返回 False 时，哨兵会继续等待。"""
    queue = await mock_interruption_broker.subscribe.coro()
    non_interrupting_stimulus = create_real_stimulus("normal-001", "没事", "user_2", [0.2])
    interrupting_stimulus = create_real_stimulus("interrupt-002", "紧急！", "user_1", [0.1])

    mock_session.intelligent_interrupter.should_interrupt.side_effect = [False, True]

    sentry_task = asyncio.create_task(core_logic._listen_for_interruptions(mock_session, 0))
    await asyncio.sleep(0.01)

    await queue.put(non_interrupting_stimulus)
    await asyncio.sleep(0.01)
    assert not sentry_task.done()

    await queue.put(interrupting_stimulus)

    result_stimulus = await asyncio.wait_for(sentry_task, timeout=1.0)

    assert result_stimulus is interrupting_stimulus
    assert mock_session.intelligent_interrupter.should_interrupt.call_count == 2
    mock_session.intelligent_interrupter.should_interrupt.assert_called_with(
        new_stimulus=interrupting_stimulus, context_stimulus=non_interrupting_stimulus
    )


@pytest.mark.asyncio
async def test_sentry_handles_stimulus_without_embedding(
    core_logic: CoreLogic, mock_session: MagicMock, mock_interruption_broker: MagicMock
):
    """测试当收到“贫血”Stimulus 时，哨兵不会崩溃并能正确更新上下文。"""
    queue = await mock_interruption_broker.subscribe.coro()
    stimulus_no_embedding = create_real_stimulus("bad-001", "没向量", "user_3", None)
    interrupting_stimulus = create_real_stimulus("good-002", "有向量", "user_1", [0.3])

    mock_session.intelligent_interrupter.should_interrupt.return_value = True

    sentry_task = asyncio.create_task(core_logic._listen_for_interruptions(mock_session, 0))
    await asyncio.sleep(0.01)

    await queue.put(stimulus_no_embedding)
    await asyncio.sleep(0.01)
    assert not sentry_task.done()

    await queue.put(interrupting_stimulus)
    result_stimulus = await asyncio.wait_for(sentry_task, timeout=1.0)

    assert result_stimulus is interrupting_stimulus
    mock_session.intelligent_interrupter.should_interrupt.assert_called_once_with(
        new_stimulus=interrupting_stimulus, context_stimulus=stimulus_no_embedding
    )