# 文件路径: src/os/apps/file_explorer/__init__.py

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from xml.etree.ElementTree import SubElement

from src.common.custom_logging.logging_config import get_logger
from src.os.models import Application, Window, WindowStatus
from src.os.window_manager import WindowManager
from src.services.action.components.base_builder import BaseAppBuilder

if TYPE_CHECKING:
    from src.bootstrap.container import ServiceContainer
    from src.os.file_system_manager import FileSystemManager

logger = get_logger(__name__)


# --- App Definition ---

app_info = Application(
    id="app-file-explorer",
    name="file_explorer",
    title="资源管理器",
    is_utility=True # 标记为系统工具
)

class FileExplorerAppBuilder(BaseAppBuilder):
    """资源管理器应用的构建器."""

    @property
    def app_name(self) -> str:
        """返回应用的名称."""
        return "file_explorer"

    async def on_before_start(self, container: "ServiceContainer") -> tuple[bool, str | None]:
        """在应用启动前执行的异步方法.

        Args:
            container (ServiceContainer): 服务容器实例.

        Returns:
            tuple[bool, str | None]: 一个元组，
            包含一个布尔值表示是否允许启动应用，以及一个可选的错误消息.
        """
        return True, None

    async def on_after_start(self, container: "ServiceContainer", app_id: str) -> Window:
        """应用启动后, 返回单例主窗口."""
        return Window(
            name="file_explorer_main", # 固定名称
            parent_app_id=app_id,
            title="资源管理器",
            # 初始路径为桌面
            content_state={"current_path": "/desktop/"},
        )

    async def render_popup_content(self, **kwargs: Any) -> None:
        """渲染弹出窗口的内容 (当前为空)."""
        pass

    async def render_window_content(self, **kwargs: Any) -> None:
        """渲染文件资源管理器窗口的内容."""
        parent_element = kwargs["parent_element"]
        window: Window = kwargs["window"]
        ui_mapping = kwargs["ui_mapping"]
        file_system_manager: FileSystemManager = kwargs["file_system_manager"]

        path_to_render = window.content_state.get("current_path", "/desktop/")

        # 渲染路径栏
        SubElement(parent_element, "path").text = path_to_render

        items_node = SubElement(parent_element, "items")
        contents = file_system_manager.list_directory_contents(path_to_render)

        if contents is None:
            items_node.text = f"错误：路径 '{path_to_render}' 不存在或无法访问。"
            return

        if not contents:
            items_node.text = "该文件夹为空。"
            return

        for item in contents:
            # 渲染文件或文件夹节点
            SubElement(items_node, item.type, attrib={"name": item.name, "item_id": item.item_id})
            # 为每个项目创建双击打开的映射
            action = "open_folder" if item.type == "folder" else "open_file"
            ui_mapping[item.item_id] = {
                "action_type": "double_click",
                "action": action,
                "target_uid": item.item_id,
            }

    async def get_action_definitions(
        self, window_manager: "WindowManager", container: "ServiceContainer"
    ) -> dict:
        """动态构建文件管理器的可用动作 Schema."""
        fs_manager = container.file_system_manager

        # 1. 找出所有可见的文件和文件夹
        visible_item_ids = []
        # 检查桌面
        desktop_items = fs_manager.list_directory_contents("/desktop")
        if desktop_items:
            visible_item_ids.extend(item.item_id for item in desktop_items)

        # 检查打开的文件浏览器窗口
        fe_window = window_manager.get_window("file_explorer_main")
        if fe_window and fe_window.status != WindowStatus.MINIMIZE:
            current_path = fe_window.content_state.get("current_path", "/desktop/")
            window_items = fs_manager.list_directory_contents(current_path)
            if window_items:
                visible_item_ids.extend(item.item_id for item in window_items)

        # 2. 找出所有文件夹，用于 "move" 动作的目标路径
        all_folders = []
        # 这是一个简化的递归，对于非常深的目录可能需要优化
        def find_all_folders(path: str) -> None:
            items = fs_manager.list_directory_contents(path)
            if items:
                for item in items:
                    if item.type == 'folder':
                        all_folders.append(item.item_id)
                        find_all_folders(item.item_id.split(':', 1)[1])
        find_all_folders("/") # 从根目录开始查找

        # 3. 构建 Schema

        # Create a copy to avoid modifying the original list
        rename_delete_enum = list(set(visible_item_ids))
        move_enum = list(set(visible_item_ids))
            # 移动只能针对文件和文件夹
        properties = {
            "create": {
                "type": "object",
                "description": "在指定路径创建一个新的文件或文件夹。",
                "properties": {
                    "type": {"type": "string", "enum": ["file", "folder"]},
                    "path": {
                        "type": "string",
                        "description": "创建的完整相对路径，例如 "
                                    "'/desktop/new_folder/' 或 '/desktop/new_file.txt'。"
                    },
                    "content": {
                        "type": "string",
                        "description": "如果创建文件，可以提供可选的初始内容。"
                    }
                },
                "required": ["type", "path"]
            }
        }

        if rename_delete_enum:
            properties["delete"] = {
                "type": "object",
                "description": "删除一个当前可见的文件或文件夹。",
                "properties": {
                    "item_id": {
                        "type": "string",
                        "description": "要删除的文件或文件夹的 item_id。",
                        "enum": rename_delete_enum
                    }
                },
                "required": ["item_id"]
            }
            properties["rename"] = {
                "type": "object",
                "description": "重命名一个当前可见的文件或文件夹。",
                "properties": {
                    "item_id": {
                        "type": "string",
                        "description": "要重命名的 item_id。",
                        "enum": rename_delete_enum
                    },
                    "new_name": {
                        "type": "string",
                        "description": "新的名称。如果是文件，必须包含扩展名。",
                    }
                },
                "required": ["item_id", "new_name"]
            }

        if move_enum and len(all_folders) > 0:
            properties["move"] = {
                "type": "object",
                "description": "移动一个可见的文件或文件夹到另一个文件夹。",
                "properties": {
                    "item_id": {
                        "type": "string",
                        "description": "要移动的 item_id。",
                        "enum": move_enum
                    },
                    "new_path": {
                        "type": "string",
                        "description": "目标文件夹的路径 (item_id)，必须以'/'结尾。",
                        "enum": all_folders
                    }
                },
                "required": ["item_id", "new_path"]
            }

        # 组装最终 Schema
        if len(properties) == 1 and "create" in properties:
            # 如果只能创建，简化 oneOf
            final_oneof = [{"required": ["create"]}]
        else:
            final_oneof = [{"required": [key]} for key in properties]

        return {
            "file_explorer": {
                "type": "object",
                "description": "文件管理操作。所有路径都是相对于工作区的根目录。",
                "properties": {
                    "command": {
                        "type": "object",
                        "description": "要执行的具体文件命令。",
                        "properties": properties,
                        "oneOf": final_oneof
                    },
                    "motivation": {"type": "string"}
                },
                "required": ["command", "motivation"]
            }
        }

    async def handle_llm_action(
        self, action_name: str, params: dict, container: "ServiceContainer", thought_key: str
    ) -> None:
        """处理LLM的文件管理器动作，并根据结果创建弹窗."""
        fs_manager = container.file_system_manager
        window_manager = container.window_manager # 获取 window_manager

        command_obj = params.get("command", {})
        command_name = next(iter(command_obj), None)
        command_params = command_obj.get(command_name, {})

        if not command_name:
            return

        # 执行对应的文件系统操作
        if command_name == "create":
            create_result = fs_manager.create(
                command_params.get("type"), command_params.get("path")
            )

            # 根据结果创建不同类型的弹窗
            if create_result.success:
                # 如果被重命名，则创建一个非模态、瞬态的通知弹窗
                if create_result.renamed:
                    notification_popup = Window(
                        name=f"win-popup-fs-notify-{uuid.uuid4().hex[:6]}",
                        parent_app_id=app_info.id,
                        title="操作通知",
                        window_class="system_notification_popup", # 自定义一个class
                        content_state={"message": create_result.message},
                        is_popup=True,
                        popup_type="notification", # 非交互式
                        transient_cycles_remaining=1,  # 只显示一个认知周期
                    )
                    window_manager.open_window(notification_popup)
                # 对于普通的成功，可以只在日志中记录，或者也弹出一个更短暂的通知
                logger.info(create_result.message)

                # 如果创建的是文件且有内容，则写入
                if (create_result.path and command_params.get("type") == 'file'
                        and command_params.get('content')):
                    fs_manager.write_file_content(
                        self.get_relative_path_str(create_result.path), # 使用返回的真实路径
                        command_params.get('content')
                    )
            else:
                # 如果创建失败，则创建一个模态错误弹窗，劫持UI
                error_popup = Window(
                    name=f"win-popup-fs-error-{uuid.uuid4().hex[:6]}",
                    parent_app_id=app_info.id,
                    title="操作失败",
                    window_class="system_error_modal", # 使用一个通用的错误class
                    content_state={"error_message": create_result.message},
                    is_popup=True,
                    popup_type="modal", # 模态，会劫持屏幕
                )
                window_manager.open_window(error_popup)

        elif command_name == "delete":
            success = fs_manager.delete(command_params.get("item_id"))
            if not success:
                error_popup = Window(
                    name=f"win-popup-fs-error-{uuid.uuid4().hex[:6]}",
                    parent_app_id=app_info.id,
                    title="删除失败",
                    window_class="system_error_modal",
                    content_state={"error_message": "无法删除该项目，可能已被移动或不存在。"},
                    is_popup=True,
                    popup_type="modal",
                )
                window_manager.open_window(error_popup)
            else:
                logger.info(f"成功删除项目: {command_params.get('item_id')}")

        elif command_name == "rename":
            success = fs_manager.rename(
                command_params.get("item_id"), command_params.get("new_name")
            )
            if not success:
                error_popup = Window(
                    name=f"win-popup-fs-error-{uuid.uuid4().hex[:6]}",
                    parent_app_id=app_info.id,
                    title="重命名失败",
                    window_class="system_error_modal",
                    content_state={"error_message": "无法重命名该项目，可能已被移动或不存在，"
                                                    "或新名称不合法。"},
                    is_popup=True,
                    popup_type="modal",
                )
                window_manager.open_window(error_popup)
            else:
                logger.info(
                    f"成功重命名项目: {command_params.get('item_id')} -> "
                    f"{command_params.get('new_name')}"
                )

        elif command_name == "move":
            # 从目标 item_id 中解析出路径
            dest_item_id = command_params.get("new_path", "")
            try:
                _type, dest_user_path = dest_item_id.split(":", 1)
                if _type == "folder":
                    success = fs_manager.move(command_params.get("item_id"), dest_user_path)
            except ValueError:
                success = False
            if not success:
                error_popup = Window(
                    name=f"win-popup-fs-error-{uuid.uuid4().hex[:6]}",
                    parent_app_id=app_info.id,
                    title="移动失败",
                    window_class="system_error_modal",
                    content_state={"error_message": "无法移动该项目，可能已被移动或不存在，"
                                                    "或目标路径不合法。"},
                    is_popup=True,
                    popup_type="modal",
                )
                window_manager.open_window(error_popup)

        # 未来可以在此处向 thought_key 中写入更详细的执行结果
        logger.info(f"File explorer action '{command_name}' executed. Success: {success}")


@dataclass
class AppDefinition:
    """AppDefinition 数据类，用于封装应用的信息和构建器类."""
    app_info: Application
    builder_class: type[BaseAppBuilder]

app_definition = AppDefinition(
    app_info=app_info,
    builder_class=FileExplorerAppBuilder
)
