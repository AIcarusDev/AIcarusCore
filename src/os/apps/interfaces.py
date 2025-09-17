# src/os/apps/interfaces.py

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from typing import TYPE_CHECKING

    from src.bootstrap.container import ServiceContainer
    from src.os.models import Application
    from src.services.action.components.base_builder import BaseAppBuilder


class IAppDefinition(ABC):
    """应用的“静态清单”(Manifest)接口.

    每个应用目录都必须提供一个此接口的实现。
    """

    @property
    @abstractmethod
    def app_info(self) -> Application:
        """返回应用的静态信息（ID, name, title）。ID必须是稳定的，且在系统中唯一."""
        pass

    @abstractmethod
    def builder_class(self) -> type[BaseAppBuilder]:
        """返回该应用的构建器类（注意是类本身，不是实例）."""
        pass
        pass


class ISession(ABC):
    """定义一个通用会话接口 (Interface for a Session).

    所有应用的会话实现都必须遵循此接口，以便核心系统可以以统一的方式与之交互。
    """

    @property
    @abstractmethod
    def conversation_id(self) -> str:
        """返回会话的全局唯一标识符 (entity_uid)."""
        pass

    @property
    @abstractmethod
    def working_memory(self) -> dict[str, Any]:
        """提供对会话级短期工作记忆的访问.

        这块内存在会话激活期间持续存在，用于存储上下文相关的临时信息，
        例如“慢思考”的结论。
        """
        pass

    @working_memory.setter
    @abstractmethod
    def working_memory(self, value: dict[str, Any]) -> None:
        """设置会话的工作记忆."""
        pass

    @property
    @abstractmethod
    def conversation_name(self) -> str | None:
        """返回会话的可读名称，例如群名或好友昵称。如果不可用，则返回 None."""
        pass


class IApp(ABC):
    """定义一个通用应用接口 (Interface for an Application).

    所有应用构建器 (Builder) 都应实现此接口，以向核心系统提供获取会话实例的能力。
    """

    @abstractmethod
    async def get_session(
        self, conversation_uid: str, container: ServiceContainer
    ) -> ISession | None:
        """根据会话 UID 异步获取一个会话实例.

        Args:
            conversation_uid: 会话的全局唯一标识符 (entity_uid).
            container: 服务容器实例，用于按需创建依赖。

        Returns:
            一个实现了 ISession 接口的会话实例，如果找不到则返回 None。
        """
        pass
