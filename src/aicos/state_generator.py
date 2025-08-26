# src/aicos/state_generator.py
import time
from xml.dom.minidom import parseString
from xml.etree.ElementTree import Element, SubElement, tostring

from src.common.custom_logging.logging_config import get_logger

# 导入核心依赖
from src.database.services.entity_graph_service import EntityGraphService

from .application_manager import ApplicationManager
from .models import Window, WindowStatus
from .window_manager import WindowManager

logger = get_logger(__name__)

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
        self.is_connected = False

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

        # 如果未连接，渲染一个简单的未连接状态
        if not self.is_connected:
            root = Element('AIC-OS', attrib={'connection': 'disconnected', 'lifecycle': 'stopped'})
            SubElement(root, 'desc').text = "你尚未连接到你的设备。"

            connect_btn_id = self._generate_ui_id('btn')
            SubElement(
                root, 'button',
                attrib={'id': connect_btn_id, 'name': 'connect','title': '连接设备'}
            )
            self._ui_mapping[connect_btn_id] = {
                'action_type': 'click',
                'action': 'connect_device',
                'target_uid': 'aicos-main' # 虚拟设备ID
            }
            desktop_node = root.find('desktop')
            if desktop_node is not None:
                system_tray_node = SubElement(desktop_node, 'system_tray')
                disconnect_btn_id = self._generate_ui_id('btn')
                SubElement(
                    system_tray_node, 'button',
                    attrib={
                        'id': disconnect_btn_id,
                        'name': 'disconnect',
                        'title': '断开与设备的连接'
                    }
                )
                self._ui_mapping[disconnect_btn_id] = {
                    'action_type': 'click',
                    'action': 'disconnect_device',
                    'target_uid': 'aicos-main'
                }

            xml_string = self._pretty_print_xml(root)
            return xml_string, self._ui_mapping

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

        # --- 定义不可关闭的核心进程 ---
        core_processes = {"task_manager", "file_explorer"}

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
            # --- [核心修改] 跳过核心进程 ---
            if app.name in core_processes:
                continue
            # -----------------------------

            proc_id = f"proc-{app.id}"
            proc_node = SubElement(
                bg_processes_node,
                "process",
                attrib={"id": proc_id, "name": app.name, "title": app.title},
            )

            # --- [核心修改] 为非核心进程添加关闭按钮 ---
            kill_btn_id = self._generate_ui_id('btn-kill')
            SubElement(
                proc_node, 'button',
                attrib={'id': kill_btn_id, 'name': 'terminate_process', 'title': '结束进程'}
            )
            self._ui_mapping[kill_btn_id] = {
                'action_type': 'click',
                'action': 'kill_process',
                'target_uid': app.id # 目标是应用ID
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
        """[最终版] 专门渲染QQ会话列表窗口的动态内容."""
        page = window.content_state.get('page', 1)
        page_size = 30 if window.status == WindowStatus.MAXIMIZE else 15

        # --- [核心修复] 从数据库获取真实的会话列表数据 ---
        # 我们需要 bot_id 来正确计算未读数
        bot_id = self.application_manager.get_self_bot_ids_map().get("qq")
        if not bot_id:
            logger.error("无法获取 QQ 的 bot_id，无法渲染会话列表。")
            SubElement(window_node, 'error').text = "内部错误：无法获取机器人ID。"
            return

        # 调用服务层获取数据
        # 注意：这里假设 entity_service 有一个获取分页会话的方法
        # 我们需要先在 entity_service 中实现它
        conversations, total_pages = await self.entity_service.get_paged_conversations(
            platform_id='qq',
            page=page,
            page_size=page_size,
            self_bot_ids={"qq": bot_id}
        )

        list_node = SubElement(window_node, 'conversation_list', attrib={
            'pagination': 'true', 'page_current': str(page),
            'page_total': str(total_pages), 'items_per_page': str(page_size)
        })

        if not conversations:
            SubElement(list_node, 'desc').text = "没有会话。"
            return

        for conv_data in conversations:
            conv_doc = conv_data.get("conv_doc")
            latest_event = conv_data.get("latest_event")
            unread_count = conv_data.get("unread_count", 0)

            if not conv_doc or not latest_event:
                continue

            conv_uid = conv_doc._key
            conv_name = conv_doc.details.name or "未知会话"
            conv_type = conv_doc.details.type

            # 使用 EventStorageService 的方法来获取最新消息的文本摘要
            latest_msg_text = await self.entity_service.event_storage_service.get_event_text_summary(  # noqa: E501
                latest_event
            )

            conv_ui_id = self._generate_ui_id('conv')
            conv_node = SubElement(list_node, 'conversation', attrib={
                'id': conv_ui_id, 'name': conv_name, 'type': conv_type, 'unread': str(unread_count)
            })
            SubElement(conv_node, 'desc').text = f"[最新消息]: {latest_msg_text}"

            enter_btn_id = self._generate_ui_id('btn')
            SubElement(
                conv_node,
                'button',
                attrib={
                    'id': enter_btn_id,
                    'name': 'enter',
                    'title': '进入会话'
                }
            )
            self._ui_mapping[enter_btn_id] = {
                'action_type': 'click',
                'action': 'open_conversation_window',
                'target_uid': conv_uid # <-- 现在这里是来自数据库的真实 UID！
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
