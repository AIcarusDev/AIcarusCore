# src/aicos/state_generator.py
import re
from xml.dom.minidom import parseString
from xml.etree.ElementTree import Element, SubElement, tostring

from src.apps.registry import platform_builder_registry
from src.common.custom_logging.logging_config import get_logger

# 导入核心依赖
from src.services.database.services.entity_graph_service import EntityGraphService
from src.services.database.services.event_storage_service import EventStorageService

from .application_manager import ApplicationManager
from .models import Window, WindowStatus
from .window_manager import WindowManager

logger = get_logger(__name__)


class AICOSStateGenerator:
    """负责将 AIC-OS 的内部状态渲染成最终的 XML 字符串.

    并生成 UI 元素到内部实体的映射。
    使用基于稳定 `name` 的、带编码的、确定性语义化ID生成方案。
    """

    def __init__(
        self,
        window_manager: WindowManager,
        application_manager: ApplicationManager,
        entity_service: EntityGraphService,
        event_service: EventStorageService,
    ) -> None:
        self.window_manager = window_manager
        self.application_manager = application_manager
        self.entity_service = entity_service
        self.event_service = event_service
        self._ui_mapping: dict[str, dict] = {}
        self.is_connected = False
        # 预编译正则表达式以提高性能
        self._invalid_id_chars_pattern = re.compile(r"[^a-zA-Z0-9_\-]")

    def _encode_id_part(self, part: str) -> str:
        """对ID路径的单个部分进行编码，确保其对XML ID有效."""
        # 1. 替换点号，因为我们用它做分隔符
        encoded_part = part.replace(".", "__dot__")
        # 2. 替换所有其他非法字符为下划线
        encoded_part = self._invalid_id_chars_pattern.sub("_", encoded_part)
        return encoded_part

    def _generate_semantic_id(self, path_parts: list[str]) -> str:
        """[新核心] 根据语义路径列表生成一个确定性的、编码过的UI ID."""
        encoded_parts = [self._encode_id_part(part) for part in path_parts]
        return ".".join(encoded_parts)

    def _pretty_print_xml(self, element: Element) -> str:
        """将 ElementTree 元素格式化为带缩进的 XML 字符串."""
        rough_string = tostring(element, "utf-8")
        reparsed = parseString(rough_string)
        return reparsed.toprettyxml(indent="  ", encoding="utf-8").decode()

    async def build_current_state(self) -> tuple[str, dict[str, dict]]:
        """构建当前状态的 XML 和 UI 映射."""
        self._ui_mapping = {}

        if not self.is_connected:
            return self._render_disconnected_state()

        root = Element("AIC-OS", attrib={"connection": "connected", "lifecycle": "running"})
        SubElement(root, "desc").text = "欢迎来到Aic-OS。一个为AI交互设计的轻量级操作系统。"

        self._render_softwares(root)

        base_path = ["aicos"]
        self._render_background_processes(root, [*base_path, "task_manager"])
        await self._render_desktop(root, [*base_path, "desktop"])

        xml_string = self._pretty_print_xml(root)
        return xml_string, self._ui_mapping

    def _render_disconnected_state(self) -> tuple[str, dict[str, dict]]:
        """渲染未连接状态的界面."""
        root = Element("AIC-OS", attrib={"connection": "disconnected", "lifecycle": "stopped"})
        SubElement(root, "desc").text = "你尚未连接到你的设备。使用 'connect' 动作来接入 AIC-OS。"

        connect_id = self._generate_semantic_id(["aicos", "connect_button"])
        SubElement(
            root, "button", attrib={"id": connect_id, "name": "connect", "title": "连接设备"}
        )
        self._ui_mapping[connect_id] = {
            "action_type": "click",
            "action": "connect_device",
            "target_uid": "aicos-main",
        }
        return self._pretty_print_xml(root), self._ui_mapping

    def _render_softwares(self, parent_element: Element) -> None:
        """渲染 <softwares> 块，代表所有已安装的应用."""
        softwares_node = SubElement(parent_element, "softwares")
        utilities_node = SubElement(softwares_node, "utilities")
        SubElement(utilities_node, "utility", id="uti-001", name="task_manager", title="任务管理器")
        SubElement(
            utilities_node, "utility", id="uti-002", name="file_explorer", title="资源管理器"
        )
        applications_node = SubElement(softwares_node, "applications")
        SubElement(applications_node, "application", id="app-001", name="qq", title="QQ")

    def _render_background_processes(
        self, parent_element: Element, current_path: list[str]
    ) -> None:
        """渲染后台进程列表，并为非核心进程添加关闭按钮."""
        bg_processes_node = SubElement(
            parent_element,
            "background_processes",
            attrib={"name": "background_processes", "parent": "uti-001"},
        )

        core_processes = {"task_manager", "file_explorer"}

        # 渲染核心进程 (无交互)
        SubElement(
            bg_processes_node, "process", attrib={"name": "task_manager", "title": "任务管理器"}
        )
        SubElement(
            bg_processes_node, "process", attrib={"name": "file_explorer", "title": "资源管理器"}
        )

        for app in self.application_manager.get_running_apps():
            if app.name in core_processes:
                continue

            proc_path = [*current_path, app.name]
            proc_node = SubElement(
                bg_processes_node, "process", attrib={"name": app.name, "title": app.title}
            )

            kill_btn_id = self._generate_semantic_id([*proc_path, "terminate_button"])
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

    async def _render_desktop(self, parent_element: Element, current_path: list[str]) -> None:
        """渲染桌面，包括快捷方式、窗口和系统托盘."""
        desktop_node = SubElement(
            parent_element, "desktop", attrib={"name": "desktop", "parent": "uti-002"}
        )
        is_desktop_visible = not any(
            w.status == WindowStatus.MAXIMIZE for w in self.window_manager.get_all_windows_sorted()
        )

        if is_desktop_visible:
            items_node = SubElement(desktop_node, "items", attrib={"name": "items"})
            qq_app_id = "app-001"
            qq_shortcut_path = [*current_path, "shortcut_qq"]
            qq_shortcut_id = self._generate_semantic_id(qq_shortcut_path)
            SubElement(
                items_node,
                "shortcut",
                attrib={"id": qq_shortcut_id, "target_id": qq_app_id, "name": "qq", "title": "QQ"},
            )
            self._ui_mapping[qq_shortcut_id] = {
                "action_type": "double_click",
                "action": "start_app",
                "target_uid": qq_app_id,
            }

        windows_node = SubElement(desktop_node, "windows", attrib={"name": "windows"})
        for window in self.window_manager.get_all_windows_sorted():
            # 为每个窗口创建一个基于其稳定ID的路径
            window_path = [*current_path, "window_" + self._encode_id_part(window.id)]
            await self._render_window_frame(windows_node, window_path, window)

        # 在桌面渲染系统托盘和断开连接按钮
        system_tray_node = SubElement(desktop_node, "system_tray", attrib={"name": "system_tray"})
        disconnect_path = [*current_path, "system_tray", "disconnect_button"]
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
        self, parent_element: Element, current_path: list[str], window: Window
    ) -> None:
        """此方法负责渲染窗口的通用外框和控件，内容部分委托给应用渲染器."""
        window_node = SubElement(
            parent_element,
            "window",
            attrib={
                "id": window.id,
                "parent": window.parent_app_id,
                "class": window.window_class,
                "title": window.title,
                "status": window.status.value,
            },
        )

        controls_node = SubElement(window_node, "controls")
        controls_path = [*current_path, "controls"]

        if window.status == WindowStatus.MINIMIZE:
            restore_btn_id = self._generate_semantic_id([*controls_path, "restore_button"])
            SubElement(
                controls_node,
                "button",
                attrib={"id": restore_btn_id, "name": "restore_down", "title": "还原"},
            )
            self._ui_mapping[restore_btn_id] = {
                "action_type": "click",
                "action": "restore_window",
                "target_uid": window.id,
            }
        else:
            minimize_btn_id = self._generate_semantic_id([*controls_path, "minimize_button"])
            SubElement(
                controls_node,
                "button",
                attrib={"id": minimize_btn_id, "name": "minimize", "title": "最小化"},
            )
            self._ui_mapping[minimize_btn_id] = {
                "action_type": "click",
                "action": "minimize_window",
                "target_uid": window.id,
            }

        if window.status == WindowStatus.NORMAL:
            maximize_btn_id = self._generate_semantic_id([*controls_path, "maximize_button"])
            SubElement(
                controls_node,
                "button",
                attrib={"id": maximize_btn_id, "name": "maximize", "title": "最大化"},
            )
            self._ui_mapping[maximize_btn_id] = {
                "action_type": "click",
                "action": "maximize_window",
                "target_uid": window.id,
            }

        close_btn_id = self._generate_semantic_id([*controls_path, "close_button"])
        SubElement(
            controls_node, "button", attrib={"id": close_btn_id, "name": "close", "title": "关闭"}
        )
        self._ui_mapping[close_btn_id] = {
            "action_type": "click",
            "action": "close_window",
            "target_uid": window.id,
        }

        # 委托渲染
        if window.status != WindowStatus.MINIMIZE:
            app = next(
                (
                    a
                    for a in self.application_manager.get_all_apps()
                    if a.id == window.parent_app_id
                ),
                None,
            )
            if app and (builder := platform_builder_registry.get_builder(app.name)):
                await builder.render_window_content(
                    parent_element=window_node,
                    current_path=current_path,
                    window=window,
                    bot_ids_map=self.application_manager.get_self_bot_ids_map(),
                    entity_service=self.entity_service,
                    event_service=self.event_service,
                    ui_mapping=self._ui_mapping,
                    generate_semantic_id=self._generate_semantic_id,
                )
            else:
                app_name = app.name if app else "未知"
                message = f"应用 '{app_name}' 没有提供内容渲染器。"
                SubElement(window_node, "content").text = message
