# 文件路径: src/os/apps/text_editor/__init__.py

from dataclasses import dataclass

from src.os.models import Application
from src.services.action.components.base_builder import BaseAppBuilder

from .builder import TextEditorAppBuilder

# --- App Definition ---

app_info = Application(
    id="app-text-editor",
    name="text_editor",
    title="文本编辑器",
    is_utility=True # 标记为系统工具
)

@dataclass
class AppDefinition:
    """定义应用的基本信息和构建类."""
    app_info: Application
    builder_class: type[BaseAppBuilder]

app_definition = AppDefinition(
    app_info=app_info,
    builder_class=TextEditorAppBuilder
)
