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

    # 1. 解析基础UI交互 (click, double_click)
    if base_interaction := aicos_interaction.get("base"):
        action_name = next(iter(base_interaction), None)
        if not action_name:
            return
        action_params = base_interaction[action_name]
        if action_name in ["click", "double_click"]:
            await _handle_base_ui_interaction(
                action_name,
                action_params,
                ui_mapping,
                window_manager,
                application_manager,
                container,
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

        params = aicos_interaction[app_name]
        logger.info(f"UI Dispatcher: 路由应用动作 '{app_name}'")
        await builder.handle_llm_action(app_name, params, container, thought_key)


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


    # --- 其他UI交互逻辑 ---
    elif internal_command == "disconnect_device":
        container.aicos_state_generator.is_connected = False
        logger.info("设备 AIC-OS 已断开。")

    elif internal_command == "kill_process":
        application_manager.stop_app(target_uid)
        logger.info(f"应用进程 '{target_uid}' 已被终止。")
        windows_to_close = [
            w for w in window_manager.get_all_windows_sorted() if w.parent_app_id == target_uid
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

            app_windows = [
                w for w in window_manager.get_all_windows_sorted() if w.parent_app_id == app_id
            ]
            if not app_windows and not closed_window.is_popup:
                application_manager.stop_app(app_id)
                logger.info(f"应用 '{app_id}' 所有窗口已关闭，进程已停止。")

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

    elif internal_command == "open_folder":
        window = window_manager.get_window(mapped_info.get("window_name"))
        if window:
            window.content_state["current_path"] = target_uid
            logger.info(f"导航到文件夹: {target_uid}")
            window_manager.focus_window(window.name)

    elif internal_command == "open_file":
        try:
            file_content = container.file_system_manager.read_file(target_uid)

            # 从路径中提取文件名
            file_name = target_uid.split('/')[-1]

            # 创建一个新的窗口实例来显示文件内容
            new_window = Window(
                name=f"editor_{target_uid.replace('/', '_').replace('.', '_')}",
                parent_app_id="app-text-editor",
                title=file_name,
                window_class="text_editor",
                content_state={"path": target_uid, "content": file_content},
            )
            window_manager.open_window(new_window)
            logger.info(f"已在新的文本编辑器窗口中打开文件: {target_uid}")

        except FileNotFoundError:
            logger.error(f"打开文件失败：文件 '{target_uid}' 未找到。")
            # 可选：在这里创建一个错误弹窗通知用户
        except Exception as e:
            logger.error(f"打开文件时发生未知错误 ({target_uid}): {e}", exc_info=True)
            # 可选：在这里创建一个错误弹窗通知用户

    elif internal_command == "delete_item":
        fs_manager = container.file_system_manager
        if fs_manager.delete(target_uid):
            logger.info(f"已删除项目: {target_uid}")
        else:
            logger.error(f"删除项目失败: {target_uid}")

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
                content_state={"conversation_uid": target_uid},
            )
            window_manager.open_window(conv_window)
        else:
            logger.error(f"无法为 '{target_uid}' 创建会话窗口，获取会话失败。")

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
