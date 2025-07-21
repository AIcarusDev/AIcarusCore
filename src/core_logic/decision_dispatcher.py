# src\core_logic\decision_dispatcher.py
import asyncio
from typing import TYPE_CHECKING

from src.action.components.message_builder import MessageBuilder
from src.common.custom_logging.logging_config import get_logger
from src.common.utils import parse_focus_path

if TYPE_CHECKING:
    from src.action.action_handler import ActionHandler
    from src.focus_chat_mode.chat_session_manager import ChatSessionManager

logger = get_logger(__name__)


async def process_llm_decision(
    decision_json: dict,
    focus_manager: "ChatSessionManager",
    action_handler: "ActionHandler",
    source_thought_key: str | None = None,
    source_action_id: str | None = None,
    current_focus_path: str | None = None,
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
        current_focus_path: (可选) 当前的焦点路径，用于解析上下文.

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

    action_category = "none"
    action_details = {}

    # 1. 解析当前上下文
    current_level, current_platform_id, _ = parse_focus_path(current_focus_path)

    if action_payload and isinstance(action_payload, dict):
        # 2. 优先检查核心动作，它们是无上下文的
        if web_search_params := action_payload.get("web_search"):
            action_category = "generic_with_result"
            action_details = {"name": "web_search", "params": web_search_params}
            # 帮 LLM 把动作修正为标准格式，以便下游处理
            action_payload = {"core": {"web_search": web_search_params}}

        # 3. 检查平台专属动作
        elif current_platform_id != "core":
            if get_list_params := action_payload.get("get_list"):
                action_category = "level_restricted_with_result"
                action_details = {"name": "get_list", "params": get_list_params}
                # 帮 LLM 把动作修正为标准格式
                action_payload = {current_platform_id: {"get_list": get_list_params}}

            elif send_message_params := action_payload.get("send_message"):
                action_category = "echoic"
                action_details = {"name": "send_message", "params": send_message_params}
                # 帮 LLM 把动作修正为标准格式
                action_payload = {current_platform_id: {"send_message": send_message_params}}

            # ... 未来其他平台动作的 elif 放在这里 ...

        # 4. 如果以上都不是，才归为“即做即走”
        if action_category == "none" and action_payload:
            action_category = "do_and_go"
            # 对于即做即走类，也尝试帮它修正格式
            if not action_payload.get("core") and not action_payload.get(current_platform_id):
                _first_action_name = next(iter(action_payload))
                if current_platform_id != "core":
                    action_payload = {current_platform_id: action_payload}
                else:  # 如果在 core 层，但不是已知的 core 动作，也归到 core 下
                    action_payload = {"core": action_payload}

    # --- 根据动作类型和意识控制的存在，执行不同策略 ---

    # 策略 1: 处理【泛用有结果类】动作 (如 web_search)
    if action_category == "generic_with_result":
        logger.info("检测到 [泛用有结果类] 动作 (web_search)，执行'先取结果'策略。")
        search_result_text = await action_handler._execute_core_web_search(action_details["params"])
        await action_handler.thought_storage_service.save_action_result_to_thought(
            thought_key=source_thought_key, result_text=search_result_text
        )
        if control_payload:
            logger.info("检测到意识控制，将携带搜索结果进行注意力转移。")
            command, params = next(iter(control_payload.items()))
            params["_handover_action_result"] = {
                "action_name": "web_search",
                "result_text": search_result_text,
            }
            await focus_manager.handle_consciousness_control(control_payload)
        else:
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

    # 策略 3: 处理【即做即走类】动作 (如 poke)
    elif action_category == "do_and_go":
        logger.info("检测到 [即做即走类] 动作。")
        await action_handler.process_action_flow(
            action_id=source_action_id,
            doc_key_for_updates=source_thought_key,
            action_json=action_payload,
        )
        if control_payload:
            logger.info("在执行'即做即走'动作后，立即执行意识控制。")
            await focus_manager.handle_consciousness_control(control_payload)

    elif action_category == "echoic":
        logger.info("检测到 [回声类] 动作 (send_message)，将等待回声后触发思考。")

        # a. 确认我们在底层会话中
        path_parts = focus_manager.current_focus_path.split(".")
        if len(path_parts) < 2:
            logger.error("回声类动作只能在底层会话中执行！")
            return
        conv_id = path_parts[-1]
        session = focus_manager.sessions.get(conv_id)
        if not session:
            logger.error(f"找不到会话 {conv_id}，无法执行 send_message。")
            return

        # b. 如果同时有意识控制，这是矛盾行为，优先执行回声动作
        if control_payload:
            logger.warning(
                f"检测到 [回声类动作({action_details['name']})] 与 [意识控制] 冲突。"
                "将优先执行动作并等待回声，忽略意识控制指令。"
            )

        # c. 直接调用 MessageBuilder，让它在后台发送消息并返回 action_ids
        message_builder = MessageBuilder(
            session, motivation=action_details["params"].get("motivation")
        )
        sent_action_ids = await message_builder.process_steps(
            action_details["params"].get("steps", [])
        )

        # d. 等待所有消息的回声
        if sent_action_ids:
            wait_tasks = [session.wait_for_echo(action_id) for action_id in sent_action_ids]
            results = await asyncio.gather(*wait_tasks)
            if all(results):
                logger.success(f"所有 {len(sent_action_ids)} 条消息的回声均已收到。")
            else:
                logger.warning(
                    f"{results.count(False)} / {len(sent_action_ids)} 条消息的回声等待超时。"
                )

        # e. 无论是否超时，都触发下一轮思考
        if action_handler.thought_trigger:
            action_handler.thought_trigger.set()

    # 策略 4: 只存在意识控制，没有动作
    elif control_payload:
        logger.info("只检测到 [意识控制] 指令。")
        await focus_manager.handle_consciousness_control(control_payload)

    else:
        logger.info("本轮决策中无任何有效动作或意识控制指令。")

    logger.info("决策分发处理完毕。")
