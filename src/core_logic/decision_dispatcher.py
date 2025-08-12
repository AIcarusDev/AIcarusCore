# src/core_logic/decision_dispatcher.py
from typing import TYPE_CHECKING, Optional

from src.action.components.message_builder import MessageBuilder
from src.common.custom_logging.logging_config import get_logger
from src.common.utils import parse_focus_path
from src.domain.models import ActionMetadata, Stimulus
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
    processed_events_this_turn: list[Stimulus] | None,  # <-- 类型变为 Stimulus
) -> bool:
    """专门处理 send_message 动作的特种行动小队."""
    if processed_events_this_turn:
        # Stimulus 对象有 timestamp 属性，这里可以直接使用
        latest_ts = max(s.timestamp for s in processed_events_this_turn)
        if latest_ts > session.last_processed_timestamp:
            session.last_processed_timestamp = latest_ts
            logger.info(f"[{session.conversation_id}] 已更新 last_processed_timestamp: {latest_ts}")

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


async def _handle_deep_think(
    control_payload: dict,
    focus_manager: "ChatSessionManager",
    current_internal_state: dict,
) -> tuple[dict, dict | None]:
    """[辅助函数] 专门处理 'deep_think' 指令."""
    logger.info("检测到 [慢思考] 指令，优先执行内部辩论...")
    new_internal_state = await focus_manager.handle_consciousness_control(
        {"deep_think": control_payload["deep_think"]}, current_internal_state
    )

    if new_internal_state:
        logger.success("“慢思考”决策管线已完成，使用其决议更新当前思考状态。")
        current_internal_state = new_internal_state
    else:
        logger.warning("“慢思考”执行完毕但未返回有效决议，将使用原始思考状态继续。")

    # 从原始 payload 中移除已处理的 deep_think
    del control_payload["deep_think"]
    # 如果 payload 处理后变为空，则将其设为 None
    remaining_control_payload = control_payload if control_payload else None

    return current_internal_state, remaining_control_payload


async def _handle_external_action(
    action_payload: dict,
    current_internal_state: dict,
    action_handler: "ActionHandler",
    core_logic: "CoreLogic",
    session: Optional["ChatSession"],
    processed_events_this_turn: list[Stimulus] | None,
    source_thought_key: str | None,
    source_action_id: str | None,
    current_focus_path: str | None,
) -> None:
    """[辅助函数] 专门处理所有外部 'action' 指令."""
    logger.info("内部状态已确定，现在开始处理 [外部行动] 指令。")
    _, current_platform_id, _ = parse_focus_path(current_focus_path)

    normalized_payload = normalize_action_payload(action_payload, current_platform_id)
    if not normalized_payload:
        logger.warning(f"动作负载在规范化后为空，无法处理: {action_payload}")
        return

    # [FIX] 使用命名表达式简化赋值和条件判断
    if (platform_key := next(iter(normalized_payload), None)) and (
        platform_actions := normalized_payload.get(platform_key)
    ):
        action_name = (
            next(iter(platform_actions), None) if isinstance(platform_actions, dict) else None
        )

        source_event_id = None
        if processed_events_this_turn:
            last_external_stimulus = next(
                (s for s in reversed(processed_events_this_turn) if s.bot_id != s.sender_id), None
            )
            if last_external_stimulus:
                source_event_id = last_external_stimulus.event_id

        motivation_text = current_internal_state.get("intent") or current_internal_state.get(
            "think", "无明确动机"
        )
        metadata = ActionMetadata(
            motivation=motivation_text,
            source_thought_id=source_thought_key,
            source_event_id=source_event_id,
        )

        if action_name == "send_message":
            logger.info("检测到 [send_message] 动作，将执行发送并立即触发后续思考。")
            if session:
                await _handle_send_message_action(
                    session,
                    platform_actions.get("send_message", {}),
                    core_logic,
                    processed_events_this_turn,
                )
            else:
                logger.error("send_message 动作只能在专注会话中执行，但当前会话实例为空！")
        else:
            logger.info(f"检测到 [即做即走类] 动作 ({platform_key}.{action_name})，将立即执行。")
            await action_handler.process_action_flow(
                action_id=source_action_id,
                doc_key_for_updates=source_thought_key,
                action_json=normalized_payload,
                metadata=metadata,
            )


async def _handle_consciousness_control(
    control_payload: dict, focus_manager: "ChatSessionManager", current_internal_state: dict
) -> None:
    """[辅助函数] 专门处理注意力转移指令."""
    logger.info("所有外部行动已处理完毕，现在开始处理 [注意力转移] 。")
    await focus_manager.handle_consciousness_control(control_payload, current_internal_state)


async def _handle_goal_management(control_payload: dict, core_logic: "CoreLogic") -> None:
    """[辅助函数] 专门处理 'manage_goals' 指令."""
    goal_params = control_payload.get("manage_goals", {})
    goal_manager = core_logic.state_manager.goal_manager

    if add_params := goal_params.get("add"):
        goals_to_add = add_params.get("goals", [])
        await goal_manager.add_goals(goals_to_add)
        logger.info(f"已添加 {len(goals_to_add)} 个新目标。")

    if remove_params := goal_params.get("remove"):
        ids_to_remove = remove_params.get("goal_ids", [])
        await goal_manager.remove_goals(ids_to_remove)
        logger.info(f"已移除 {len(ids_to_remove)} 个目标。")

    # 从 payload 中移除已处理的 manage_goals
    del control_payload["manage_goals"]


async def process_llm_decision(
    decision_json: dict,
    focus_manager: "ChatSessionManager",
    action_handler: "ActionHandler",
    core_logic: "CoreLogic",
    source_thought_key: str | None = None,
    source_action_id: str | None = None,
    current_focus_path: str | None = None,
    session: Optional["ChatSession"] = None,
    processed_events_this_turn: list[Stimulus] | None = None,
) -> None:
    """[协调者] 一个统一的LLM决策分发器(重构后)."""
    if not decision_json or not isinstance(decision_json, dict):
        logger.warning("收到的LLM决策为空或非字典格式，无法分发。")
        return

    logger.info(f"决策分发器开始处理LLM决策: {decision_json}")

    # 1. 获取初始状态
    control_payload = decision_json.get("consciousness_control")
    action_payload = decision_json.get("action")
    current_internal_state = decision_json.get("internal_state", {})

    # 2. 优先处理“慢思考”
    if control_payload and "deep_think" in control_payload:
        current_internal_state, control_payload = await _handle_deep_think(
            control_payload, focus_manager, current_internal_state
        )

    # 在尝试访问 control_payload 之前，必须检查它是否为 None
    if control_payload and "manage_goals" in control_payload:
        await _handle_goal_management(control_payload, core_logic)

    # 3. 处理外部动作
    if action_payload:
        await _handle_external_action(
            action_payload,
            current_internal_state,
            action_handler,
            core_logic,
            session,
            processed_events_this_turn,
            source_thought_key,
            source_action_id,
            current_focus_path,
        )

    # 4. 处理剩余的注意力转移指令
    if control_payload:
        await _handle_consciousness_control(control_payload, focus_manager, current_internal_state)

    # 5. 如果什么都没做，记录一下
    if not action_payload and not decision_json.get("consciousness_control"):
        logger.info("本轮决策中无任何有效动作或注意力转移指令。")

    logger.info("决策分发处理完毕。")
