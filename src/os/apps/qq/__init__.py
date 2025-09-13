# 文件路径: src/os/apps/qq/__init__.py

from src.os.apps.interfaces import IAppDefinition
from src.os.apps.qq.builder import QQBuilder
from src.os.models import Application


class QQAppDefinition(IAppDefinition):
    """QQ 应用的静态清单实现."""

    @property
    def app_info(self) -> Application:
        """返回 QQ 应用的信息.

        Returns:
            Application: 包含 QQ 应用信息的 Application 实例。
        """
        # ID 和 name 的定义被内聚到了应用自身
        # ID 是稳定且唯一的，name 用于关联 Builder
        return Application(id="app-qq", name="qq", title="QQ")

    @property
    def builder_class(self) -> type[QQBuilder]:  # noqa: D102
        return QQBuilder


# 关键：导出一个名为 `app_definition` 的实例，供应用管理器发现
app_definition = QQAppDefinition()
