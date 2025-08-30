# 文件路径: src/os/decision_dispatcher.py

import uuid
from typing import TYPE_CHECKING

from src.common.custom_logging.logging_config import get_logger
from src.domain.models import ActionMetadata
from src.os.apps.interfaces import IApp
from src.os.apps.registry import platform_builder_registry
from src.os.models import Window, WindowStatus

if TYPE_CHECKING:
    from src.bootstrap.container import ServiceContainer
    from src.os.application_manager import ApplicationManager
    from src.os.window_manager import WindowManager


logger = get_logger(__name__)


async def process_aicos_decision(
    decision_json: dict | None,
    ui_mapping: dict,
    container: "ServiceContainer",
) -> None:
    """统一的、纯粹的LLM决策路由器.

    它的唯一职责是根据决策JSON的结构，将任务路由到正确的服务或处理器。
    """
    if not decision_json or not isinstance(decision_json, dict):
        return

    logger.info(f"AIC-OS 决策路由器处理决策: {decision_json}")

    # 查找统一的 'action' 键
    if action_payload := decision_json.get("action"):
        if internal_action := action_payload.get("internal"):
            await _route_internal_action(internal_action, container)

        if external_action := action_payload.get("external"):
            await _route_external_action(external_action, ui_mapping, container)


async def _route_internal_action(internal_action: dict, container: "ServiceContainer") -> None:
    """将内部动作路由到对应的服务执行."""
    action_name = next(iter(internal_action), None)
    if not action_name:
        return

    params = internal_action[action_name]
    logger.info(f"路由内部动作: '{action_name}'")

    if action_name == "manage_goals":
        goal_manager = container.goal_manager
        if add_params := params.get("add"):
            await goal_manager.add_goals(add_params.get("goals", []))
        if remove_params := params.get("remove"):
            await goal_manager.remove_goals(remove_params.get("goal_ids", []))

    elif action_name == "deep_think":
        # deep_think 的所有执行逻辑和副作用（如更新会话记忆）都已封装在其服务内部
        await container.deliberation_service.execute(
            pipeline_params=params,
            container=container,  # 传递容器，让服务自己获取上下文
        )
    else:
        logger.warning(f"接收到未知的内部动作: {action_name}")


async def _route_external_action(
    external_action: dict, ui_mapping: dict, container: "ServiceContainer"
) -> None:
    """将外部动作路由到对应的处理器."""
    action_handler = container.action_handler
    aicos_state_generator = container.aicos_state_generator

    # 解析 Innate (先天) 动作
    if innate_action := external_action.get("innate"):
        action_name = next(iter(innate_action), None)
        if not action_name:
            return
        action_params = innate_action[action_name]

        if action_name == "connect":
            if action_params.get("device_name") == "AIC-OS":
                aicos_state_generator.is_connected = True
                logger.info("设备 AIC-OS 已连接。")
        else:
            # 所有其他的 innate 动作 (文件操作、网页搜索等) 都由 ActionHandler 统一处理
            motivation = action_params.get("motivation", "由 AI 核心决策发起")
            temp_thought_id = f"thought_for_{action_name}_{uuid.uuid4().hex[:6]}"
            # 这里的 "core" 命名空间是历史遗留，代表了不属于任何特定平台的“核心”能力
            action_json_for_handler = {"core": {action_name: action_params}}
            await action_handler.process_action_flow(
                action_id=f"action_{uuid.uuid4().hex[:6]}",
                doc_key_for_updates=temp_thought_id,
                action_json=action_json_for_handler,
                metadata=ActionMetadata(motivation=motivation),
            )
    # 解析 AIC-OS 交互动作
    elif aicos_interaction := external_action.get("AIC-OS"):
        await _handle_aicos_interaction(aicos_interaction, ui_mapping, container)


async def _handle_aicos_interaction(
    aicos_interaction: dict, ui_mapping: dict, container: "ServiceContainer"
) -> None:
    """处理所有 AIC-OS 相关的交互动作."""
    window_manager = container.window_manager
    application_manager = container.application_manager

    # 解析基础UI交互
    if base_interaction := aicos_interaction.get("base"):
        action_name = next(iter(base_interaction), None)
        if not action_name:
            return
        action_params = base_interaction[action_name]
        if action_name in ["click", "double_click"]:
            # [职责不变] UI 交互是OS内核的一部分，由路由器直接处理是合理的
            await _handle_ui_interaction(
                action_name,
                action_params,
                ui_mapping,
                window_manager,
                application_manager,
                container,
            )

    # 解析特定应用的交互 (例如 qq)
    else:
        for platform_id, platform_action in aicos_interaction.items():
            if platform_id == "base":
                continue

            action_name = next(iter(platform_action), None)
            if not action_name:
                continue

            params = platform_action[action_name]

            logger.info(f"路由平台GUI动作 '{platform_id}.{action_name}' 到 ActionHandler")
            await container.action_handler.handle_aicos_gui_action(
                platform_id, action_name, params, window_manager
            )
            break # 决策中只有一个平台动作，处理完第一个就可以退出


async def _handle_ui_interaction(
    action_name: str,
    params: dict,
    ui_mapping: dict,
    window_manager: "WindowManager",
    application_manager: "ApplicationManager",
    container: "ServiceContainer",
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

    if internal_command == "disconnect_device":
        container.aicos_state_generator.is_connected = False
        logger.info("设备 AIC-OS 已断开。")

    elif internal_command == "kill_process":
        application_manager.stop_app(target_uid)
        logger.info(f"应用进程 '{target_uid}' 已被终止。")
        windows_to_close = [
            w for w in window_manager.get_all_windows_sorted() if w.parent_app_id == target_uid
        ]
        for window in windows_to_close:
            window_manager.close_window(window.id)
        logger.info(f"已关闭属于应用 '{target_uid}' 的 {len(windows_to_close)} 个窗口。")

    elif internal_command == "scroll_chat_window":
        direction = mapped_info.get("direction")
        window = window_manager.get_window(target_uid)
        if window and direction:
            current_page = window.content_state.get("page", 1)
            total_pages = window.content_state.get("total_pages", 1)
            new_page = (
                max(1, current_page - 1)
                if direction == "up"
                else min(total_pages, current_page + 1)
                if direction == "down"
                else current_page
            )
            if new_page != current_page:
                window.content_state["page"] = new_page
                logger.info(f"窗口 '{target_uid}' 已滚动至第 {new_page} 页。")
                window_manager.focus_window(target_uid)
        else:
            logger.warning(f"滚动操作失败：找不到窗口 '{target_uid}' 或缺少方向。")

    elif internal_command == "minimize_window":
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
        app_windows = [
            w for w in window_manager.get_all_windows_sorted() if w.parent_app_id == target_uid
        ]
        if application_manager.is_running(target_uid) and app_windows:
            logger.warning(f"应用 '{target_uid}' 已在运行中，将聚焦其窗口。")
            window_manager.focus_window(app_windows[0].id)
        else:
            application_manager.start_app(target_uid)
            logger.info(f"应用 '{target_uid}' 已启动。")
            app = next((a for a in application_manager.get_all_apps() if a.id == target_uid), None)
            if app and app.name == "qq":
                main_window = Window(
                    id=f"win-{app.id}-main",
                    parent_app_id=app.id,
                    title=f"{app.title}",
                    window_class="main/conversation_list",
                )
                window_manager.open_window(main_window)

    elif internal_command == "open_conversation_window":
        # 通用化打开会话窗口的逻辑
        app_list = application_manager.get_all_apps()
        # 从会话UID中解析出平台ID
        platform_id = target_uid.split("_")[0]
        # 找到这个平台对应的APP
        app = next((a for a in app_list if a.name == platform_id), None)

        if not app:
            logger.error(f"无法打开会话窗口：找不到负责平台 '{platform_id}' 的应用。")
            return

        builder = platform_builder_registry.get_builder(platform_id)
        if not builder or not isinstance(builder, IApp):
            logger.error(f"严重错误：平台 '{platform_id}' 的构建器未实现 IApp 接口。")
            return

        session = await builder.get_session(target_uid, container)

        if session:
            if not application_manager.is_running(app.id):
                application_manager.start_app(app.id)

            conv_window = Window(
                id=f"win-conv-{target_uid.replace('_', '-')}",
                parent_app_id=app.id,
                title=f"与 {session.conversation_name} 的对话", # ISession 需要有 conversation_name
                window_class="conversation",
                content_state={"conversation_uid": target_uid},
            )
            window_manager.open_window(conv_window)
        else:
            logger.error(f"无法为 '{target_uid}' 创建会话窗口，获取会话失败。")
