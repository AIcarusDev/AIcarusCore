# src/core_logic/decision_dispatcher.py (竞速模式适配版 V1.0)
import asyncio
from typing import TYPE_CHECKING, Optional

from aicarus_protocols import Event
from src.action.components.message_builder import MessageBuilder
from src.common.custom_logging.logging_config import get_logger
from src.common.utils import parse_focus_path
from src.platform_builders.registry import platform_builder_registry

if TYPE_CHECKING:
    from src.action.action_handler import ActionHandler
    from src.core_logic.consciousness_flow import CoreLogic
    from src.focus_chat_mode.chat_session import ChatSession
    from src.focus_chat_mode.chat_session_manager import ChatSessionManager

logger = get_logger(__name__)


def normalize_action_payload(action_payload: dict, current_platform_id: str) -> dict:
    """一步到位地规范化LLM返回的action_payload.

    它现在会智能判断动作是属于 'core' 还是特定平台.
    """
    if not action_payload or not isinstance(action_payload, dict):
        return {}

    all_platform_ids = platform_builder_registry.get_all_builders().keys()
    if any(key in all_platform_ids for key in action_payload) or "core" in action_payload:
        return action_payload

    if (action_name := next(iter(action_payload), None)) and (
        core_builder := platform_builder_registry.get_builder("core")
    ):
        core_actions_schema, _ = core_builder.get_level_actions_definitions("core")
        if action_name in core_actions_schema.get("properties", {}):
            logger.debug(f"动作 '{action_name}' 被识别为核心动作。")
            normalized_payload = {"core": action_payload}
            logger.info(f"[探灯A] 扁平动作已规范化为: {normalized_payload}")
            return normalized_payload

    logger.debug(f"检测到扁平的平台动作，将使用当前平台上下文 '{current_platform_id}' 进行规范化。")
    normalized_payload = {current_platform_id: action_payload}
    logger.info(f"[探灯A] 扁平动作已规范化为: {normalized_payload}")
    return normalized_payload


async def _handle_send_message_action(
    session: "ChatSession",
    params: dict,
    core_logic: "CoreLogic",
    processed_events_this_turn: list[Event] | None,
) -> None:
    """专门处理 send_message 动作的特种行动小队.

    负责锁定状态、发送消息、等待回声、触发后续思考.

    Args:
        session: 当前的专注会话实例.
        params: 包含发送消息所需的参数.
        core_logic: 核心逻辑处理器，用于触发后续思考.
        processed_events_this_turn: 本轮处理过的事件列表（可选）.
    """
    # 1. 锁定时间戳
    if processed_events_this_turn:
        latest_ts = max(event.time for event in processed_events_this_turn)
        if latest_ts > session.last_processed_timestamp:
            session.last_processed_timestamp = latest_ts
            logger.info(f"[{session.conversation_id}] 高潮锁定：时间戳已更新至 {latest_ts}")

    # 2. 锁定记忆烙印
    if steps := params.get("steps", []):
        texts_to_send = [
            s.get("params", {}).get("content", "") for s in steps if s.get("command") == "text"
        ]
        if new_context_text := " ".join(texts_to_send).strip():
            core_logic._last_interrupt_context_text = new_context_text

    # 3. 清理并准备发送
    session.sent_action_ids_this_turn.clear()
    logger.debug(f"[{session.conversation_id}] 已清空上一轮的 sent_action_ids_this_turn 列表。")

    message_builder = MessageBuilder(session, motivation=params.get("motivation"))
    await message_builder.process_steps(steps)

    # 4. 等待回声
    if sent_action_ids := session.sent_action_ids_this_turn:
        logger.debug(
            f"[{session.conversation_id}] 准备为 {len(sent_action_ids)} 个动作等待回声: "
            f"{sent_action_ids}"
        )
        wait_tasks = [session.wait_for_echo(action_id) for action_id in sent_action_ids]
        results = await asyncio.gather(*wait_tasks)

        # 5. 处理结果
        success_count = results.count(True)
        if success_count == len(sent_action_ids):
            logger.success(
                f"[{session.conversation_id}] 所有 {len(sent_action_ids)} 条消息的回声均已收到。"
            )
            logger.info(f"[{session.conversation_id}] 消息已全部发送完毕，立即触发下一轮思考。")
            core_logic.trigger_immediate_thought_cycle()
        else:
            logger.warning(
                f"[{session.conversation_id}] {len(sent_action_ids) - success_count} "
                f"/ {len(sent_action_ids)} 条消息的回声等待超时。"
            )


async def process_llm_decision(
    decision_json: dict,
    focus_manager: "ChatSessionManager",
    action_handler: "ActionHandler",
    core_logic: "CoreLogic",
    source_thought_key: str | None = None,
    source_action_id: str | None = None,
    current_focus_path: str | None = None,
    session: Optional["ChatSession"] = None,
    processed_events_this_turn: list[Event] | None = None,
) -> None:
    """一个统一的LLM决策分发器.

    Args:
        decision_json: LLM返回的决策JSON对象.
        focus_manager: 当前的专注会话管理器.
        action_handler: 处理动作流的核心处理器.
        core_logic: 核心逻辑处理器.
        source_thought_key: 来源思考的键（可选）.
        source_action_id: 来源动作的ID（可选）.
        current_focus_path: 当前专注路径（可选）.
        session: 当前的专注会话实例（可选）.
        processed_events_this_turn: 本轮处理过的事件列表（可选）.
    """
    if not decision_json or not isinstance(decision_json, dict):
        logger.warning("收到的LLM决策为空或非字典格式，无法分发。")
        return

    logger.info(f"决策分发器开始处理LLM决策: {decision_json}")
    _, current_platform_id, _ = parse_focus_path(current_focus_path)

    action_payload = normalize_action_payload(decision_json.get("action"), current_platform_id)
    control_payload = decision_json.get("consciousness_control")

    # --- 步骤 1: 优先处理 Action (如果有)，并等待其完成 ---
    if action_payload:
        platform_key = next(iter(action_payload), None)
        action_name = next(iter(action_payload.get(platform_key, {})), None)
        # 特殊处理 send_message 动作
        if action_name == "send_message":
            logger.info("检测到 [send_message] 动作，将完整执行并等待回声，然后再继续。")
            action_params = action_payload.get(platform_key, {}).get("send_message", {})

            if session:
                await _handle_send_message_action(
                    session, action_params, core_logic, processed_events_this_turn
                )
            else:
                logger.error("send_message 动作只能在专注会话中执行，但当前会话实例为空！")

        else:  # 如果是其他即做即走的动作
            logger.info(f"检测到 [即做即走类] 动作 ({platform_key}.{action_name})，将立即执行。")
            await action_handler.process_action_flow(
                action_id=source_action_id,
                doc_key_for_updates=source_thought_key,
                action_json=action_payload,
            )

    # --- 步骤 2: 在所有 Action 处理完毕后，再处理 Consciousness Control (如果有) ---
    if control_payload:
        logger.info("所有 Action 已处理完毕，现在开始处理 [意识控制] 指令。")
        await focus_manager.handle_consciousness_control(control_payload)

    # --- 步骤 3: 检查是否无任何指令 ---
    if not action_payload and not control_payload:
        logger.info("本轮决策中无任何有效动作或意识控制指令。")
        if core_logic and core_logic.immediate_thought_trigger:
            level, _, _ = parse_focus_path(current_focus_path)
            if level != 'cellular':
                logger.info("AI决定保持沉默，且不在专注聊天中，将在常规间隔后进行下一轮思考。")
                # 这里不需要手动触发，让主循环的 timeout 机制自然触发即可。
                pass

    logger.info("决策分发处理完毕。")
