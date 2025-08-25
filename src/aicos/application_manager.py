# src/aicos/application_manager.py

from .models import Application, ApplicationLifecycle


class ApplicationManager:
    """管理 AIC-OS 中所有应用程序的生命周期."""

    def __init__(self) -> None:
        # _applications 是唯一真实来源，存储所有“已安装”的应用
        # 实际项目中，这应该从数据库或配置文件加载
        self._applications: dict[str, Application] = {}
        self._self_bot_ids_map: dict[str, str] = {}

    def set_self_bot_ids_map(self, bot_ids_map: dict[str, str]) -> None:
        """从外部注入 bot_id 映射."""
        self._self_bot_ids_map = bot_ids_map

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
