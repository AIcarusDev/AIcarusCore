# src\core_logic\decision_dispatcher.py
import asyncio
from typing import TYPE_CHECKING

from src.action.components.message_builder import MessageBuilder
from src.common.custom_logging.logging_config import get_logger
from src.common.utils import parse_focus_path
from src.platform_builders.registry import platform_builder_registry

if TYPE_CHECKING:
    from src.action.action_handler import ActionHandler
    from src.focus_chat_mode.chat_session_manager import ChatSessionManager

logger = get_logger(__name__)


def normalize_action_payload(action_payload: dict, current_platform_id: str) -> dict:
    """一步到位地规范化LLM返回的action_payload.

    它不依赖任何硬编码的动作列表，而是利用当前的平台上下文来确保动作被正确包裹.
    """
    if not action_payload or not isinstance(action_payload, dict):
        # 如果输入为空或格式不正确，直接返回空字典
        return {}

    # 检查是否已经是带顶键格式，例如 {"qq": {...}} 或 {"core": {...}}
    # 在当前逻辑下不可能是带顶键的格式，但是以防未来会用到
    # 我们从 platform_builder_registry 获取所有已知的平台ID
    all_platform_ids = platform_builder_registry.get_all_builders().keys()

    # 如果顶级键已经是 'core' 或一个已知的平台ID，说明格式已经是正确的，直接返回
    if any(key in all_platform_ids for key in action_payload) or "core" in action_payload:
        logger.debug(f"Action payload 已经是标准格式，无需规范化: {action_payload}")
        return action_payload

    # 如果不是带顶键的格式，说明是一个扁平的动作负载
    # 然后我们用当前的平台上下文 (current_platform_id) 来包裹它
    logger.debug(f"检测到扁平的动作负载，将使用当前平台上下文 '{current_platform_id}' 进行规范化。")

    # 示例:
    # - current_platform_id = "qq", payload = {"send_message": ...}
    #   返回: {"qq": {"send_message": ...}}
    # - current_platform_id = "core", payload = {"web_search": ...}
    #   返回: {"core": {"web_search": ...}}
    return {current_platform_id: action_payload}


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
    _, current_platform_id, _ = parse_focus_path(current_focus_path)
    action_payload = normalize_action_payload(decision_json.get("action"), current_platform_id)
    control_payload = decision_json.get("consciousness_control")
    action_category = "none"
    action_details = {}

    if action_payload and isinstance(action_payload, dict):
        platform_key = next(iter(action_payload), None)
        if platform_key and isinstance(action_payload[platform_key], dict):
            action_name = next(iter(action_payload[platform_key]), None)
            action_params = action_payload[platform_key].get(action_name) if action_name else None

            # TODO: 当动作变多时，if/elif 会变得很长
            # 可以考虑将动作的“元数据”（比如它的类别）也注册到 PlatformBuilder 中
            # 例如，在 qq_builder.py 的 get_level_actions_definitions 中，除了返回 Schema，
            # 还可以返回一个元数据字典，来定性动作的类别
            # 现在暂时使用简单的 if/elif 来判断动作类型
            if action_name == "web_search":
                action_category = "generic_with_result"
                action_details = {"name": "web_search", "params": action_params}
            elif action_name == "get_list":
                action_category = "level_restricted_with_result"
                action_details = {"name": "get_list", "params": action_params}
            elif action_name == "send_message":
                action_category = "echoic"
                action_details = {"name": "send_message", "params": action_params}
            else:
                action_category = "do_and_go"

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
            new_control_payload_with_handover = {command: params}
            await focus_manager.handle_consciousness_control(new_control_payload_with_handover)
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

        session.sent_action_ids_this_turn.clear()
        logger.debug(f"[{session.conversation_id}] 已清空上一轮的 sent_action_ids_this_turn 列表。")

        # c. 直接调用 MessageBuilder，让它在后台发送消息并返回 action_ids
        message_builder = MessageBuilder(
            session, motivation=action_details["params"].get("motivation")
        )
        # process_steps 会在后台发送消息，并将 action_id 存入 session.sent_action_ids_this_turn
        await message_builder.process_steps(action_details["params"].get("steps", []))
        # 从 session 中获取本轮发送的 action_id 列表，而不是用 process_steps 的返回值
        sent_action_ids = session.sent_action_ids_this_turn

        # d. 等待所有消息的“回声”
        if sent_action_ids:
            logger.debug(f"准备为 {len(sent_action_ids)} 个动作等待回声: {sent_action_ids}")
            wait_tasks = [session.wait_for_echo(action_id) for action_id in sent_action_ids]
            results = await asyncio.gather(*wait_tasks)
            logger.debug(f"回声等待结束，收到的结果: {results}")

            # 增加对 results 类型的检查，防止因意外返回值导致迭代错误
            if isinstance(results, list):
                success_count = results.count(True)
                if success_count == len(sent_action_ids):
                    logger.success(f"所有 {len(sent_action_ids)} 条消息的回声均已收到。")
                else:
                    logger.warning(
                        f"{len(sent_action_ids) - success_count} "
                        f"/ {len(sent_action_ids)} 条消息的回声等待超时。"
                    )
            else:
                logger.error(
                    f"wait_for_all_actions_echo 返回了非预期的类型: "
                    f"{type(results)}，内容: {results}"
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
