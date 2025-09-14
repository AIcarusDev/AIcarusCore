# 文件路径: src/os/apps/file_explorer/__init__.py

from dataclasses import dataclass
from xml.etree.ElementTree import SubElement
from typing import Any, Dict, TYPE_CHECKING

from src.os.models import Application, Window
from src.services.action.components.base_builder import BaseAppBuilder

if TYPE_CHECKING:
    from src.bootstrap.container import ServiceContainer

# --- App Definition ---

app_info = Application(
    id="app-file-explorer",
    name="file_explorer",
    title="资源管理器"
)

class FileExplorerAppBuilder(BaseAppBuilder):
    """
    Builder for the File Explorer application.
    Responsible for rendering the window content of the file explorer.
    """
    @property
    def app_name(self) -> str:
        """返回 app_name."""
        return "file_explorer"

    async def on_before_start(self, container: "ServiceContainer") -> tuple[bool, str | None]:
        """应用启动前."""
        return True, None

    async def on_after_start(self, container: "ServiceContainer", app_id: str) -> Window:
        """应用启动后, 返回一个主窗口."""
        return Window(
            name="file_explorer_main",
            parent_app_id=app_id,
            title="资源管理器",
            window_class="main",
            content_state={"current_path": "/desktop"},
        )

    async def render_popup_content(self, **kwargs) -> None:
        """渲染弹窗内容 (文件浏览器目前没有弹窗)."""
        pass

    async def handle_llm_action(self, action_name: str, params: Dict[str, Any], **kwargs) -> str:
        """处理LLM动作 (文件浏览器目前没有)."""
        return f"错误：文件浏览器应用无法处理 '{action_name}' 动作。"

    async def render_window_content(self, **kwargs) -> None:
        parent_element = kwargs["parent_element"]
        current_path = kwargs["current_path"]
        window = kwargs["window"]
        ui_mapping = kwargs["ui_mapping"]
        generate_semantic_id = kwargs["generate_semantic_id"]
        
        action_handler = kwargs.get("action_handler")
        if not action_handler or not hasattr(action_handler, "_container"):
            parent_element.text = "错误：无法访问服务容器。"
            return
        
        container = action_handler._container
        file_system_manager = container.file_system_manager

        path_to_render = window.content_state.get("current_path", "/desktop")
        
        breadcrumbs_node = SubElement(parent_element, "breadcrumbs")
        breadcrumbs_node.text = f"当前路径: {path_to_render}"

        items_node = SubElement(parent_element, "items")
        children = file_system_manager.get_children(path_to_render)

        if children is None:
            items_node.text = f"错误：路径 '{path_to_render}' 不存在。"
            return

        if not children:
            items_node.text = "该文件夹为空。"

        for name, item in children.items():
            item_path = [*current_path, item.type, name]
            item_id = generate_semantic_id(item_path)
            
            if item.type == "folder":
                node = SubElement(items_node, "folder", attrib={"id": item_id, "name": name})
                ui_mapping[item_id] = {
                    "action_type": "double_click",
                    "action": "open_folder",
                    "target_uid": f"{path_to_render.rstrip('/')}/{name}"
                }
            elif item.type == "file":
                node = SubElement(items_node, "file", attrib={"id": item_id, "name": name})
                ui_mapping[item_id] = {
                    "action_type": "double_click",
                    "action": "open_file",
                    "target_uid": f"{path_to_render.rstrip('/')}/{name}"
                }
            
            delete_button_path = [*item_path, "delete"]
            delete_button_id = generate_semantic_id(delete_button_path)
            SubElement(node, "button", attrib={"id": delete_button_id, "name": "delete", "title": "删除"})
            ui_mapping[delete_button_id] = {
                "action_type": "click",
                "action": "delete_item",
                "target_uid": f"{path_to_render.rstrip('/')}/{name}"
            }

@dataclass
class AppDefinition:
    app_info: Application
    builder_class: type[BaseAppBuilder]

app_definition = AppDefinition(
    app_info=app_info,
    builder_class=FileExplorerAppBuilder
)
