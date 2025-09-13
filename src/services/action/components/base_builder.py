# 文件路径: src/services/action/components/base_builder.py

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any
from xml.etree.ElementTree import Element

from aicarus_protocols import Event

if TYPE_CHECKING:
    from src.bootstrap.container import ServiceContainer
    from src.os.models import Window
    from src.os.window_manager import WindowManager
    from src.services.database.services.entity_graph_service import EntityGraphService
    from src.services.database.services.event_storage_service import EventStorageService


class BaseAppBuilder(ABC):
    """应用构建器的基类，定义了所有应用的通用接口."""

    @property
    @abstractmethod
    def app_name(self) -> str:
        """返回应用的内部名称 (例如 'qq', 'termux')，用于关联Builder."""
        pass

    @abstractmethod
    async def on_before_start(self, container: ServiceContainer) -> tuple[bool, str | None]:
        """在应用启动前调用的钩子，用于执行前置检查."""
        # 默认实现为总是允许启动
        return True, None

    @abstractmethod
    async def on_after_start(self, container: ServiceContainer, app_id: str) -> Window:
        """在应用成功启动后调用的钩子，用于创建并返回应用的主窗口."""
        pass

    @abstractmethod
    async def render_window_content(
        self,
        parent_element: Element,
        current_path: list[str],
        window: Window,
        bot_ids_map: dict,
        entity_service: EntityGraphService,
        event_service: EventStorageService,
        ui_mapping: dict,
        generate_semantic_id: callable,
        image_collector: list[dict],
    ) -> None:
        """一个抽象方法，用于渲染特定平台窗口的内容.

        OS层的StateGenerator会调用此方法，将内容渲染的职责委托给具体的应用。
        """
        pass

    @abstractmethod
    async def render_popup_content(
        self,
        parent_element: Element,
        current_path: list[str],
        window: Window,
        bot_ids_map: dict,
        entity_service: EntityGraphService,
        event_service: EventStorageService,
        ui_mapping: dict,
        generate_semantic_id: callable,
        image_collector: list[dict],
    ) -> None:
        """渲染应用弹窗的内容."""
        pass

    async def get_action_definitions(
        self, window_manager: WindowManager, container: ServiceContainer
    ) -> dict:
        """返回此应用提供的、非UI绑定的、可供LLM调用的动作的JSON Schema.

        这是一个动态方法，因为可用的动作可能取决于当前窗口状态。
        """
        return {}

    @abstractmethod
    async def handle_llm_action(
        self, action_name: str, params: dict, container: ServiceContainer, thought_key: str
    ) -> None:
        """处理由LLM决策的、分发到此应用的特定动作."""
        pass

    def build_action_event(
        self, action_name: str, params: dict[str, Any], bot_id: str
    ) -> Event | None:
        """根据动作名称和参数，构建一个平台专属的、可执行的 Event 对象."""
        return None

    @property
    def needs_on_connect_inspection(self) -> bool:
        """告知 Core，此平台连接后是否需要执行安检。默认为 False."""
        return False

    async def run_on_connect_inspection(self, container: ServiceContainer) -> None:  # noqa: B027
        """由 CoreWebsocketServer 调用的、平台专属的安检流程.

        默认实现为空，需要安检的平台应重写此方法。
        """
        pass
