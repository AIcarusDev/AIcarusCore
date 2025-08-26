# src/core_logic/decision_dispatcher.py
from typing import TYPE_CHECKING

from src.aicos.models import Window, WindowStatus
from src.common.custom_logging.logging_config import get_logger
from src.common.utils import parse_entity_uid

if TYPE_CHECKING:
    from src.action.action_handler import ActionHandler
    from src.aicos.application_manager import ApplicationManager
    from src.aicos.state_generator import AICOSStateGenerator
    from src.aicos.window_manager import WindowManager
    from src.core_logic.state_manager import AIStateManager
    from src.focus_chat_mode.chat_session_manager import ChatSessionManager

logger = get_logger(__name__)


async def process_aicos_decision(
    decision_json: dict,
    ui_mapping: dict[str, dict],
    window_manager: "WindowManager",
    application_manager: "ApplicationManager",
    action_handler: "ActionHandler",
    chat_session_manager: "ChatSessionManager",
    state_manager: "AIStateManager",
    aicos_state_generator: "AICOSStateGenerator",
) -> None:
    """统一的、基于 GUI 隐喻的 LLM 决策分发器."""
    if not decision_json or not isinstance(decision_json, dict):
        return

    logger.info(f"AIC-OS 决策分发器处理决策: {decision_json}")

    current_internal_state = decision_json.get("internal_state", {})

    # --- 1. 优先处理内部动作 --
    if internal_action := decision_json.get("internal_action"):
        await _handle_internal_action(
            internal_action, state_manager, chat_session_manager, current_internal_state
        )

    # --- 2. 处理外部动作 ---
    if external_action := decision_json.get("external_action"):
        action_name = next(iter(external_action), None)
        if not action_name:
            return
        action_params = external_action[action_name]

        if action_name in ["click", "double_click"]:
            await _handle_ui_interaction(
                action_name,
                action_params,
                ui_mapping,
                window_manager,
                application_manager,
                chat_session_manager,
                aicos_state_generator,
            )

        elif action_name == "send_message":
            await _handle_send_message(
                action_params, window_manager, action_handler, chat_session_manager
            )


async def _handle_ui_interaction(
    action_name: str,
    params: dict,
    ui_mapping: dict,
    window_manager: "WindowManager",
    application_manager: "ApplicationManager",
    chat_session_manager: "ChatSessionManager",
    aicos_state_generator: "AICOSStateGenerator",
) -> None:
    """处理所有低阶 UI 交互动作 (click, double_click)."""
    target_id = params.get("target_id")
    if not target_id or target_id not in ui_mapping:
        logger.error(f"AI 试图操作无效UI元素: '{target_id}'。行动忽略。")
        return

    mapped_info = ui_mapping[target_id]
    internal_command = mapped_info.get("action")
    target_uid = mapped_info.get("target_uid")

    logger.info(
        f"UI操作: '{action_name}({target_id})' -> 内部指令: '{internal_command}({target_uid})'"
    )
    if internal_command == "connect_device":
        aicos_state_generator.is_connected = True
        logger.info("设备 AIC-OS 已连接。")
    elif internal_command == "disconnect_device":
        aicos_state_generator.is_connected = False
        logger.info("设备 AIC-OS 已断开。")

    elif internal_command == "kill_process":
        # 1. 停止应用进程
        application_manager.stop_app(target_uid)
        logger.info(f"应用进程 '{target_uid}' 已被终止。")

        # 2. 找到并关闭该应用的所有窗口
        windows_to_close = [
            w for w in window_manager.get_all_windows_sorted() if w.parent_app_id == target_uid
        ]
        for window in windows_to_close:
            window_manager.close_window(window.id)
        logger.info(f"已关闭属于应用 '{target_uid}' 的 {len(windows_to_close)} 个窗口。")

    if internal_command == "scroll_chat_window":
        direction = mapped_info.get("direction")
        window = window_manager.get_window(target_uid)
        if window and direction:
            current_page = window.content_state.get("page", 1)
            total_pages = window.content_state.get("total_pages", 1)

            if direction == "up":
                new_page = max(1, current_page - 1)
            elif direction == "down":
                new_page = min(total_pages, current_page + 1)
            else:
                new_page = current_page

            if new_page != current_page:
                window.content_state["page"] = new_page
                logger.info(f"窗口 '{target_uid}' 已滚动至第 {new_page} 页。")
                # 聚焦窗口以更新其 last_focused_timestamp
                window_manager.focus_window(target_uid)
        else:
            logger.warning(f"滚动操作失败：找不到窗口 '{target_uid}' 或缺少方向。")

    # --- 内部指令路由 ---
    if internal_command == "minimize_window":
        window_manager.set_window_status(target_uid, WindowStatus.MINIMIZE)
    elif internal_command == "maximize_window":
        window_manager.set_window_status(target_uid, WindowStatus.MAXIMIZE)
    elif internal_command == "restore_window":
        window_manager.set_window_status(target_uid, WindowStatus.NORMAL)
    elif internal_command == "close_window":
        closed_window = window_manager.get_window(target_uid)
        if closed_window:
            window_manager.close_window(target_uid)
            app_id = closed_window.parent_app_id
            app_windows = [
                w for w in window_manager.get_all_windows_sorted() if w.parent_app_id == app_id
            ]
            if not app_windows:
                application_manager.stop_app(app_id)
                logger.info(f"应用 '{app_id}' 所有窗口已关闭，进程已停止。")

    elif internal_command == "start_app":
        if application_manager.is_running(target_uid):
            logger.warning(f"应用 '{target_uid}' 已在运行中，将聚焦其窗口。")
            # 查找该应用已有的窗口并聚焦
            app_windows = [
                w for w in window_manager.get_all_windows_sorted() if w.parent_app_id == target_uid
            ]
            if app_windows:
                window_manager.focus_window(app_windows[0].id)
        else:
            application_manager.start_app(target_uid)
            logger.info(f"应用 '{target_uid}' 已启动。")

            # --- [新核心逻辑] ---
            # 根据应用ID，创建并打开其主窗口
            app = next((a for a in application_manager.get_all_apps() if a.id == target_uid), None)
            if app and app.name == "qq":
                main_window = Window(
                    id=f"win-{app.id}-main",
                    parent_app_id=app.id,
                    title=f"{app.title}",
                    window_class="main/conversation_list",
                )
                window_manager.open_window(main_window)
            # ... else if for other apps ...

    elif internal_command == "open_conversation_window":
        app_id = "app-001"  # 硬编码QQ的应用ID
        if not application_manager.is_running(app_id):
            application_manager.start_app(app_id)

        session = await chat_session_manager.get_or_create_session(target_uid)
        if session:
            conv_window = Window(
                id=f"win-conv-{target_uid.replace('_', '-')}",  # 创建唯一的窗口ID
                parent_app_id=app_id,
                title=f"与 {session.conversation_name} 的对话",
                window_class="conversation",
                content_state={"conversation_uid": target_uid},
            )
            window_manager.open_window(conv_window)
        else:
            logger.error(f"无法为 '{target_uid}' 创建会话窗口，获取会话失败。")


async def _handle_send_message(
    params: dict,
    window_manager: "WindowManager",
    action_handler: "ActionHandler",
    chat_session_manager: "ChatSessionManager",
) -> None:
    """[新核心逻辑] 处理高阶的 send_message 动作."""
    target_window_id = params.get("target_window_id")
    steps = params.get("steps")
    motivation = params.get("motivation", "由AIC-OS MessageBuilder发起")

    # 1. --- 验证输入 ---
    if not target_window_id or not steps:
        logger.error("send_message 指令缺少 target_window_id 或 steps。")
        return

    window = window_manager.get_window(target_window_id)
    if not window:
        logger.error(f"AI 试图向一个不存在的窗口 '{target_window_id}' 发送消息。")
        return
    if window.window_class != "conversation":
        logger.error(
            f"AI 试图向一个非聊天窗口 '{target_window_id}' (类型: {window.window_class}) 发送消息。"
        )
        return
    if window.status == WindowStatus.MINIMIZE:
        logger.error(f"AI 试图向一个已最小化的窗口 '{target_window_id}' 发送消息。")
        return

    # 2. --- 提取持久化ID和上下文信息 ---
    conversation_uid = window.content_state.get("conversation_uid")
    if not conversation_uid:
        logger.error(f"窗口 '{target_window_id}' 缺少 conversation_uid 状态，无法确定发送目标。")
        return

    parsed_info = parse_entity_uid(conversation_uid)
    if not parsed_info:
        logger.error(f"无法从持久化ID '{conversation_uid}' 中解析出平台、类型和原生ID。")
        return

    platform, conv_type, native_id = parsed_info

    # 从 ChatSessionManager 获取执行动作所需的 bot_id
    bot_id = chat_session_manager.self_bot_ids_map.get(platform)
    if not bot_id:
        logger.error(f"无法为平台 '{platform}' 找到对应的 bot_id，无法执行发送动作。")
        return

    # 3. --- 构建 ActionHandler 所需的参数 ---
    # ActionHandler 的 execute_simple_action 需要一个特定的参数结构
    action_params_for_handler = {
        "conversation_id": native_id,
        "conversation_type": conv_type,
        "content": steps,  # steps 的格式与 ActionHandler 期望的 content 格式完全兼容
    }

    logger.info(f"准备通过 ActionHandler 发送消息至会话 '{conversation_uid}' (原生ID: {native_id})")

    # 4. --- 调用 ActionHandler 执行 ---
    action_result = await action_handler.execute_simple_action(
        platform_id=platform,
        action_name="send_message",
        params=action_params_for_handler,
        bot_id=bot_id,
        description="由 AIC-OS 发送",
        motivation=motivation,
    )

    # 5. --- 处理结果 ---
    if action_result.is_success:
        logger.info(f"消息已成功发送至会话 '{conversation_uid}'。回执: {action_result.payload}")
        # 消息发送成功后，可以聚焦该窗口，模拟人类发送完消息后的行为
        window_manager.focus_window(target_window_id)
    else:
        logger.error(f"消息发送至会话 '{conversation_uid}' 失败: {action_result.error_message}")
        # TODO: 未来可以将这个错误信息反馈给 AI


async def _handle_internal_action(
    internal_action: dict,
    state_manager: "AIStateManager",
    chat_session_manager: "ChatSessionManager",
    current_internal_state: dict,
) -> None:
    """处理所有内部动作 (deep_think, manage_goals)."""
    action_name = next(iter(internal_action), None)
    if not action_name:
        return
    params = internal_action[action_name]

    logger.info(f"处理内部动作: '{action_name}'")

    if action_name == "manage_goals":
        goal_manager = state_manager.goal_manager
        if not goal_manager:
            logger.error("GoalManager 未初始化，无法处理 manage_goals 动作。")
            return

        if add_params := params.get("add"):
            goals_to_add = add_params.get("goals", [])
            await goal_manager.add_goals(goals_to_add)
            logger.info(f"已通过内部动作添加 {len(goals_to_add)} 个新目标。")

        if remove_params := params.get("remove"):
            ids_to_remove = remove_params.get("goal_ids", [])
            await goal_manager.remove_goals(ids_to_remove)
            logger.info(f"已通过内部动作移除 {len(ids_to_remove)} 个目标。")

    elif action_name == "deep_think":
        if not chat_session_manager or not chat_session_manager.deliberation_service:
            logger.error("DeliberationService 未初始化，无法处理 deep_think 动作。")
            return

        # 注意：deep_think 的结果 (新的 internal_state) 会在下一轮思考的
        # ThoughtPersistor 中被保存，从而影响后续的 <history_internal_info>。
        # 这里我们只负责触发它，而不直接改变当前循环的状态。

        # TODO: _extract_context_from_ui 应该返回 session，这里暂时为 None
        session = None

        await chat_session_manager.deliberation_service.execute(
            pipeline_params=params, current_internal_state=current_internal_state, session=session
        )
