# 文件路径: src/os/apps/text_editor/builder.py

from typing import TYPE_CHECKING, Any
from xml.etree.ElementTree import SubElement

from src.common.custom_logging.logging_config import get_logger
from src.os.models import Window, WindowStatus
from src.services.action.components.base_builder import BaseAppBuilder

if TYPE_CHECKING:
    from src.bootstrap.container import ServiceContainer
    from src.os.file_system_manager import FileSystemManager
    from src.os.window_manager import WindowManager


logger = get_logger(__name__)
class TextEditorAppBuilder(BaseAppBuilder):
    """文本编辑器应用的构建器."""

    @property
    def app_name(self) -> str:
        """返回应用的名称."""
        return "text_editor"

    async def on_before_start(self, container: "ServiceContainer") -> tuple[bool, str | None]:
        """在应用启动之前执行的异步操作.

        Args:
            container: 服务容器，提供对各种服务的访问。

        Returns:
            一个元组，包含一个布尔值表示是否允许应用启动，以及一个可选的错误消息。
            如果允许启动，则布尔值为 True，否则为 False。如果启动被阻止，可以提供一个错误消息。
        """
        return True, None

    async def on_after_start(self, container: "ServiceContainer", app_id: str) -> Window:
        """应用启动后, 返回一个空的单例主窗口."""
        return Window(
            name="text_editor_main", # 固定名称
            parent_app_id=app_id,
            title="文本编辑器",
            # 初始状态为空，没有打开任何文件
            content_state={"tabs": [], "active_tab_id": None},
        )

    async def render_popup_content(self, **kwargs: Any) -> None:
        """渲染弹出窗口内容 (当前文本编辑器应用不需要)."""
        pass

    async def render_window_content(self, **kwargs: Any) -> None:
        """渲染文本编辑器窗口的内容，包括标签页和激活文件的内容."""
        parent_element = kwargs["parent_element"]
        window: Window = kwargs["window"]
        ui_mapping = kwargs["ui_mapping"]
        generate_semantic_id = kwargs["generate_semantic_id"]
        file_system_manager: FileSystemManager = kwargs["file_system_manager"]

        tabs = window.content_state.get("tabs", [])
        active_tab_id = window.content_state.get("active_tab_id")

        description = "可以浏览编辑所有文本或代码的多功能编辑器，"
        description += "写入即自动保存。"
        SubElement(parent_element, "desc").text = description

        tabs_node = SubElement(parent_element, "tabs")

        active_file_path = None

        for tab in tabs:
            item_id = tab.get("item_id")
            file_name = tab.get("name")
            is_active = (item_id == active_tab_id)

            tab_node = SubElement(
                tabs_node,
                "tab",
                attrib={"item_id": item_id, "active": str(is_active).lower()},
            )
            SubElement(tab_node, "file_name").text = file_name

            # 为 tab id 中的特殊字符进行转义，以便生成合法的 semantic id
            safe_tab_id_part = item_id.replace(":", "_").replace("/", "_").replace(".", "_")

            if not is_active:
                view_btn_id = generate_semantic_id(["text_editor", "tab", safe_tab_id_part, "view"])
                SubElement(
                    tab_node,
                    "button",
                    attrib={"id": view_btn_id, "name": "view", "title": "查看"}
                )
                ui_mapping[view_btn_id] = {
                    "action_type": "click",
                    "action": "view_tab",
                    "target_uid": item_id
                }
            else:
                active_file_path = tab.get("path")

            close_btn_id = generate_semantic_id(["text_editor", "tab", safe_tab_id_part, "close"])
            SubElement(
                tab_node,
                "button",
                attrib={"id": close_btn_id, "name": "close", "title": "关闭"},
            )
            ui_mapping[close_btn_id] = {
                "action_type": "click",
                "action": "close_tab",
                "target_uid": item_id
            }

        if active_file_path:
            content_node = SubElement(
                parent_element,
                "content",
                attrib={"file_path": active_file_path}
            )
            file_content = file_system_manager.read_file_content(active_file_path)
            if file_content is not None:
                content_node.text = file_content
            else:
                content_node.text = f"错误：无法读取文件 '{active_file_path}' 的内容。"

    async def get_action_definitions(
        self, window_manager: "WindowManager", container: "ServiceContainer"
    ) -> dict[str, Any]:
        """动态构建文本编辑器的可用动作 Schema."""
        editor_window = window_manager.get_window("text_editor_main")

        # 仅当编辑器窗口打开、非最小化且有激活的标签页时，才提供 edit 动作
        if (
            editor_window and
            editor_window.status != WindowStatus.MINIMIZE and
            editor_window.content_state.get("active_tab_id")
        ):
            return {
                "text_editor": {
                    "type": "object",
                    "description": "在文本编辑器中对当前激活的文件进行内容修改。",
                    "properties": {
                        "edit": {
                            "type": "object",
                            "description": "编辑当前激活标签页的文件内容。",
                            "properties": {
                                "content": {
                                    "type": "string", "description": "要写入的全新或追加的内容。"
                                },
                                "append": {
                                    "type": "boolean",
                                    "description": "默认为 false (覆盖)。"
                                                "若为 true，则在文件末尾追加内容。",
                                    "default": False,
                                },
                            },
                            "required": ["content"],
                        },
                        "motivation": {"type": "string"}
                    },
                    "required": ["edit", "motivation"]
                }
            }
        return {}

    async def handle_llm_action(
        self,
        action_name: str,
        params: dict[str, Any],
        container: "ServiceContainer",
        thought_key: str,
    ) -> None:
        """处理LLM的文本编辑器动作."""
        if action_name != "text_editor":
            return

        fs_manager = container.file_system_manager
        window_manager = container.window_manager

        command_obj = params.get("edit", {})
        content = command_obj.get("content")
        append = command_obj.get("append", False)

        if content is None:
            return

        editor_window = window_manager.get_window("text_editor_main")
        if not editor_window:
            return

        active_tab_id = editor_window.content_state.get("active_tab_id")
        if not active_tab_id:
            return

        try:
            _type, user_path = active_tab_id.split(":", 1)
        except ValueError:
            return

        success = fs_manager.write_file_content(user_path, content, append)
        logger.info(f"Text editor action 'edit' on '{user_path}' executed. Success: {success}")
