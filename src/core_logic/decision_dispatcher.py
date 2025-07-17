# D:\Aic\AIcarusCore\src\core_logic\decision_dispatcher.py

from typing import TYPE_CHECKING

from src.common.custom_logging.logging_config import get_logger

if TYPE_CHECKING:
    from src.action.action_handler import ActionHandler
    from src.focus_chat_mode.chat_session_manager import ChatSessionManager

logger = get_logger(__name__)


async def process_llm_decision(
    decision_json: dict,
    focus_manager: "ChatSessionManager",
    action_handler: "ActionHandler",
    # 我们还需要知道这个决策来自哪里，以便更新对应的思考文档
    source_thought_key: str | None = None,
    source_action_id: str | None = None,
) -> None:
    """一个统一的LLM决策分发器.

    它像一个交通警察，负责解析LLM的完整决策，并将不同类型的指令分发给正确的处理器.
    目前支持两种类型的指令：
    1. 意识控制指令 (consciousness_control): 决定AI的“注意力”要去哪里.
    2. 外部行动指令 (action): 决定AI要“做什么”.

    Args:
        decision_json: 包含了LLM决策的完整JSON对象.
        focus_manager: ChatSessionManager的实例，用于处理意识控制指令.
        action_handler: ActionHandler的实例，用于处理外部行动指令.
        source_thought_key: (可选) 产生这个决策的思考文档的_key.
        source_action_id: (可选) 产生这个决策的思考文档关联的action_id.

    Returns:
        None
    这个函数会根据决策的内容，调用不同的处理器来执行相应的操作.
    如果决策中包含意识控制指令，它会交给 FocusManager 处理.
    如果包含外部行动指令，它会交给 ActionHandler 处理.
    如果决策格式不正确或为空，它会记录警告日志并返回.
    """
    if not decision_json or not isinstance(decision_json, dict):
        logger.warning("收到的LLM决策为空或非字典格式，无法分发。")
        return

    logger.info(f"决策分发器开始处理LLM决策: {decision_json}")

    action_payload = decision_json.get("action")
    control_payload = decision_json.get("consciousness_control")

    # --- 识别动作类型 ---
    action_category = "none"
    action_details = {}

    if action_payload:
        if action_payload.get("core", {}).get("web_search"):
            action_category = "generic_with_result"
            action_details = {"name": "web_search", "params": action_payload["core"]["web_search"]}
        elif action_payload.get("napcat_qq", {}).get("get_list"):
            action_category = "level_restricted_with_result"
            action_details = {"name": "get_list", "params": action_payload["napcat_qq"]["get_list"]}
        elif action_payload: # 其他所有动作（如 send_message, do_nothing）都归为此类
            action_category = "do_and_go"

    # --- 根据动作类型和意识控制的存在，执行不同策略 ---

    # 策略 1: 处理【泛用有结果类】动作 (如 web_search)
    if action_category == "generic_with_result":
        logger.info("检测到 [泛用有结果类] 动作 (web_search)，执行'先取结果'策略。")

        # 1a. 先执行动作，拿到结果
        search_result_text = await action_handler._execute_core_web_search(action_details["params"])

        # 1b. 将结果保存到原始思想点
        await action_handler.thought_storage_service.save_action_result_to_thought(
            thought_key=source_thought_key,
            result_text=search_result_text
        )

        # 1c. 如果同时有意识控制，将结果作为“行李”传递
        if control_payload:
            logger.info("检测到意识控制，将携带搜索结果进行注意力转移。")
            command, params = next(iter(control_payload.items()))
            params["_handover_action_result"] = {
                "action_name": "web_search",
                "result_text": search_result_text
            }
            await focus_manager.handle_consciousness_control(control_payload)
        else:
            # 如果没有意识控制，就地触发思考
            if action_handler.thought_trigger:
                action_handler.thought_trigger.set()

    # 策略 2: 处理【层级限定有结果类】动作 (如 get_list)
    elif action_category == "level_restricted_with_result":
        if control_payload:
            logger.warning(
                f"检测到 [层级限定动作({action_details['name']})] 与 [意识控制] 冲突。"
                "将优先执行动作，忽略意识控制指令。"
            )
        # 无论有无冲突，都只执行动作
        await action_handler.process_action_flow(
            action_id=source_action_id,
            doc_key_for_updates=source_thought_key,
            action_json=action_payload,
        )

    # 策略 3: 处理【即做即走类】动作 (如 send_message)
    elif action_category == "do_and_go":
        logger.info("检测到 [即做即走类] 动作。")
        # 3a. 先把动作丢给 ActionHandler，它内部会用 create_task 非阻塞执行
        await action_handler.process_action_flow(
            action_id=source_action_id,
            doc_key_for_updates=source_thought_key,
            action_json=action_payload,
        )
        # 3b. 然后，如果存在意识控制，立即执行
        if control_payload:
            logger.info("在执行'即做即走'动作后，立即执行意识控制。")
            await focus_manager.handle_consciousness_control(control_payload)

    # 策略 4: 只存在意识控制，没有动作
    elif control_payload:
        logger.info("只检测到 [意识控制] 指令。")
        await focus_manager.handle_consciousness_control(control_payload)

    else:
        logger.info("本轮决策中无任何有效动作或意识控制指令。")

    logger.info("决策分发处理完毕。")
