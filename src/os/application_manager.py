# 文件路径: src/os/application_manager.py

from .models import Application, ApplicationLifecycle


class ApplicationManager:
    """管理 AIC-OS 中所有应用程序的生命周期."""

    def __init__(self) -> None:
        # _applications 是唯一真实来源，存储所有“已安装”的应用
        # 实际项目中，这应该从数据库或配置文件加载
        self._applications: dict[str, Application] = {}
        self._self_bot_ids_map: dict[str, str] = {}

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

    def load_installed_apps(self, apps: list[Application]) -> None:
        """加载系统“已安装”的所有应用."""
        self._applications = {app.id: app for app in apps}

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
            self,
            ui_mapping: dict,
            allow_only_click: bool = False
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

        return properties

