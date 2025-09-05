# 文件路径: src/os/ui_dispatcher.py

from typing import TYPE_CHECKING

from src.common.custom_logging.logging_config import get_logger
from src.os.apps.interfaces import IApp
from src.os.apps.registry import platform_builder_registry
from src.os.models import Window, WindowStatus

if TYPE_CHECKING:
    from src.bootstrap.container import ServiceContainer
    from src.os.application_manager import ApplicationManager
    from src.os.window_manager import WindowManager

logger = get_logger(__name__)


async def handle_os_interaction(
    aicos_interaction: dict, ui_mapping: dict, container: "ServiceContainer"
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

    # 2. 解析特定应用的交互 (例如 qq.send_message)
    else:
        # 遍历 interaction, 找到平台ID (e.g., 'qq')
        for platform_id, platform_action in aicos_interaction.items():
            if platform_id == "base":
                continue
            action_name = next(iter(platform_action), None)
            if not action_name:
                continue
            params = platform_action[action_name]
            logger.info(f"UI Dispatcher: 路由平台GUI动作 '{platform_id}.{action_name}'")

            # 将 container 传递给 ActionHandler，让它有能力调用其他服务
            await container.action_handler.handle_aicos_gui_action(
                platform_id, action_name, params, window_manager, container
            )
            break # 一个决策只执行一个平台的动作


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

    elif internal_command == "start_app":
        app = next((a for a in application_manager.get_all_apps() if a.id == target_uid), None)
        if not app:
            logger.error(f"尝试启动一个不存在的应用: '{target_uid}'")
            return

        # --- 特殊处理 QQ 应用的启动 ---
        #TODO: 这理应由apps中的qq管理，当前暂时由这里处理
        if app.name == "qq":
            # 检查 QQ 应用是否已通过安检 (即 bot_id 是否已设置)
            if not application_manager.get_self_bot_ids_map().get("qq"):
                error_message = (
                    f"无法启动应用 '{app.title}'。\n"
                    f"原因：QQ 应用尚未完成身份安检。"
                    f"请确保 QQ 适配器已连接并成功初始化。"
                )
                logger.error(error_message.replace('\n', ' '))
                error_popup = Window(
                    name=f"win-error-startup-{app.id}",
                    parent_app_id=app.id,
                    title=f"{app.title} - 启动失败",
                    window_class="system_error_modal",
                    content_state={"error_message": error_message},
                    is_popup=True,
                    popup_type='modal',
                    transient_cycles_remaining=None,
                )
                window_manager.open_window(error_popup)
                return

            # 安检已通过，正常启动
            application_manager.start_app(target_uid)
            logger.info(f"应用 '{target_uid}' 已启动。")
            main_window = Window(
                name="qq_main",
                parent_app_id=app.id,
                title=f"{app.title}",
                window_class="main",
                content_state={"view": "conversation_list"}
            )
            window_manager.open_window(main_window)
            return

    elif internal_command == "open_conversation_window":
        app_list = application_manager.get_all_apps()
        platform_id = target_uid.split("_")[0]
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
