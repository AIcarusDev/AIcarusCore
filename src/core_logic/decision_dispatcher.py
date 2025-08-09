# src/core_logic/decision_dispatcher.py
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
) -> bool:
    """专门处理 send_message 动作的特种行动小队."""
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

    message_builder = MessageBuilder(session, motivation=params.get("motivation", "没有明确动机"))
    any_message_sent = await message_builder.process_steps(steps)

    if any_message_sent:
        logger.info(
            f"[{session.conversation_id}] MessageBuilder 已成功发送消息，立即触发下一轮思考。"
        )
        # 立即触发思考，让AI的反应更连贯
        core_logic.trigger_immediate_thought_cycle()
        return True
    else:
        logger.warning(f"[{session.conversation_id}] MessageBuilder 未能发送任何消息。")
        return False


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

    # --- 步骤 1: 解析所有潜在指令 ---
    control_payload = decision_json.get("consciousness_control")
    action_payload = decision_json.get("action")
    current_internal_state = decision_json.get("internal_state", {})

    # --- 步骤 2: (特例优先) 检查并执行“慢思考” ---
    if control_payload and "deep_think" in control_payload:
        logger.info("检测到 [慢思考] 指令，优先执行内部辩论...")

        # “慢思考”是同步阻塞的，它会返回一个修正后的思考状态
        new_internal_state = await focus_manager.handle_consciousness_control(
            {"deep_think": control_payload["deep_think"]}, current_internal_state
        )

        if new_internal_state:
            logger.success("“慢思考”决策管线已完成，使用其决议更新当前思考状态。")
            current_internal_state = new_internal_state  # 更新思考状态
        else:
            logger.warning("“慢思考”执行完毕但未返回有效决议，将使用原始思考状态继续。")

        # 从控制载荷中移除已被处理的慢思考指令
        del control_payload["deep_think"]
        if not control_payload:  # 如果没有其他控制指令了
            control_payload = None

    # --- 步骤 3: 执行“外部行动” ---
    # 使用最新的（可能已被慢思考修正的）状态来驱动行动
    normalized_action_payload = normalize_action_payload(action_payload, current_platform_id)
    if normalized_action_payload:
        logger.info("内部状态已确定，现在开始处理 [外部行动] 指令。")

        platform_key = next(iter(normalized_action_payload), None)
        action_name = next(iter(normalized_action_payload.get(platform_key, {})), None)

        if action_name == "send_message":
            logger.info("检测到 [send_message] 动作，将执行发送并立即触发后续思考。")
            action_params = normalized_action_payload.get(platform_key, {}).get("send_message", {})
            if session:
                await _handle_send_message_action(
                    session, action_params, core_logic, processed_events_this_turn
                )
            else:
                logger.error("send_message 动作只能在专注会话中执行，但当前会话实例为空！")
        else:
            logger.info(f"检测到 [即做即走类] 动作 ({platform_key}.{action_name})，将立即执行。")
            await action_handler.process_action_flow(
                action_id=source_action_id,
                doc_key_for_updates=source_thought_key,
                action_json=normalized_action_payload,
            )

    # --- 步骤 4: (最后执行) 处理剩余的“注意力转移”指令 ---
    if control_payload:
        logger.info("所有外部行动已处理完毕，现在开始处理 [注意力转移] 。")
        await focus_manager.handle_consciousness_control(control_payload, current_internal_state)

    # --- 步骤 5: 检查是否无任何指令 ---
    if not normalized_action_payload and not control_payload:
        logger.info("本轮决策中无任何有效动作或注意力转移指令。")

    logger.info("决策分发处理完毕。")
