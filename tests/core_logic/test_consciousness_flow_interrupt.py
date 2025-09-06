# tests/core_logic/test_consciousness_flow_interrupt.py

import asyncio
from unittest.mock import MagicMock

import pytest
from pytest_mock import MockerFixture
from src.core_logic.consciousness_flow import CoreLogic, ThoughtGenerationError
from src.domain.models import Stimulus
from src.focus_chat_mode.components import PromptComponents
from src.prompt_builder import PromptBuilderError
from src.services.database.models import ThoughtChainDocument


def create_real_stimulus(
    event_id: str, text: str, sender_id: str, embedding: list[float] | None
) -> Stimulus:
    """创建一个真实的 Stimulus 对象用于测试."""
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
    """创建一个模拟的 ChatSession."""
    session = mocker.MagicMock()
    session.conversation_id = "test_conv_123"
    session.intelligent_interrupter = mocker.MagicMock()
    session.get_bot_profile = mocker.AsyncMock(return_value={"user_id": "bot_id"})
    return session


# 创建一个 fixture，它只负责提供一个单一的、共享的队列实例
@pytest.fixture
def shared_interrupt_queue() -> asyncio.Queue:
    """提供一个在测试作用域内共享的 asyncio.Queue 实例."""
    return asyncio.Queue()


@pytest.fixture
def mock_interruption_broker(
    mocker: MockerFixture, shared_interrupt_queue: asyncio.Queue
) -> MagicMock:
    """创建一个模拟的 InterruptionBroker，它总是返回同一个共享队列."""
    broker = mocker.MagicMock()
    # 关键：配置 subscribe 的 AsyncMock，使其 return_value 固定为我们注入的共享队列
    broker.subscribe = mocker.AsyncMock(return_value=shared_interrupt_queue)
    broker.unsubscribe = mocker.AsyncMock()
    return broker


@pytest.fixture
def core_logic(mocker: MockerFixture, mock_interruption_broker: MagicMock) -> CoreLogic:
    """创建一个带有模拟依赖的 CoreLogic 实例."""
    logic = CoreLogic(
        core_comm_layer=mocker.MagicMock(),
        action_handler_instance=mocker.MagicMock(),
        state_manager=mocker.MagicMock(),
        chat_session_manager=mocker.MagicMock(),
        thought_storage_service=mocker.MagicMock(),
        entity_graph_service=mocker.MagicMock(),  # 添加缺失的 entity_graph_service 参数
        thought_generator=mocker.MagicMock(),
        thought_persistor=mocker.MagicMock(),
        prompt_builder=mocker.MagicMock(),
        stop_event=mocker.MagicMock(),
        interruption_broker=mock_interruption_broker,
        immediate_thought_trigger=asyncio.Event(),  # 使用真正的 asyncio.Event 而不是 MagicMock
    )
    return logic


@pytest.mark.asyncio
async def test_sentry_returns_stimulus_on_interrupt(
    core_logic: CoreLogic,
    mock_session: MagicMock,
    shared_interrupt_queue: asyncio.Queue,
) -> None:
    """测试当 should_interrupt 返回 True 时，哨兵能正确返回 Stimulus."""
    # 不再需要调用 broker.subscribe()，因为我们直接操作注入的队列
    interrupting_stimulus = create_real_stimulus("interrupt-001", "紧急！", "user_1", [0.1])
    mock_session.intelligent_interrupter.should_interrupt.return_value = True

    sentry_task = asyncio.create_task(core_logic._listen_for_interruptions(mock_session, 0))
    await asyncio.sleep(0.01)
    # 直接向共享队列中放入 stimulus
    await shared_interrupt_queue.put(interrupting_stimulus)

    result_stimulus = await asyncio.wait_for(sentry_task, timeout=1.0)

    assert result_stimulus is interrupting_stimulus
    mock_session.intelligent_interrupter.should_interrupt.assert_called_once_with(
        new_stimulus=interrupting_stimulus, context_stimulus=None
    )


@pytest.mark.asyncio
async def test_sentry_continues_on_no_interrupt(
    core_logic: CoreLogic,
    mock_session: MagicMock,
    shared_interrupt_queue: asyncio.Queue,
) -> None:
    """测试当 should_interrupt 返回 False 时，哨兵会继续等待."""
    non_interrupting_stimulus = create_real_stimulus("normal-001", "没事", "user_2", [0.2])
    interrupting_stimulus = create_real_stimulus("interrupt-002", "紧急！", "user_1", [0.1])

    mock_session.intelligent_interrupter.should_interrupt.side_effect = [False, True]

    sentry_task = asyncio.create_task(core_logic._listen_for_interruptions(mock_session, 0))
    await asyncio.sleep(0.01)

    await shared_interrupt_queue.put(non_interrupting_stimulus)
    await asyncio.sleep(0.01)
    assert not sentry_task.done()

    await shared_interrupt_queue.put(interrupting_stimulus)

    result_stimulus = await asyncio.wait_for(sentry_task, timeout=1.0)

    assert result_stimulus is interrupting_stimulus
    assert mock_session.intelligent_interrupter.should_interrupt.call_count == 2
    mock_session.intelligent_interrupter.should_interrupt.assert_called_with(
        new_stimulus=interrupting_stimulus, context_stimulus=non_interrupting_stimulus
    )


@pytest.mark.asyncio
async def test_sentry_handles_stimulus_without_embedding(
    core_logic: CoreLogic,
    mock_session: MagicMock,
    shared_interrupt_queue: asyncio.Queue,
) -> None:
    """测试当收到"贫血"Stimulus 时，哨兵不会崩溃并能正确更新上下文."""
    stimulus_no_embedding = create_real_stimulus("bad-001", "没向量", "user_3", None)
    interrupting_stimulus = create_real_stimulus("good-002", "有向量", "user_1", [0.3])

    mock_session.intelligent_interrupter.should_interrupt.return_value = True

    sentry_task = asyncio.create_task(core_logic._listen_for_interruptions(mock_session, 0))
    await asyncio.sleep(0.01)

    await shared_interrupt_queue.put(stimulus_no_embedding)
    await asyncio.sleep(0.01)
    assert not sentry_task.done()

    await shared_interrupt_queue.put(interrupting_stimulus)
    result_stimulus = await asyncio.wait_for(sentry_task, timeout=1.0)

    assert result_stimulus is interrupting_stimulus
    mock_session.intelligent_interrupter.should_interrupt.assert_called_once_with(
        new_stimulus=interrupting_stimulus, context_stimulus=stimulus_no_embedding
    )


@pytest.mark.asyncio
async def test_get_current_session_with_valid_focus_path(
    core_logic: CoreLogic, mocker: MockerFixture
) -> None:
    """测试获取当前会话的有效焦点路径."""
    mock_session = mocker.MagicMock()
    mock_session.conversation_id = "cellular_test_platform_group.123456"

    core_logic.chat_session_manager.current_focus_path = {
        "target_path": "cellular.test_platform.group.123456"
    }
    core_logic.chat_session_manager.sessions = {"cellular_test_platform_group.123456": mock_session}

    result = core_logic._get_current_session()
    assert result is mock_session


@pytest.mark.asyncio
async def test_get_current_session_with_invalid_focus_path(core_logic: CoreLogic) -> None:
    """测试获取当前会话的无效焦点路径."""
    core_logic.chat_session_manager.current_focus_path = {"target_path": "invalid.path.format"}
    # 确保会话字典为空，这样get方法会返回None而不是MagicMock
    core_logic.chat_session_manager.sessions = {}
    result = core_logic._get_current_session()
    assert result is None


@pytest.mark.asyncio
async def test_get_current_session_no_focus_path(core_logic: CoreLogic) -> None:
    """测试没有焦点路径时获取当前会话."""
    core_logic.chat_session_manager.current_focus_path = None
    result = core_logic._get_current_session()
    assert result is None


@pytest.mark.asyncio
async def test_trigger_immediate_thought_cycle(core_logic: CoreLogic) -> None:
    """测试立即触发思考循环."""
    assert not core_logic.immediate_thought_trigger.is_set()
    core_logic.trigger_immediate_thought_cycle()
    assert core_logic.immediate_thought_trigger.is_set()


@pytest.mark.asyncio
async def test_wait_for_next_cycle_timeout(core_logic: CoreLogic) -> None:
    """测试等待下一个周期的超时情况."""
    start_time = asyncio.get_event_loop().time()
    await core_logic._wait_for_next_cycle(0.1)
    end_time = asyncio.get_event_loop().time()
    # 允许一定的误差范围，因为计时可能不精确
    assert end_time - start_time >= 0.09


@pytest.mark.asyncio
async def test_wait_for_next_cycle_immediate_trigger(core_logic: CoreLogic) -> None:
    """测试等待下一个周期被立即触发."""

    async def trigger_immediately() -> None:
        await asyncio.sleep(0.01)
        core_logic.trigger_immediate_thought_cycle()

    start_time = asyncio.get_event_loop().time()
    await asyncio.gather(core_logic._wait_for_next_cycle(1.0), trigger_immediately())
    end_time = asyncio.get_event_loop().time()
    assert end_time - start_time < 0.5  # 应该远小于1秒


@pytest.mark.asyncio
async def test_process_sentry_victory(core_logic: CoreLogic, mock_session: MagicMock) -> None:
    """测试处理哨兵胜利场景."""
    interrupting_stimulus = create_real_stimulus("interrupt-003", "测试中断", "user_1", [0.1])

    # 创建一个已完成的任务，并设置返回值
    async def mock_sentry_task() -> object:
        return interrupting_stimulus

    sentry_task = asyncio.create_task(mock_sentry_task())
    await sentry_task  # 确保任务完成

    await core_logic._process_sentry_victory(sentry_task, mock_session)

    # 验证interruption_context被正确设置
    assert mock_session.interruption_context == {
        "was_interrupted": True,
        "interrupting_stimulus": interrupting_stimulus,
    }
    assert mock_session.last_processed_timestamp == interrupting_stimulus.timestamp
    assert core_logic._last_interrupt_context_stimulus is interrupting_stimulus


@pytest.mark.asyncio
async def test_process_main_task_victory(
    core_logic: CoreLogic, mock_session: MagicMock, mocker: MockerFixture
) -> None:
    """测试处理主任务胜利场景."""
    mock_session.last_processed_timestamp = 1000
    last_processed_ts = 2000

    # 创建一个已完成的任务，并设置返回值
    async def mock_main_task() -> int:
        return last_processed_ts

    main_task = asyncio.create_task(mock_main_task())
    await main_task  # 确保任务完成

    # 模拟entity_graph_service的异步方法
    core_logic.entity_graph_service.update_conversation_last_read_timestamp = mocker.AsyncMock()

    await core_logic._process_main_task_victory(main_task, mock_session)

    assert mock_session.last_processed_timestamp == last_processed_ts
    assert mock_session.interruption_context is None
    assert core_logic._last_interrupt_context_stimulus is None
    core_logic.entity_graph_service.update_conversation_last_read_timestamp.assert_called_once_with(
        conversation_entity_uid=mock_session.conversation_id, timestamp=last_processed_ts
    )


@pytest.mark.asyncio
async def test_handle_race_outcome_sentry_victory(
    core_logic: CoreLogic, mock_session: MagicMock, mocker: MockerFixture
) -> None:
    """测试处理竞速结果 - 哨兵胜利."""
    interrupting_stimulus = create_real_stimulus("interrupt-004", "竞速中断", "user_1", [0.1])

    # 创建已完成和未完成的任务
    async def mock_sentry_task() -> object:
        return interrupting_stimulus

    sentry_task = asyncio.create_task(mock_sentry_task())
    await sentry_task

    main_task = asyncio.create_task(asyncio.sleep(10))  # 长时间运行的任务

    done = {sentry_task}
    pending = {main_task}

    # 模拟process_sentry_victory方法
    core_logic._process_sentry_victory = mocker.AsyncMock()

    await core_logic._handle_race_outcome(done, pending, mock_session, main_task, sentry_task)

    # 验证主任务被取消
    assert main_task.cancelled()
    core_logic._process_sentry_victory.assert_called_once_with(sentry_task, mock_session)


@pytest.mark.asyncio
async def test_handle_race_outcome_main_victory(
    core_logic: CoreLogic, mock_session: MagicMock, mocker: MockerFixture
) -> None:
    """测试处理竞速结果 - 主任务胜利."""
    last_processed_ts = 3000

    # 创建已完成和未完成的任务
    async def mock_main_task() -> int:
        """模拟主任务."""
        return last_processed_ts

    main_task = asyncio.create_task(mock_main_task())
    await main_task

    sentry_task = asyncio.create_task(asyncio.sleep(10))  # 长时间运行的任务

    done = {main_task}
    pending = {sentry_task}

    # 模拟process_main_task_victory方法
    core_logic._process_main_task_victory = mocker.AsyncMock()

    await core_logic._handle_race_outcome(done, pending, mock_session, main_task, sentry_task)

    # 验证哨兵任务被取消
    assert sentry_task.cancelled()
    core_logic._process_main_task_victory.assert_called_once_with(main_task, mock_session)


@pytest.mark.asyncio
async def test_prepare_and_run_race_no_session(
    core_logic: CoreLogic, mocker: MockerFixture
) -> None:
    """测试准备和执行竞速 - 没有会话的情况."""
    core_logic._get_current_session = mocker.Mock(return_value=None)
    core_logic._run_full_thought_cycle = mocker.AsyncMock(return_value=None)

    await core_logic._prepare_and_run_race()

    core_logic._run_full_thought_cycle.assert_called_once_with(None)


@pytest.mark.asyncio
async def test_prepare_and_run_race_with_session(
    core_logic: CoreLogic, mock_session: MagicMock, mocker: MockerFixture
) -> None:
    """测试准备和执行竞速 - 有会话的情况."""
    core_logic._get_current_session = mocker.Mock(return_value=mock_session)
    core_logic._run_full_thought_cycle = mocker.AsyncMock(return_value=1000)
    core_logic._listen_for_interruptions = mocker.AsyncMock(return_value=None)
    core_logic._handle_race_outcome = mocker.AsyncMock()

    await core_logic._prepare_and_run_race()

    core_logic._run_full_thought_cycle.assert_called_once_with(mock_session)
    core_logic._listen_for_interruptions.assert_called_once_with(mock_session, mocker.ANY)


@pytest.mark.asyncio
async def test_generate_and_persist_thought_success(
    core_logic: CoreLogic, mocker: MockerFixture
) -> None:
    """测试成功生成和持久化思考."""
    prompt_components = PromptComponents(
        system_prompt_blocks={"main": "Test system prompt"},
        user_prompt_blocks={"main": "Test user prompt"},
        image_references=[],
        user_map={},
        uid_str_to_platform_id_map={},
    )

    mock_thought_json = {"thought": "test thought", "action_payload": {}}
    mock_thought_doc = ThoughtChainDocument(
        _key="test_thought_id",
        timestamp="1000",
        mood="neutral",
        think="test thought",
        intent="test intent",
        source_type="test",
        action_id="test_action_id",
        action_payload={},
    )

    core_logic.prompt_builder.finalize_prompts = mocker.Mock(
        return_value=("system", "user", "schema")
    )
    core_logic.thought_generator.generate_thought = mocker.AsyncMock(return_value=mock_thought_json)
    core_logic.thought_persistor.store_thought = mocker.AsyncMock(
        return_value=("saved_key", mock_thought_doc)
    )

    result_thought, result_key = await core_logic._generate_and_persist_thought(
        prompt_components, "test_focus_path"
    )

    assert result_thought is mock_thought_doc
    assert result_key == "saved_key"
    core_logic.thought_generator.generate_thought.assert_called_once()
    core_logic.thought_persistor.store_thought.assert_called_once()


@pytest.mark.asyncio
async def test_generate_and_persist_thought_generation_failure(
    core_logic: CoreLogic, mocker: MockerFixture
) -> None:
    """测试思考生成失败的情况."""
    prompt_components = PromptComponents(
        system_prompt_blocks={"main": "Test system prompt"},
        user_prompt_blocks={"main": "Test user prompt"},
        image_references=[],
        user_map={},
        uid_str_to_platform_id_map={},
    )

    core_logic.prompt_builder.finalize_prompts = mocker.Mock(
        return_value=("system", "user", "schema")
    )
    core_logic.thought_generator.generate_thought = mocker.AsyncMock(return_value=None)

    with pytest.raises(ThoughtGenerationError, match="LLM未能生成有效的思考JSON"):
        await core_logic._generate_and_persist_thought(prompt_components, "test_focus_path")


@pytest.mark.asyncio
async def test_generate_and_persist_thought_persistence_failure(
    core_logic: CoreLogic, mocker: MockerFixture
) -> None:
    """测试思考持久化失败的情况."""
    prompt_components = PromptComponents(
        system_prompt_blocks={"main": "Test system prompt"},
        user_prompt_blocks={"main": "Test user prompt"},
        image_references=[],
        user_map={},
        uid_str_to_platform_id_map={},
    )

    mock_thought_json = {"thought": "test thought", "action_payload": {}}

    core_logic.prompt_builder.finalize_prompts = mocker.Mock(
        return_value=("system", "user", "schema")
    )
    core_logic.thought_generator.generate_thought = mocker.AsyncMock(return_value=mock_thought_json)
    core_logic.thought_persistor.store_thought = mocker.AsyncMock(return_value=(None, None))

    with pytest.raises(ThoughtGenerationError, match="未能将新的思考持久化到数据库"):
        await core_logic._generate_and_persist_thought(prompt_components, "test_focus_path")


@pytest.mark.asyncio
async def test_run_full_thought_cycle_prompt_builder_error(
    core_logic: CoreLogic, mock_session: MagicMock, mocker: MockerFixture
) -> None:
    """测试完整思考周期 - PromptBuilder错误."""
    core_logic.prompt_builder.build_prompts_components = mocker.AsyncMock(
        side_effect=PromptBuilderError("Build error")
    )

    result = await core_logic._run_full_thought_cycle(mock_session)
    assert result is None


@pytest.mark.asyncio
async def test_run_full_thought_cycle_thought_generation_error(
    core_logic: CoreLogic, mock_session: MagicMock, mocker: MockerFixture
) -> None:
    """测试完整思考周期 - 思考生成错误."""
    core_logic.prompt_builder.build_prompts_components = mocker.AsyncMock(
        return_value=(
            PromptComponents(
                system_prompt_blocks={"main": "test"},
                user_prompt_blocks={"main": "test"},
                image_references=[],
                user_map={},
                uid_str_to_platform_id_map={},
            ),
            [],
        )
    )
    core_logic._generate_and_persist_thought = mocker.AsyncMock(
        side_effect=ThoughtGenerationError("Generation error")
    )

    result = await core_logic._run_full_thought_cycle(mock_session)
    assert result is None


@pytest.mark.asyncio
async def test_start_and_stop_thinking_loop(core_logic: CoreLogic, mocker: MockerFixture) -> None:
    """测试启动和停止思考循环."""
    # 模拟核心思考循环
    mock_loop = mocker.AsyncMock()
    core_logic._core_thinking_loop = mock_loop

    # 启动思考循环
    task = await core_logic.start_thinking_loop()
    assert task is not None
    assert core_logic.thinking_loop_task is task

    # 停止思考循环
    await core_logic.stop()
    assert core_logic.stop_event.is_set()
    mock_loop.assert_called_once()


@pytest.mark.asyncio
async def test_core_thinking_loop_cancelled(core_logic: CoreLogic, mocker: MockerFixture) -> None:
    """测试核心思考循环被取消的情况."""
    # 模拟核心思考循环立即返回（因为stop_event已设置）
    core_logic.stop_event.set()  # 立即设置停止事件
    core_logic._prepare_and_run_race = mocker.AsyncMock()

    # 调用核心思考循环，应该立即退出
    await core_logic._core_thinking_loop()

    # 验证_prepare_and_run_race没有被调用（因为循环立即退出）
    core_logic._prepare_and_run_race.assert_not_called()
