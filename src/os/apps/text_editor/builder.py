# 文件路径: src/os/apps/text_editor/builder.py

from xml.etree.ElementTree import SubElement
from typing import Any, Dict, TYPE_CHECKING

from src.os.models import Window
from src.services.action.components.base_builder import BaseAppBuilder

if TYPE_CHECKING:
    from src.bootstrap.container import ServiceContainer
    from src.os.window_manager import WindowManager

class TextEditorAppBuilder(BaseAppBuilder):
    """
    Builder for the Text Editor application.
    Responsible for rendering the window content of a text file.
    """
    @property
    def app_name(self) -> str:
        """返回 app_name."""
        return "text_editor"

    async def on_before_start(self, container: "ServiceContainer") -> tuple[bool, str | None]:
        """应用启动前."""
        return True, None

    async def on_after_start(self, container: "ServiceContainer", app_id: str) -> Window:
        """应用启动后, 返回一个主窗口."""
        # 文本编辑器通常是通过“打开文件”来启动的，而不是自己有一个主界面。
        # 但为了遵循 App 协议，我们返回一个不可见的、立即被回收的占位窗口。
        return Window(
            name="text_editor_placeholder",
            parent_app_id=app_id,
            title="文本编辑器",
            window_class="placeholder",
            transient_cycles_remaining=0,  # 立即被垃圾回收
        )

    async def render_popup_content(self, **kwargs) -> None:
        """渲染弹窗内容 (文本编辑器目前没有弹窗)."""
        pass

    async def render_window_content(self, **kwargs) -> None:
        """
        Renders the content of the text editor window, which includes the
        file content and associated actions.
        """
        parent_element = kwargs["parent_element"]
        window: Window = kwargs["window"]
        
        # 从窗口状态中获取文件路径和内容
        file_path = window.content_state.get("path", "未知文件")
        file_content = window.content_state.get("content", "无法加载文件内容。")

        # 创建编辑器根元素
        editor_node = SubElement(parent_element, "editor", attrib={"file_path": file_path})
        
        # 显示文件内容
        content_node = SubElement(editor_node, "content")
        content_node.text = file_content
        
        # 根据用户要求，此处不添加“保存”按钮，为自动保存做准备。
        # 未来可以在这里添加其他UI元素，例如状态栏、字数统计等。
        actions_node = SubElement(editor_node, "actions")
        actions_node.text = "编辑内容后将自动保存。"

    async def get_action_definitions(
        self, window_manager: "WindowManager", container: "ServiceContainer"
    ) -> Dict[str, Any]:
        """
        Defines the 'edit_file' action that the LLM can use to modify the file content.
        """
        return {
            "edit_file": {
                "type": "object",
                "description": "修改当前打开的文件的内容。此操作会直接覆写整个文件。",
                "properties": {
                    "target_window_id": {
                        "type": "string",
                        "description": "必须提供当前编辑器窗口的ID。",
                    },
                    "content": {
                        "type": "string",
                        "description": "要写入文件的全新内容。",
                    },
                    "motivation": {"type": "string"},
                },
                "required": ["target_window_id", "content", "motivation"],
            }
        }

    async def handle_llm_action(self, action_name: str, params: Dict[str, Any], **kwargs) -> str:
        """
        Handles the 'edit_file' action triggered by the LLM.
        """
        if action_name != "edit_file":
            return f"错误：文本编辑器应用无法处理 '{action_name}' 动作。"

        container = kwargs.get("container")
        if not container:
            return "错误：无法访问服务容器。"

        window_id = params.get("target_window_id")
        new_content = params.get("content")

        if not window_id or new_content is None:
            return "错误：缺少窗口ID或写入内容。"

        # 从窗口管理器获取窗口信息
        window = container.window_manager.get_window(window_id)
        if not window:
            return f"错误：找不到ID为 '{window_id}' 的窗口。"

        file_path = window.content_state.get("path")
        if not file_path:
            return f"错误：窗口 '{window_id}' 中没有文件路径信息。"

        # 调用文件系统服务写入文件
        # 注意：这里的 write_file 默认是覆盖写入 (append=False)
        write_params = {
            "path": file_path,
            "content": new_content,
            "append": False,
            "motivation": params.get("motivation", "通过文本编辑器修改文件。")
        }
        
        # FileSystemService 的方法不是 async 的，所以直接调用
        # 注意：这里的调用方式可能需要根据 FileSystemService 的实际实现调整
        # 假设 FileSystemManager 提供了直接的文件写入方法
        try:
            container.file_system_manager.write_file(file_path, new_content)
            
            # 更新窗口状态以反映最新的内容
            window.content_state["content"] = new_content
            
            return f"成功将内容写入文件 '{file_path}'。"
        except Exception as e:
            return f"错误：写入文件 '{file_path}' 时发生错误: {e}"
