# 文件路径: src/os/ui_dispatcher.py

import time
from typing import TYPE_CHECKING

from src.common.custom_logging.logging_config import get_logger
from src.os.apps.interfaces import IApp
from src.os.models import Window, WindowStatus

if TYPE_CHECKING:
    from src.bootstrap.container import ServiceContainer
    from src.os.application_manager import ApplicationManager
    from src.os.window_manager import WindowManager

logger = get_logger(__name__)


async def handle_os_interaction(
    aicos_interaction: dict, ui_mapping: dict, container: "ServiceContainer", thought_key: str
) -> None:
    """统一处理所有与 AIC-OS GUI 相关的交互.

    这是 OS 层的唯一交互入口.
    """
    window_manager = container.window_manager
    application_manager = container.application_manager

    # 1. 解析基础UI交互 (click, double_click, input_override)
    if base_interaction := aicos_interaction.get("base"):
        action_name = next(iter(base_interaction), None)
        if not action_name:
            return
        action_params = base_interaction[action_name]

        # 对 input_override 的处理
        if action_name in ["click", "double_click"]:
            await _handle_base_ui_interaction(
                action_name,
                action_params,
                ui_mapping,
                window_manager,
                application_manager,
                container,
            )
        elif action_name == "input_override":
            await _handle_input_override_action(
                action_params, ui_mapping, application_manager, container, thought_key
            )
    else:
        # --- 将所有非 'base' 的动作分发给对应的 Builder ---
        app_name = next(iter(aicos_interaction), None)
        if not app_name:
            return

        builder = application_manager.get_builder_by_name(app_name)
        if not builder:
            logger.warning(f"UI Dispatcher: 收到未知应用的动作请求: {app_name}")
            return

        # params 现在是 aicos_interaction[app_name] 下的所有动作
        # 例如 {"send_message": {...}}
        action_definitions = aicos_interaction[app_name]
        action_name = next(iter(action_definitions), None)
        if not action_name:
            return

        params = action_definitions[action_name]
        logger.info(f"UI Dispatcher: 路由应用动作 '{app_name}.{action_name}'")
        # 将真正的 action_name 传给 builder
        await builder.handle_llm_action(action_name, params, container, thought_key)


async def _handle_input_override_action(
    params: dict,
    ui_mapping: dict,
    application_manager: "ApplicationManager",
    container: "ServiceContainer",
    thought_key: str,
) -> None:
    """处理通用的 input_override 动作，并将其分发给对应的 App Builder."""
    target_id = params.get("target_id")
    content = params.get("content")

    if not target_id or content is None or target_id not in ui_mapping:
        logger.error(f"AI 试图向无效的输入框 '{target_id}' 输入内容。行动忽略。")
        return

    # 从语义化ID中解析出应用名称
    try:
        app_name = target_id.split('.')[0]
    except IndexError:
        logger.error(f"无法从 target_id '{target_id}' 中解析出应用名称。")
        return

    builder = application_manager.get_builder_by_name(app_name)
    if not builder:
        logger.warning(f"UI Dispatcher: 收到未知应用的 input_override 请求: {app_name}")
        return

    logger.info(f"UI Dispatcher: 路由 input_override 动作到 App '{app_name}'")
    await builder.handle_input_override(target_id, content, container, thought_key)


async def _handle_base_ui_interaction(
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

    # --- 文件和窗口打开逻辑 ---
    if internal_command == "open_folder":
        # 获取单例窗口
        fe_window = window_manager.get_window("file_explorer_main")
        if not fe_window:  # 如果不存在，则启动应用来创建它
            await _start_app_and_get_window(
                "app-file-explorer", application_manager, window_manager, container
            )
            fe_window = window_manager.get_window("file_explorer_main")

        if fe_window:
            # 更新路径状态并聚焦
            try:
                _type, user_path = target_uid.split(":", 1)
                fe_window.content_state["current_path"] = user_path
                window_manager.focus_window(fe_window.name)
                logger.info(f"导航到文件夹: {user_path}")
            except ValueError:
                logger.error(f"无效的文件夹 item_id: {target_uid}")

    elif internal_command == "open_file":
        editor_window = window_manager.get_window("text_editor_main")
        if not editor_window:
            await _start_app_and_get_window(
                "app-text-editor", application_manager, window_manager, container
            )
            editor_window = window_manager.get_window("text_editor_main")

        if editor_window:
            try:
                _type, user_path = target_uid.split(":", 1)
                file_name = user_path.split('/')[-1]

                # 检查是否已在tab中
                tabs = editor_window.content_state.setdefault("tabs", [])
                if any(tab['item_id'] == target_uid for tab in tabs):
                    # 如果已存在，则仅切换
                    editor_window.content_state["active_tab_id"] = target_uid
                else:
                    # 如果不存在，则添加新tab并切换
                    tabs.append({"item_id": target_uid, "path": user_path, "name": file_name})
                    editor_window.content_state["active_tab_id"] = target_uid

                window_manager.focus_window(editor_window.name)
                logger.info(f"已在文本编辑器中打开或切换到文件: {user_path}")
            except (ValueError, KeyError):
                logger.error(f"打开文件失败，无效的 item_id 或窗口状态: {target_uid}")

    elif internal_command == "view_tab":
        editor_window = window_manager.get_window("text_editor_main")
        if editor_window:
            editor_window.content_state["active_tab_id"] = target_uid
            window_manager.focus_window("text_editor_main")

    elif internal_command == "close_tab":
        editor_window = window_manager.get_window("text_editor_main")
        if editor_window:
            tabs = editor_window.content_state.get("tabs", [])
            # 移除标签页
            editor_window.content_state["tabs"] = [
                t for t in tabs if t.get("item_id") != target_uid
            ]
            # 如果关闭的是当前激活的标签页，则激活列表中的最后一个（如果还有的话）
            if editor_window.content_state.get("active_tab_id") == target_uid:
                remaining_tabs = editor_window.content_state["tabs"]
                if remaining_tabs:
                    editor_window.content_state["active_tab_id"] = remaining_tabs[-1].get("item_id")
                else:
                    editor_window.content_state["active_tab_id"] = None
            window_manager.focus_window("text_editor_main")

    # 处理侧边栏切换
    elif internal_command == "toggle_sidebar":
        window = window_manager.get_window(target_uid)
        if window:
            current_state = window.content_state.get("sidebar_visible", False)
            window.content_state["sidebar_visible"] = not current_state
            logger.info(f"窗口 '{target_uid}' 的侧边栏状态已切换为: {not current_state}")
            window_manager.focus_window(target_uid)

    # --- 其他UI交互逻辑 ---
    elif internal_command == "disconnect_device":
        container.aicos_state_generator.is_connected = False
        logger.info("设备 AIC-OS 已断开。")

    elif internal_command == "kill_process":
        application_manager.stop_app(target_uid)
        logger.info(f"应用进程 '{target_uid}' 已被终止。")
        windows_to_close = [
            w for w in window_manager.get_all_windows_sorted()
            if w.parent_app_id == target_uid
            and not w.is_popup
        ]
        for window in windows_to_close:
            window_manager.close_window(window.name)
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

            # 如果关闭的是会话窗口，则停用会话以更新时间戳
            if closed_window.window_class == "conversation":
                conversation_uid = closed_window.content_state.get("conversation_uid")
                app = application_manager.get_app_by_id(app_id)
                # 检查 container 中是否有 qq_chat_session_manager
                session_manager = getattr(container, "qq_chat_session_manager", None)
                if conversation_uid and app and session_manager:
                    logger.info(f"正在为已关闭的窗口停用会话: {conversation_uid}")
                    await session_manager.deactivate_session(conversation_uid)

            # 只有在关闭的不是弹窗时，才检查是否需要关闭应用
            if not closed_window.is_popup:
                # 检查主窗口时也要排除弹窗
                app_windows = [
                    w for w in window_manager.get_all_windows_sorted()
                    if w.parent_app_id == app_id and not w.is_popup
                ]
                if not app_windows:
                    application_manager.stop_app(app_id)
                    logger.info(f"应用 '{app_id}' 所有主窗口已关闭，进程已停止。")

    elif internal_command == "switch_window_view":
        window = window_manager.get_window(target_uid)
        new_view = mapped_info.get("view_name")
        if window and new_view:
            window.content_state["view"] = new_view
            logger.info(f"窗口 '{target_uid}' 的视图已切换到 '{new_view}'。")
            window_manager.focus_window(target_uid)

    elif internal_command == "toggle_collapsible_list":
        window = window_manager.get_window(target_uid)
        list_name = mapped_info.get("list_name")
        if window and list_name:
            list_states = window.content_state.setdefault("collapsible_lists", {})
            current_state = list_states.get(list_name, "collapsed")
            list_states[list_name] = "expanded" if current_state == "collapsed" else "collapsed"
            logger.info(f"窗口 '{target_uid}' 中的列表 '{list_name}' 状态已切换。")
            window_manager.focus_window(target_uid)

    elif internal_command == "paginate_collapsible_list":
        window = window_manager.get_window(target_uid)
        list_name = mapped_info.get("list_name")
        direction = mapped_info.get("direction")
        if window and list_name and direction:
            list_pages = window.content_state.setdefault("list_pages", {})
            current_page = list_pages.get(list_name, 1)
            # 总页数应该由渲染器在渲染时计算并存入 content_state
            total_pages = window.content_state.get("list_total_pages", {}).get(list_name, 1)

            if direction == "next":
                list_pages[list_name] = min(total_pages, current_page + 1)
            elif direction == "prev":
                list_pages[list_name] = max(1, current_page - 1)
            logger.info(f"窗口 '{target_uid}' 中列表 '{list_name}' 已翻页。")
            window_manager.focus_window(target_uid)

    elif internal_command == "start_app":
        await _start_app_and_get_window(target_uid, application_manager, window_manager, container)

    elif internal_command == "open_conversation_window":
        # 在打开窗口前，立即将会话标记为已读
        logger.info(f"进入会话 '{target_uid}'，立即将其标记为已读。")
        await container.entity_graph_service.update_conversation_last_read_timestamp(
            target_uid, time.time() * 1000
        )

        # 这里的逻辑也应该更通用
        platform_id = target_uid.split("_")[0]
        app = next((a for a in application_manager.get_all_apps() if a.name == platform_id), None)

        if not app:
            logger.error(f"无法打开会话窗口：找不到负责平台 '{platform_id}' 的应用。")
            return

        builder = application_manager.get_builder_by_name(platform_id)
        if not builder or not isinstance(builder, IApp):
            logger.error(f"严重错误：平台 '{platform_id}' 的构建器未实现 IApp 接口。")
            return

        session = await builder.get_session(target_uid, container)

        if session:
            if not application_manager.is_running(app.id):
                application_manager.start_app(app.id)

            window_name = f"conv_{target_uid}"
            conv_window = Window(
                name=window_name,
                parent_app_id=app.id,
                title=f"与 {session.conversation_name} 的对话",
                window_class="conversation",
                content_state={
                    "conversation_uid": target_uid,
                    "sidebar_visible": False
                }, # 侧边栏默认关闭
            )
            window_manager.open_window(conv_window)

    # [核心修正] 处理所有未被识别的、应用专属的指令
    else:
        try:
            # 约定：所有应用专属指令的 target_id 格式为 app_name.something...
            app_name = target_id.split('.')[0]
            builder = application_manager.get_builder_by_name(app_name)
            if builder:
                logger.info(
                    f"UI Dispatcher: 路由应用专属 UI 指令 '{internal_command}' 到 App '{app_name}'"
                )
                await builder.handle_ui_command(internal_command, target_uid, container)
            else:
                logger.warning(
                    f"收到一个未知的内部指令 '{internal_command}' "
                    f"且找不到对应的 App Builder。"
                )
        except IndexError:
            logger.warning(
                f"收到一个未知的内部指令 '{internal_command}'，"
                f"且其 target_id '{target_id}' 格式不规范。"
            )


async def _start_app_and_get_window(
    app_id: str,
    application_manager: "ApplicationManager",
    window_manager: "WindowManager",
    container: "ServiceContainer",
) -> Window | None:
    """一个辅助函数，用于启动应用并打开其主窗口."""
    app = application_manager.get_app_by_id(app_id)
    if not app:
        logger.error(f"尝试启动一个不存在的应用: '{app_id}'")
        return None

    builder = application_manager.get_builder_by_name(app.name)
    if not builder:
        logger.error(f"应用 '{app.title}' 没有找到构建器，无法启动。")
        return None

    can_start, error_message = await builder.on_before_start(container)
    if not can_start:
        logger.error(f"应用 '{app.title}' 启动前检查失败: {error_message}")
        error_popup = Window(
            name=f"win-error-startup-{app.id}",
            parent_app_id=app.id,
            title=f"{app.title} - 启动失败",
            window_class="system_error_modal",
            content_state={"error_message": error_message or "发生未知启动错误。"},
            is_popup=True,
            popup_type="modal",
        )
        window_manager.open_window(error_popup)
        return None

    application_manager.start_app(app_id)
    logger.info(f"应用 '{app.title}' 已启动。")
    main_window = await builder.on_after_start(container, app.id)
    window_manager.open_window(main_window)
    return main_window
