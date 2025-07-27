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
        logger.debug(f"Action payload 已经是标准格式，无需规范化: {action_payload}")
        return action_payload

    # 1. 提取出动作的名字，比如 'web_search'
    action_name = next(iter(action_payload), None)

    if action_name:
        # 2. 找到我们的核心翻译官 CoreBuilder
        core_builder = platform_builder_registry.get_builder("core")
        if core_builder:
            # 3. 问问核心翻译官，它在任何一个层级认不认识这个动作
            #    (web_search 在所有层级都可用，所以随便查一个层就行)
            core_actions_schema, _ = core_builder.get_level_actions_definitions('core')
            if action_name in core_actions_schema.get('properties', {}):
                # 4. 如果认识，就把它标记为 'core' 动作！
                logger.debug(f"动作 '{action_name}' 被识别为核心动作。")
                normalized_payload = {'core': action_payload}
                logger.info(f"[探灯A] 扁平动作已规范化为: {normalized_payload}")
                return normalized_payload

    # 如果不是核心动作，才走原来的老路
    logger.debug(f"检测到扁平的平台动作，将使用当前平台上下文 '{current_platform_id}' 进行规范化。")
    normalized_payload = {current_platform_id: action_payload}
    logger.info(f"[探灯A] 扁平动作已规范化为: {normalized_payload}")
    return normalized_payload


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

    它负责解析并执行LLM的决策。对于需要等待结果的动作（如send_message）,
    它会阻塞直到动作完全确认完成.
    """
    if not decision_json or not isinstance(decision_json, dict):
        logger.warning("收到的LLM决策为空或非字典格式，无法分发。")
        return

    logger.info(f"决策分发器开始处理LLM决策: {decision_json}")

    _, current_platform_id, current_conv_id = parse_focus_path(current_focus_path)
    action_payload = normalize_action_payload(decision_json.get("action"), current_platform_id)
    control_payload = decision_json.get("consciousness_control")

    # --- 1. 优先处理行动指令 (Action) ---
    if action_payload:
        platform_key = next(iter(action_payload), None)
        if not platform_key or not isinstance(action_payload.get(platform_key), dict):
            logger.warning(f"行动指令格式不正确，无法处理: {action_payload}")
            return

        action_name = next(iter(action_payload[platform_key]), None)
        action_params = action_payload[platform_key].get(action_name) if action_name else None

        # --- 策略A: 处理需要等待回声的 `send_message` 动作 ---
        if action_name == "send_message":
            if not current_conv_id or not focus_manager:
                logger.error("send_message 动作只能在专注会话中执行，但当前会话ID为空！")
                return

            session = focus_manager.sessions.get(current_conv_id)
            if not session:
                logger.error(f"找不到会话 {current_conv_id}，无法执行 send_message。")
                return

            # 1. 锁定时间戳
            if processed_events_this_turn:
                latest_ts = max(event.time for event in processed_events_this_turn)
                if latest_ts > session.last_processed_timestamp:
                    session.last_processed_timestamp = latest_ts
                    # 【探针植入】
                    logger.info(f"[{session.conversation_id}] 高潮锁定：时间戳已更新至 {latest_ts}")

            # 2. 锁定记忆烙印
            steps = action_params.get("steps", [])
            # 提取所有要发送的文本内容
            texts_to_send = [
                s.get("params", {}).get("content", "") for s in steps if s.get("command") == "text"
            ]
            # 把它们拼接起来，作为最新的“上下文记忆”
            new_context_text = " ".join(texts_to_send).strip()
            if new_context_text:
                core_logic._last_interrupt_context_text = new_context_text

            logger.info(
                f"[{current_conv_id}] 检测到 [回声类] 动作 (send_message)，将等待回声后才算完成。"
            )

            # 清空上一轮可能残留的ID列表
            session.sent_action_ids_this_turn.clear()
            logger.debug(
                f"[{session.conversation_id}] 已清空上一轮的 sent_action_ids_this_turn 列表。"
            )

            # 使用 MessageBuilder 在后台发送消息，它会把 action_id 存入 session
            message_builder = MessageBuilder(session, motivation=action_params.get("motivation"))
            await message_builder.process_steps(action_params.get("steps", []))

            # 从 session 中获取本轮发送的所有 action_id
            sent_action_ids = session.sent_action_ids_this_turn
            if sent_action_ids:
                logger.debug(
                    f"[{current_conv_id}] 准备为 {len(sent_action_ids)} 个动作等待回声: "
                    f"{sent_action_ids}"
                )

                # 创建等待所有回声的任务
                wait_tasks = [session.wait_for_echo(action_id) for action_id in sent_action_ids]
                results = await asyncio.gather(*wait_tasks)  # 阻塞在这里，直到所有回声都收到或超时

                logger.debug(f"[{current_conv_id}] 回声等待结束，收到的结果: {results}")
                success_count = results.count(True)
                if success_count == len(sent_action_ids):
                    logger.success(
                        f"[{current_conv_id}] 所有 {len(sent_action_ids)} 条消息的回声均已收到。"
                    )
                    # 触发下一轮思考
                    logger.info(f"[{current_conv_id}] 消息已全部发送完毕，立即触发下一轮思考。")
                    core_logic.trigger_immediate_thought_cycle()
                else:
                    logger.warning(
                        f"[{current_conv_id}] {len(sent_action_ids) - success_count} "
                        f"/ {len(sent_action_ids)} 条消息的回声等待超时。"
                    )

            # send_message 处理完毕，无论是否超时，都继续处理意识控制指令（如果有）

        # --- 策略B: 处理其他所有“即做即走”的动作 ---
        else:
            logger.info(f"检测到 [即做即走类] 动作 ({platform_key}.{action_name})。")
            # 这些动作不需要等待，ActionHandler会处理它们，并把结果写回思想点
            await action_handler.process_action_flow(
                action_id=source_action_id,
                doc_key_for_updates=source_thought_key,
                action_json=action_payload,
            )

    # --- 2. 接着处理意识控制指令 (Consciousness Control) ---
    if control_payload:
        logger.info("处理 [意识控制] 指令。")
        await focus_manager.handle_consciousness_control(control_payload)

    # --- 3. 如果没有任何指令 ---
    if not action_payload and not control_payload:
        logger.info("本轮决策中无任何有效动作或意识控制指令。")

    logger.info("决策分发处理完毕。")
