# 文件路径: src/os/state_generator.py

import re
from xml.dom.minidom import parseString
from xml.etree.ElementTree import Element, SubElement, tostring

from src.common.custom_logging.logging_config import get_logger
from src.services.action.action_handler import ActionHandler
from src.services.database.services.entity_graph_service import EntityGraphService
from src.services.database.services.event_storage_service import EventStorageService
from src.services.database.services.media_cache_service import MediaCacheService

from .application_manager import ApplicationManager
from .file_system_manager import FileSystemManager
from .models import Window, WindowStatus
from .window_manager import WindowManager

logger = get_logger(__name__)


class AICOSStateGenerator:
    """负责将 AIC-OS 的内部状态渲染成最终的 XML 字符串.

    并生成 UI 元素到内部实体的映射。
    使用基于稳定 `name` 的、带编码的、确定性语义化ID生成方案。
    支持模态弹窗的渲染劫持。
    """

    def __init__(
        self,
        window_manager: WindowManager,
        application_manager: ApplicationManager,
        entity_service: EntityGraphService,
        event_service: EventStorageService,
        media_cache_service: MediaCacheService,
        action_handler: ActionHandler,
        file_system_manager: FileSystemManager, # 注入
    ) -> None:
        self.window_manager = window_manager
        self.application_manager = application_manager
        self.entity_service = entity_service
        self.event_service = event_service
        self.media_cache_service = media_cache_service
        self.action_handler = action_handler
        self.file_system_manager = file_system_manager # 存储
        self._ui_mapping: dict[str, dict] = {}
        self.is_connected = False
        # 预编译正则表达式以提高性能
        self._invalid_id_chars_pattern = re.compile(r"[^a-zA-Z0-9_\-]")

    def _generate_semantic_id(self, path_parts: list[str]) -> str:
        """根据语义路径列表生成一个确定性的UI ID."""
        # 直接用点连接，因为我们现在使用安全的UID
        return ".".join(path_parts)

    def _pretty_print_xml(self, element: Element) -> str:
        """将 ElementTree 元素格式化为带缩进的 XML 字符串."""
        rough_string = tostring(element, "utf-8")
        reparsed = parseString(rough_string)
        return reparsed.toprettyxml(indent="  ", encoding="utf-8").decode()

    async def build_current_state(
        self, image_collector: list[dict] | None = None
    ) -> tuple[str, dict[str, dict]]:
        """构建当前状态的 XML 和 UI 映射."""
        self._ui_mapping = {}
        image_collector = image_collector if image_collector is not None else []

        # 窗老化逻辑
        self.window_manager.age_transient_popups()

        if not self.is_connected:
            return self._render_disconnected_state()

        root = Element("AIC-OS", attrib={"connection": "connected", "lifecycle": "running"})
        SubElement(root, "desc").text = "欢迎来到AIc-OS。一个为AI交互设计的轻量级操作系统。"

        # 模态弹窗检查
        active_modal = self.window_manager.get_active_modal_popup()
        if active_modal:
            logger.info(f"检测到模态弹窗 '{active_modal.name}'，执行劫持渲染。")
            desktop_node = SubElement(
                root,
                "desktop",
                attrib={"name": "desktop", "status": "modal_lock"},
            )
            windows_node = SubElement(desktop_node, "windows", attrib={"name": "windows"})
            modal_path = ["aicos", "desktop", "modal_" + active_modal.name]
            await self._render_window_frame(windows_node, modal_path, active_modal, image_collector)
        else:
            # 正常渲染
            self._render_softwares(root)
            self._render_background_processes(root)
            await self._render_desktop(root, image_collector)

        xml_string = self._pretty_print_xml(root)
        return xml_string, self._ui_mapping

    def _render_disconnected_state(self) -> tuple[str, dict[str, dict]]:
        """渲染未连接状态的界面."""
        root = Element("AIC-OS", attrib={"connection": "disconnected", "lifecycle": "stopped"})
        SubElement(
            root, "desc"
        ).text = "你尚未连接到你的设备。可以使用 'connect' 动作来接入 AIC-OS。"

        return self._pretty_print_xml(root), self._ui_mapping

    def _render_softwares(self, parent_element: Element) -> None:
        """渲染 <softwares> 块，代表所有已安装的应用."""
        softwares_node = SubElement(parent_element, "softwares")

        # --- 区分 utilities 和 applications ---
        utilities_node = SubElement(softwares_node, "utilities")
        applications_node = SubElement(softwares_node, "applications")

        # 从 ApplicationManager 动态获取应用列表
        # 假设 Application 定义中有一个 is_utility 标志
        for app in self.application_manager.get_all_apps():
            target_node = utilities_node if app.is_utility else applications_node
            node_type = "utility" if app.is_utility else "application"
            SubElement(target_node, node_type, id=app.id, name=app.name, title=app.title)


    def _render_background_processes(
        self,
        parent_element: Element,
    ) -> None:
        """渲染后台进程列表，并为非核心进程添加关闭按钮."""
        bg_processes_node = SubElement(
            parent_element,
            "background_processes",
            attrib={"name": "background_processes"},
        )

        # 渲染所有正在运行的应用作为进程
        for app in self.application_manager.get_running_apps():
            proc_path = [app.name]
            proc_node = SubElement(
                bg_processes_node, "process", attrib={"name": app.name, "title": app.title}
            )

            # 系统工具通常不允许被关闭
            if not app.is_utility:
                kill_btn_id = self._generate_semantic_id([*proc_path, "terminate"])
                SubElement(
                    proc_node,
                    "button",
                    attrib={"id": kill_btn_id, "name": "terminate_process", "title": "结束进程"},
                )
                self._ui_mapping[kill_btn_id] = {
                    "action_type": "click",
                    "action": "kill_process",
                    "target_uid": app.id,
                }


    async def _render_desktop(self, parent_element: Element, image_collector: list[dict]) -> None:
        """渲染桌面，包括快捷方式、窗口和系统托盘."""
        desktop_node = SubElement(parent_element, "desktop")
        is_desktop_visible = not any(
            w.status == WindowStatus.MAXIMIZE for w in self.window_manager.get_all_windows_sorted()
        )

        if is_desktop_visible:
            items_node = SubElement(desktop_node, "items")
            # 动态渲染所有应用的快捷方式
            for app in self.application_manager.get_all_apps():
                # 系统工具通常没有快捷方式
                if app.is_utility:
                    continue
                shortcut_path = [f"shortcut_{app.name}"]
                shortcut_id = self._generate_semantic_id(shortcut_path)
                SubElement(
                    items_node,
                    "shortcut",
                    attrib={
                        "id": shortcut_id,
                        "target_id": app.id,
                        "name": app.name,
                        "title": app.title,
                    },
                )
                self._ui_mapping[shortcut_id] = {
                    "action_type": "double_click",
                    "action": "start_app",
                    "target_uid": app.id,
                }

            # --- 渲染桌面上的物理文件和文件夹 ---
            desktop_contents = self.file_system_manager.list_directory_contents("/desktop")
            if desktop_contents:
                for item in desktop_contents:
                    SubElement(
                        items_node, item.type, attrib={"name": item.name, "item_id": item.item_id}
                    )
                    # 为桌面上的每个项目创建双击打开的映射
                    action = "open_folder" if item.type == "folder" else "open_file"
                    self._ui_mapping[item.item_id] = {
                        "action_type": "double_click",
                        "action": action,
                        "target_uid": item.item_id, # target_uid 就是 item_id
                    }

        windows_node = SubElement(desktop_node, "windows")
        for window in self.window_manager.get_all_windows_sorted():
            # 为每个窗口创建一个基于其稳定ID的路径
            window_path = [window.name]
            await self._render_window_frame(windows_node, window_path, window, image_collector)

        # 在桌面渲染系统托盘和断开连接按钮
        system_tray_node = SubElement(desktop_node, "system_tray", attrib={"name": "system_tray"})
        disconnect_path = ["system_tray", "disconnect"]
        disconnect_btn_id = self._generate_semantic_id(disconnect_path)
        SubElement(
            system_tray_node,
            "button",
            attrib={"id": disconnect_btn_id, "name": "disconnect", "title": "断开与设备的连接"},
        )
        self._ui_mapping[disconnect_btn_id] = {
            "action_type": "click",
            "action": "disconnect_device",
            "target_uid": "aicos-main",
        }

    async def _render_window_frame(
        self,
        parent_element: Element,
        current_path: list[str],
        window: Window,
        image_collector: list[dict],
    ) -> None:
        """此方法负责渲染窗口的通用外框和控件，内容部分委托给应用渲染器."""
        # 弹窗属性的渲染
        window_attrs = {
            "name": window.name,
            "parent_app": window.parent_app_id,
            "title": window.title,
            "status": window.status.value,
        }
        if window.is_popup:
            window_attrs["class"] = "popup"  # 覆盖或设置为 popup
            if window.popup_type:
                window_attrs["popup-type"] = window.popup_type

        window_node = SubElement(parent_element, "window", attrib=window_attrs)

        controls_node = SubElement(window_node, "controls")
        controls_path = [*current_path, "controls"]

        if window.status == WindowStatus.MINIMIZE:
            restore_btn_id = self._generate_semantic_id([*controls_path, "restore"])
            SubElement(
                controls_node,
                "button",
                attrib={"id": restore_btn_id, "name": "restore_down", "title": "还原"},
            )
            self._ui_mapping[restore_btn_id] = {
                "action_type": "click",
                "action": "restore_window",
                "target_uid": window.name,
            }
        else:
            minimize_btn_id = self._generate_semantic_id([*controls_path, "minimize"])
            SubElement(
                controls_node,
                "button",
                attrib={"id": minimize_btn_id, "name": "minimize", "title": "最小化"},
            )
            self._ui_mapping[minimize_btn_id] = {
                "action_type": "click",
                "action": "minimize_window",
                "target_uid": window.name,
            }

        if window.status == WindowStatus.NORMAL:
            maximize_btn_id = self._generate_semantic_id([*controls_path, "maximize"])
            SubElement(
                controls_node,
                "button",
                attrib={"id": maximize_btn_id, "name": "maximize", "title": "最大化"},
            )
            self._ui_mapping[maximize_btn_id] = {
                "action_type": "click",
                "action": "maximize_window",
                "target_uid": window.name,
            }

        close_btn_id = self._generate_semantic_id([*controls_path, "close"])
        close_btn_title = "确认" if window.popup_type == "modal" else "关闭"
        SubElement(
            controls_node,
            "button",
            attrib={"id": close_btn_id, "name": "close", "title": close_btn_title},
        )
        self._ui_mapping[close_btn_id] = {
            "action_type": "click",
            "action": "close_window",
            "target_uid": window.name,
        }

        if window.status != WindowStatus.MINIMIZE:
            if window.window_class in ["system_error_modal", "system_notification_popup"]:
                content_node = SubElement(window_node, "content", attrib={"type": "system_message"})
                message_key = (
                    "error_message"
                    if window.window_class == "system_error_modal"
                    else "message"
                )
                content_node.text = window.content_state.get(message_key, "发生未知系统事件。")
                return  # 处理完毕，直接返回

            # 委托应用渲染器渲染内容
            app = self.application_manager.get_app_by_id(window.parent_app_id)
            builder = self.application_manager.get_builder_by_name(app.name) if app else None

            if builder:
                # 统一在一个 content 节点下渲染
                content_node = SubElement(window_node, "content")
                render_args = {
                    "parent_element": content_node,
                    "window": window,
                    "ui_mapping": self._ui_mapping,
                    "generate_semantic_id": self._generate_semantic_id,
                    "image_collector": image_collector,
                    "bot_ids_map": self.application_manager.get_self_bot_ids_map(),
                    "entity_service": self.entity_service,
                    "event_service": self.event_service,
                    "media_cache_service": self.media_cache_service,
                    "action_handler": self.action_handler,
                    "file_system_manager": self.file_system_manager,
                }
                if window.is_popup:
                    await builder.render_popup_content(**render_args)
                else:
                    await builder.render_window_content(**render_args)
            else:
                app_name = app.name if app else "未知"
                message = f"应用 '{app_name}' 没有提供内容渲染器。"
                SubElement(window_node, "content").text = message
