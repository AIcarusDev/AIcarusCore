# 文件路径: src/os/application_manager.py

import importlib
import pkgutil

from src.common.custom_logging.logging_config import get_logger
from src.services.action.components.base_builder import BaseAppBuilder

from .models import Application, ApplicationLifecycle

logger = get_logger(__name__)


class ApplicationManager:
    """管理 AIC-OS 中所有应用的发现、加载和生命周期."""

    def __init__(self) -> None:
        self._applications: dict[str, Application] = {}  # Key: app.id
        self._builders: dict[str, BaseAppBuilder] = {}  # Key: app.name
        self._self_bot_ids_map: dict[str, str] = {}

    def discover_and_load_apps(self, package: type) -> None:
        """在系统启动时，自动扫描 apps 包，发现并加载所有应用."""
        logger.info(f"应用管理器：开始从包 '{package.__name__}' 自动发现应用...")

        if not hasattr(package, "__path__"):
            logger.error(f"提供的包 '{package.__name__}' 不是一个有效的包。")
            return

        for module_info in pkgutil.iter_modules(package.__path__, package.__name__ + "."):
            if not module_info.ispkg:
                continue

            try:
                module = importlib.import_module(module_info.name)
                if hasattr(module, "app_definition"):
                    definition = module.app_definition
                    app_info = definition.app_info
                    builder_class = definition.builder_class

                    if app_info.id in self._applications:
                        logger.warning(
                            f"发现重复的应用ID '{app_info.id}'！后加载的应用将覆盖前者。"
                        )

                    self._applications[app_info.id] = app_info
                    self._builders[app_info.name] = builder_class()

                    logger.info(
                        f"✅ 成功发现并加载应用: '{app_info.title}' "
                        f"(ID: {app_info.id}, Name: {app_info.name})"
                    )

            except Exception as e:
                logger.error(f"加载应用 '{module_info.name}' 时发生错误: {e}", exc_info=True)

        logger.info(f"应用发现完成。共加载 {len(self._applications)} 个应用。")

    def get_builder_by_name(self, app_name: str) -> BaseAppBuilder | None:
        """根据应用的 name (例如 'qq') 获取其 Builder 实例."""
        return self._builders.get(app_name)

    def get_app_by_id(self, app_id: str) -> Application | None:
        """根据唯一的 app_id 获取应用信息."""
        return self._applications.get(app_id)

    def set_self_bot_ids_map(self, bot_ids_map: dict[str, str]) -> None:
        """从外部一次性注入完整的 bot_id 映射."""
        self._self_bot_ids_map = bot_ids_map

    def set_self_bot_id_for_platform(self, platform_id: str, bot_id: str) -> None:
        """为单个平台设置或更新机器人的 bot_id.

        这是供各个应用构建器在安检完成后调用的标准接口。
        """
        self._self_bot_ids_map[platform_id] = bot_id

    def get_self_bot_ids_map(self) -> dict[str, str]:
        """获取 bot_id 映射."""
        return self._self_bot_ids_map

    def start_app(self, app_id: str) -> bool:
        """启动一个应用，将其生命周期状态设置为 RUNNING."""
        if app_id in self._applications:
            self._applications[app_id].lifecycle = ApplicationLifecycle.RUNNING
            return True
        return False

    def stop_app(self, app_id: str) -> bool:
        """停止一个应用，将其生命周期状态设置为 STOPPED."""
        if app_id in self._applications:
            self._applications[app_id].lifecycle = ApplicationLifecycle.STOPPED
            return True
        return False

    def is_running(self, app_id: str) -> bool:
        """检查一个应用是否正在运行."""
        app = self._applications.get(app_id)
        return app is not None and app.lifecycle == ApplicationLifecycle.RUNNING

    def get_running_apps(self) -> list[Application]:
        """获取所有正在运行的应用列表."""
        return [
            app
            for app in self._applications.values()
            if app.lifecycle == ApplicationLifecycle.RUNNING
        ]

    def get_all_apps(self) -> list[Application]:
        """获取所有已安装的应用列表."""
        return list(self._applications.values())

    def build_base_interaction_schema(
        self, ui_mapping: dict, allow_only_click: bool = False
    ) -> dict:
        """根据当前的 UI 映射，构建基础交互动作的 JSON Schema."""
        properties = {}
        # Click
        clickable_ids = [
            key for key, info in ui_mapping.items() if info.get("action_type") == "click"
        ]
        if clickable_ids:
            properties["click"] = {
                "type": "object",
                "title": "单击",
                "description": "通常用于点击某个按钮。",
                "properties": {
                    "target_id": {"type": "string", "enum": clickable_ids},
                    "motivation": {"type": "string"},
                },
                "required": ["target_id", "motivation"],
            }

        # Double Click
        if not allow_only_click:
            double_clickable_ids = [
                key for key, info in ui_mapping.items() if info.get("action_type") == "double_click"
            ]
            if double_clickable_ids:
                properties["double_click"] = {
                    "type": "object",
                    "title": "双击",
                    "description": "通常用于打开某个文件或快捷方式。",
                    "properties": {
                        "target_id": {"type": "string", "enum": double_clickable_ids},
                        "motivation": {"type": "string"},
                    },
                    "required": ["target_id", "motivation"],
                }

        # 通用输入动作
        input_field_ids = [
            key for key, info in ui_mapping.items() if info.get("action_type") == "input_override"
        ]
        if input_field_ids:
            properties["input_override"] = {
                "type": "object",
                "title": "输入覆盖",
                "description": "向指定的输入框中填入新内容。",
                "properties": {
                    "target_id": {
                        "type": "string",
                        "description": "要输入内容的目标输入框的ID。",
                        "enum": input_field_ids
                    },
                    "content": {
                        "type": "string",
                        "description": "要填入的文本内容。"
                    },
                    "motivation": {"type": "string"},
                },
                "required": ["target_id", "content", "motivation"],
            }

        return properties
