# src/aicos/state_generator.py
import time
from xml.dom.minidom import parseString
from xml.etree.ElementTree import Element, SubElement, tostring

# 导入核心依赖
from src.database.services.entity_graph_service import EntityGraphService

from .application_manager import ApplicationManager
from .models import Window, WindowStatus
from .window_manager import WindowManager


class AICOSStateGenerator:
    """负责将 AIC-OS 的内部状态渲染成最终的 XML 字符串，并生成 UI 元素到内部实体的映射."""

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
        self._id_counter = 0

    def _generate_ui_id(self, prefix: str) -> str:
        self._id_counter += 1
        return f"{prefix}-{self._id_counter:04d}"

    def _pretty_print_xml(self, element: Element) -> str:
        rough_string = tostring(element, "utf-8")
        reparsed = parseString(rough_string)
        return reparsed.toprettyxml(indent="  ", encoding="utf-8").decode()

    async def build_current_state(self) -> tuple[str, dict[str, dict]]:
        """构建当前状态的 XML 和 UI 映射。这是该类的主要入口点."""
        self._ui_mapping = {}
        self._id_counter = 0

        root = Element("AIC-OS", attrib={"connection": "connected", "lifecycle": "running"})
        SubElement(root, "desc").text = "欢迎来到Aic-OS。一个为AI交互设计的轻量级操作系统。"

        self._render_softwares(root)
        self._render_background_processes(root)
        await self._render_desktop(root)

        xml_string = self._pretty_print_xml(root)

        return xml_string, self._ui_mapping

    def _render_softwares(self, parent_element: Element) -> None:
        """渲染 <softwares> 块，代表所有已安装的应用."""
        softwares_node = SubElement(parent_element, "softwares")
        # TODO: 未来这部分应该从配置或数据库动态加载
        utilities_node = SubElement(softwares_node, "utilities")
        SubElement(utilities_node, "utility", id="uti-001", name="task_manager", title="任务管理器")
        SubElement(
            utilities_node, "utility", id="uti-002", name="file_explorer", title="资源管理器"
        )

        applications_node = SubElement(softwares_node, "applications")
        SubElement(applications_node, "application", id="app-001", name="qq", title="QQ")

    def _render_background_processes(self, parent_element: Element) -> None:
        bg_processes_node = SubElement(
            parent_element, "background_processes", attrib={"parent": "uti-001"}
        )
        running_apps = self.application_manager.get_running_apps()

        # 始终显示系统核心进程
        SubElement(
            bg_processes_node,
            "process",
            attrib={"id": "proc-uti-001", "name": "task_manager", "title": "任务管理器"},
        )
        SubElement(
            bg_processes_node,
            "process",
            attrib={"id": "proc-uti-002", "name": "file_explorer", "title": "资源管理器"},
        )

        for app in running_apps:
            proc_id = f"proc-{app.id}"
            SubElement(
                bg_processes_node,
                "process",
                attrib={"id": proc_id, "name": app.name, "title": app.title},
            )
            self._ui_mapping[proc_id] = {
                "action_type": "terminatable",
                "action": "stop_app",
                "target_uid": app.id,
            }

    async def _render_desktop(self, parent_element: Element) -> None:
        desktop_node = SubElement(parent_element, "desktop", attrib={"parent": "uti-002"})
        is_desktop_visible = not any(
            w.status == WindowStatus.MAXIMIZE for w in self.window_manager.get_all_windows_sorted()
        )

        if is_desktop_visible:
            items_node = SubElement(desktop_node, "items")
            qq_shortcut_id = self._generate_ui_id("desk")
            SubElement(
                items_node,
                "shortcut",
                attrib={"id": qq_shortcut_id, "target_id": "app-001", "name": "qq", "title": "QQ"},
            )
            self._ui_mapping[qq_shortcut_id] = {
                "action_type": "double_click",
                "action": "start_app",
                "target_uid": "app-001",
            }

        windows_node = SubElement(desktop_node, "windows")
        for window in self.window_manager.get_all_windows_sorted():
            await self._render_window_frame(windows_node, window)

    async def _render_window_frame(self, parent_element: Element, window: Window) -> None:
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
        if window.status == WindowStatus.MINIMIZE:
            restore_btn_id = self._generate_ui_id("btn")
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
            minimize_btn_id = self._generate_ui_id("btn")
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
            maximize_btn_id = self._generate_ui_id("btn")
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

        close_btn_id = self._generate_ui_id("btn")
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
                await self._render_qq_conversation_list(window_node, window)
            elif window.window_class == "conversation":  # <-- 新增的分支
                await self._render_conversation_window(window_node, window)

    async def _render_qq_conversation_list(self, window_node: Element, window: Window) -> None:
        """[新核心逻辑] 专门渲染QQ会话列表窗口的动态内容."""
        page = window.content_state.get("page", 1)
        page_size = 30 if window.status == WindowStatus.MAXIMIZE else 15

        # 从数据库获取真实的会话列表数据
        # 注意：这里需要一个新的 service 方法来获取分页数据
        # conversations, total_pages = await self.entity_service.get_paged_conversations('qq', page, page_size)  # noqa: E501
        # --- 模拟数据 ---
        conversations = [
            {
                "uid": "qq_group_12345",
                "name": "开发交流群",
                "type": "group",
                "unread": 3,
                "latest_msg": "gemini：这个设计太棒了！",
            },
            {
                "uid": "qq_private_54321",
                "name": "未来星",
                "type": "private",
                "unread": 2,
                "latest_msg": "收到了吗？",
            },
        ]
        total_pages = 1
        # --- 模拟结束 ---

        list_node = SubElement(
            window_node,
            "conversation_list",
            attrib={
                "pagination": "true",
                "page_current": str(page),
                "page_total": str(total_pages),
                "items_per_page": str(page_size),
            },
        )

        for conv in conversations:
            conv_ui_id = self._generate_ui_id("conv")
            conv_node = SubElement(
                list_node,
                "conversation",
                attrib={
                    "id": conv_ui_id,
                    "name": conv["name"],
                    "type": conv["type"],
                    "unread": str(conv["unread"]),
                },
            )
            SubElement(conv_node, "desc").text = f"[最新消息]: {conv['latest_msg']}"

            enter_btn_id = self._generate_ui_id("btn")
            SubElement(
                conv_node,
                "button",
                attrib={"id": enter_btn_id, "name": "enter", "title": "进入会话"},
            )
            self._ui_mapping[enter_btn_id] = {
                "action_type": "click",
                "action": "open_conversation_window",
                "target_uid": conv["uid"],  # 映射到持久化的会话UID
            }

    async def _render_conversation_window(self, window_node: Element, window: Window) -> None:
        """[新核心逻辑] 专门渲染单个聊天窗口的动态内容，包括分页的聊天记录."""
        page = window.content_state.get("page", 1)
        page_size = 30 if window.status == WindowStatus.MAXIMIZE else 15
        conversation_uid = window.content_state.get("conversation_uid")

        if not conversation_uid:
            SubElement(window_node, "error").text = "无法加载聊天记录：未指定会话ID。"
            return

        # 1. 从数据库获取分页后的真实聊天数据
        (
            messages,
            current_page,
            total_pages,
        ) = await self.entity_service.event_storage_service.get_paged_chat_history(
            conversation_uid, page, page_size
        )

        # 更新窗口状态中的总页数，以便 dispatcher 可以验证
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

        # 2. 条件性地渲染“向上滚动”按钮
        if current_page > 1:
            scroll_up_id = self._generate_ui_id("btn")
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

        # 3. 渲染消息列表
        for msg in messages:
            # TODO: 这里需要一个更复杂的逻辑来从 event doc 中解析出 sender_name
            # 暂时使用 user_id 作为占位符
            sender_name = msg.get("user_info", {}).get("user_nickname", "未知用户")
            timestamp = time.strftime("%H:%M:%S", time.localtime(msg.get("timestamp", 0) / 1000))

            # 这里需要一个将 content (segment list) 转换为纯文本的辅助函数
            content_text = "".join(
                [
                    s.get("data", {}).get("text", "")
                    for s in msg.get("content", [])
                    if s.get("type") == "text"
                ]
            )

            msg_node = SubElement(
                list_node, "div", attrib={"class": "message", "id": msg.get("event_id")}
            )
            SubElement(msg_node, "sender").text = sender_name
            SubElement(msg_node, "timestamp").text = timestamp
            SubElement(msg_node, "content").text = content_text if content_text else "[非文本消息]"

        # 4. 条件性地渲染“向下滚动”按钮
        if current_page < total_pages:
            scroll_down_id = self._generate_ui_id("btn")
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

        # 5. 渲染抽象的动作栏
        action_bar_node = SubElement(window_node, "action_bar")
        SubElement(action_bar_node, "desc").text = "你可以使用 send_message 动作来回复。"
