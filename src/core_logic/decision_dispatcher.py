# D:\Aic\AIcarusCore\src\core_logic\decision_dispatcher.py

from typing import TYPE_CHECKING, Any

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
    source_action_id: str | None = None
) -> None:
    """
    一个统一的LLM决策分发器。
    它像一个交通警察，负责解析LLM的完整决策，并将不同类型的指令分发给正确的处理器。
    目前支持两种类型的指令：
    1. 意识控制指令 (consciousness_control): 决定AI的“注意力”要去哪里。
    2. 外部行动指令 (action): 决定AI要“做什么”。
    Args:
        decision_json: 包含了LLM决策的完整JSON对象。
        focus_manager: ChatSessionManager的实例，用于处理意识控制指令。
        action_handler: ActionHandler的实例，用于处理外部行动指令。
        source_thought_key: (可选) 产生这个决策的思考文档的_key。
        source_action_id: (可选) 产生这个决策的思考文档关联的action_id。
    Returns:
        None
    这个函数会根据决策的内容，调用不同的处理器来执行相应的操作。
    如果决策中包含意识控制指令，它会交给 FocusManager 处理。
    如果包含外部行动指令，它会交给 ActionHandler 处理。
    如果决策格式不正确或为空，它会记录警告日志并返回。
    """
    if not decision_json or not isinstance(decision_json, dict):
        logger.warning("收到的LLM决策为空或非字典格式，无法分发。")
        return

    logger.info(f"决策分发器开始处理LLM决策: {decision_json}")

    action_payload = decision_json.get("action")
    consciousness_control = decision_json.get("consciousness_control")

    is_blocking_action_present = False
    if action_payload:
        # 定义哪些是阻塞性动作
        # 例如：get_list, web_search 等
        # 这些动作会阻塞意识控制的执行
        blocking_actions = {"get_list", "web_search"}
        if any(key in blocking_actions for key in action_payload.keys()):
            is_blocking_action_present = True

    # 1. 处理意识控制指令
    if consciousness_control and not is_blocking_action_present:
        # 只有在没有阻塞性动作时，才处理意识控制
        logger.debug("检测到意识控制指令，且无阻塞性动作，交由FocusManager处理。")
        await focus_manager.handle_consciousness_control(consciousness_control)
    elif consciousness_control and is_blocking_action_present:
        logger.warning(
            "决策中同时存在阻塞性动作和意识控制指令，"
            "将优先执行阻塞性动作，并忽略意识控制指令。"
        )

    # 2. 处理外部行动指令 (action)
    # 这个指令决定了AI要“做什么”。
    action_payload = decision_json.get("action")
    if action_payload and isinstance(action_payload, dict):
        logger.debug("检测到外部行动指令，交由ActionHandler处理。")
        # 注意：这里的 action_payload 就是V4.0中 'action' 字段下的整个对象
        # 例如: {"web_search": {"query": "...", "motivation": "..."}}

        # 我们需要一个 action_id，如果原始思考文档里有就用它的，没有就生成一个新的
        action_id_to_use = source_action_id or decision_json.get("thought_id")
        if not action_id_to_use:
            # 这通常不应该发生，但作为后备
            import uuid
            action_id_to_use = str(uuid.uuid4())
            logger.warning(f"决策中缺少有效的action_id，已临时生成: {action_id_to_use}")

        await action_handler.process_action_flow(
            action_id=action_id_to_use,
            doc_key_for_updates=source_thought_key,
            action_json=action_payload # 将整个action对象传过去
        )

    logger.info("决策分发处理完毕。")