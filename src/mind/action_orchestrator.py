# 文件路径: src/mind/action_orchestrator.py

import uuid
from typing import TYPE_CHECKING
from xml.etree.ElementTree import Element, SubElement, tostring

from src.common.custom_logging.logging_config import get_logger
from src.domain.models import ActionMetadata
from src.os.ui_dispatcher import handle_os_interaction

if TYPE_CHECKING:
    from src.bootstrap.container import ServiceContainer


logger = get_logger(__name__)


def _format_resolution_for_memory(resolution: dict) -> str:
    """将慢思考的决议字典格式化为适合存入 action_result 的 XML 字符串."""
    root = Element("deliberation_result")

    summary = SubElement(root, "summary")
    summary.text = resolution.get("summary", "无总结。")

    final_state = SubElement(root, "final_internal_state")
    mood = SubElement(final_state, "mood")
    mood.text = resolution.get("final_mood", "平静")
    think = SubElement(final_state, "think")
    think.text = resolution.get("final_think", "...")
    intent = SubElement(final_state, "intent")
    intent.text = resolution.get("final_intent", "无")

    return tostring(root, encoding='unicode')


async def orchestrate_action(
    decision_json: dict | None,
    ui_mapping: dict,
    container: "ServiceContainer",
    thought_key: str,
    external_info_snapshot: str | None,
) -> None:
    """动作总编排器.

    Mind 和 Action/OS 之间的桥梁，负责解析LLM的决策并分发到正确的处理器。
    """
    if not decision_json or not isinstance(decision_json, dict):
        return

    logger.info(f"动作编排器开始处理决策 (源自 Thought: {thought_key}): {decision_json}")

    # 1. 解析动作负载
    if action_payload := decision_json.get("action"):
        # 2. 路由内部动作
        if internal_action := action_payload.get("internal"):
            await _route_internal_action(
                internal_action, container, thought_key, external_info_snapshot
            )

        # 3. 路由外部动作
        if external_action := action_payload.get("external"):
            await _route_external_action(external_action, ui_mapping, container)


async def _route_internal_action(
    internal_action: dict,
    container: "ServiceContainer",
    thought_key: str,
    external_info_snapshot: str | None
) -> None:
    """将内部动作路由到对应的 Mind 层服务执行."""
    action_name = next(iter(internal_action), None)
    if not action_name:
        return

    params = internal_action[action_name]
    logger.info(f"编排内部动作: '{action_name}'")

    if action_name == "manage_goals":
        goal_manager = container.goal_manager
        if add_params := params.get("add"):
            await goal_manager.add_goals(add_params.get("goals", []))
        if remove_params := params.get("remove"):
            await goal_manager.remove_goals(remove_params.get("goal_ids", []))

    elif action_name == "deep_think":
        resolution = await container.deliberation_service.execute(
            pipeline_params=params,
            container=container,
            external_info_snapshot=external_info_snapshot,
        )
        if resolution:
            result_str = _format_resolution_for_memory(resolution)
            await container.thought_storage_service.save_action_result_to_thought(
                thought_key=thought_key, result_text=result_str
            )
            container.core_logic.trigger_immediate_thought_cycle()
    else:
        logger.warning(f"接收到未知的内部动作: {action_name}")


async def _route_external_action(
    external_action: dict,
    ui_mapping: dict,
    container: "ServiceContainer"
) -> None:
    """将外部动作路由到对应的 Service 或 OS 层处理器."""
    action_handler = container.action_handler
    aicos_state_generator = container.aicos_state_generator

    # 分发 "innate" (固有能力) 动作
    if innate_action := external_action.get("innate"):
        action_name = next(iter(innate_action), None)
        if not action_name:
            return
        action_params = innate_action[action_name]

        # 特殊处理 connect, 因为它直接改变OS状态
        if action_name == "connect" and action_params.get("device_name") == "AIC-OS":
            aicos_state_generator.is_connected = True
            logger.info("设备 AIC-OS 已连接。")
            return

        # 其他固有能力(文件、搜索)是需要异步等待结果的，交给 ActionHandler 处理
        motivation = action_params.get("motivation", "由 AI 核心决策发起")
        temp_thought_id = f"thought_for_{action_name}_{uuid.uuid4().hex[:6]}"

        # ActionHandler 的 process_action_flow 现在是处理这类动作的专家
        action_json_for_handler = {"core": {action_name: action_params}}
        await action_handler.process_action_flow(
            action_id=f"action_{uuid.uuid4().hex[:6]}",
            doc_key_for_updates=temp_thought_id,
            action_json=action_json_for_handler,
            metadata=ActionMetadata(motivation=motivation),
        )

    # 分发所有 AIC-OS 的 UI 交互到 UI Dispatcher
    elif aicos_interaction := external_action.get("AIC-OS"):
        await handle_os_interaction(aicos_interaction, ui_mapping, container)
