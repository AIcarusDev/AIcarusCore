# src/aicos/state_generator.py
import re
import time
from xml.dom.minidom import parseString
from xml.etree.ElementTree import Element, SubElement, tostring

from src.common.custom_logging.logging_config import get_logger

# 导入核心依赖
from src.services.database.services.entity_graph_service import EntityGraphService

from .application_manager import ApplicationManager
from .models import Window, WindowStatus
from .window_manager import WindowManager

logger = get_logger(__name__)


class AICOSStateGenerator:
    """[最终版] 负责将 AIC-OS 的内部状态渲染成最终的 XML 字符串.

    并生成 UI 元素到内部实体的映射。
    使用基于稳定 `name` 的、带编码的、确定性语义化ID生成方案。
    """

    def __init__(
        self,
        window_manager: WindowManager,
        application_manager: ApplicationManager,
        entity_service: EntityGraphService,
    ) -> None:
        self.window_manager = window_manager
        self.application_manager = application_manager
        self.entity_service = entity_service
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
        """渲染窗口的通用外框和控件."""
        window_node = SubElement(
            parent_element,
            "window",
            attrib={
                "id": window.id,
                "parent": window.parent_app_id,
                "class": window.window_class,
                "title": window.title,
                "status": window.status.value,
                "name": self._encode_id_part(window.id),
            },
        )

        controls_node = SubElement(window_node, "controls", attrib={"name": "controls"})
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

        if window.status != WindowStatus.MINIMIZE:
            if window.window_class == "main/conversation_list":
                await self._render_qq_conversation_list(window_node, current_path, window)
            elif window.window_class == "conversation":
                await self._render_conversation_window(window_node, current_path, window)

    async def _render_qq_conversation_list(
        self, window_node: Element, current_path: list[str], window: Window
    ) -> None:
        """渲染QQ会话列表窗口的动态内容."""
        page = window.content_state.get("page", 1)
        page_size = 30 if window.status == WindowStatus.MAXIMIZE else 15

        bot_id = self.application_manager.get_self_bot_ids_map().get("qq")
        if not bot_id:
            logger.error("无法获取 QQ 的 bot_id，无法渲染会话列表。")
            SubElement(window_node, "error", name="error").text = "内部错误：无法获取机器人ID。"
            return

        conversations, total_pages = await self.entity_service.get_paged_conversations(
            platform_id="qq", page=page, page_size=page_size, self_bot_ids={"qq": bot_id}
        )

        list_node = SubElement(
            window_node,
            "conversation_list",
            attrib={
                "name": "conversation_list",
                "pagination": "true",
                "page_current": str(page),
                "page_total": str(total_pages),
                "items_per_page": str(page_size),
            },
        )
        list_path = [*current_path, "conversation_list"]

        if not conversations:
            SubElement(list_node, "desc", name="empty_desc").text = "没有会话。"
            return

        for conv_data in conversations:
            conv_doc = conv_data.get("conv_doc")
            if not conv_doc:
                continue

            conv_uid = conv_doc._key
            conv_name = conv_doc.details.name or "未知会话"

            conv_path = [*list_path, conv_name]
            conv_node = SubElement(
                list_node,
                "conversation",
                attrib={
                    "name": conv_name,
                    "title": conv_name,
                    "type": conv_doc.details.type,
                    "unread": str(conv_data.get("unread_count", 0)),
                },
            )

            latest_msg_text = (
                await self.entity_service.event_storage_service.get_event_text_summary(
                    conv_data.get("latest_event")
                )
            )
            SubElement(
                conv_node, "desc", name="latest_message"
            ).text = f"[最新消息]: {latest_msg_text}"

            enter_btn_id = self._generate_semantic_id([*conv_path, "enter_button"])
            SubElement(
                conv_node,
                "button",
                attrib={"id": enter_btn_id, "name": "enter", "title": "进入会话"},
            )
            self._ui_mapping[enter_btn_id] = {
                "action_type": "click",
                "action": "open_conversation_window",
                "target_uid": conv_uid,
            }

    async def _render_conversation_window(
        self, window_node: Element, current_path: list[str], window: Window
    ) -> None:
        """渲染单个聊天窗口的动态内容，包括分页的聊天记录."""
        page = window.content_state.get("page", 1)
        page_size = 30 if window.status == WindowStatus.MAXIMIZE else 15
        conversation_uid = window.content_state.get("conversation_uid")

        if not conversation_uid:
            SubElement(window_node, "error", name="error").text = "无法加载聊天记录：未指定会话ID。"
            return

        (
            messages,
            current_page,
            total_pages,
        ) = await self.entity_service.event_storage_service.get_paged_chat_history(
            conversation_uid, page, page_size
        )
        window.content_state["total_pages"] = total_pages

        list_node = SubElement(
            window_node,
            "list",
            attrib={
                "name": "chat_history",
                "pagination": "true",
                "page_current": str(current_page),
                "page_total": str(total_pages),
                "items_per_page": str(page_size),
            },
        )
        list_path = [*current_path, "chat_history"]

        if current_page > 1:
            scroll_up_id = self._generate_semantic_id([*list_path, "scroll_up_button"])
            SubElement(
                list_node,
                "button",
                attrib={"id": scroll_up_id, "name": "scroll_up", "title": "向上滚动查看更早的消息"},
            )
            self._ui_mapping[scroll_up_id] = {
                "action_type": "click",
                "action": "scroll_chat_window",
                "target_uid": window.id,
                "direction": "up",
            }

        for msg in messages:
            sender_name = msg.get("user_info", {}).get("user_nickname", "未知用户")
            timestamp = time.strftime("%H:%M:%S", time.localtime(msg.get("timestamp", 0) / 1000))
            content_text = "".join(
                [
                    s.get("data", {}).get("text", "")
                    for s in msg.get("content", [])
                    if s.get("type") == "text"
                ]
            )

            msg_node = SubElement(
                list_node,
                "div",
                attrib={
                    "class": "message",
                    "id": msg.get("event_id"),
                    "name": f"message_{msg.get('event_id')}",
                },
            )
            SubElement(msg_node, "sender", name="sender").text = sender_name
            SubElement(msg_node, "timestamp", name="timestamp").text = timestamp
            SubElement(msg_node, "content", name="content").text = (
                content_text if content_text else "[非文本消息]"
            )

        if current_page < total_pages:
            scroll_down_id = self._generate_semantic_id([*list_path, "scroll_down_button"])
            SubElement(
                list_node,
                "button",
                attrib={
                    "id": scroll_down_id,
                    "name": "scroll_down",
                    "title": "向下滚动查看最新的消息",
                },
            )
            self._ui_mapping[scroll_down_id] = {
                "action_type": "click",
                "action": "scroll_chat_window",
                "target_uid": window.id,
                "direction": "down",
            }

        action_bar_node = SubElement(window_node, "action_bar", name="action_bar")
        SubElement(
            action_bar_node, "desc", name="desc"
        ).text = "你可以使用 send_message 动作来回复。"
