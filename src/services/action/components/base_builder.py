# src/services/action/components/base_builder.py
from abc import ABC, abstractmethod
from typing import Any
from xml.etree.ElementTree import Element

from aicarus_protocols import Event

# 导入依赖类型
from src.os.models import Window
from src.os.window_manager import WindowManager
from src.services.database.services.entity_graph_service import EntityGraphService
from src.services.database.services.event_storage_service import EventStorageService


class BasePlatformBuilder(ABC):
    """平台构建器的基类，定义了所有平台构建器的通用接口和属性."""

    # 定义渲染器接口，所有平台构建器都必须实现
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

    @property
    @abstractmethod
    def platform_id(self) -> str:
        """返回平台ID."""
        pass

    def get_action_definitions(self, window_manager: "WindowManager") -> dict:
        """返回平台提供的非UI动作定义.

        Args:
            window_manager: 窗口管理器实例，用于动态生成与窗口相关的 Schema。
        """
        return {}

    @abstractmethod
    def build_action_event(
        self, action_name: str, params: dict[str, Any], bot_id: str
    ) -> Event | None:
        """根据动作名称和参数，构建一个平台专属的、可执行的 Event 对象."""
        pass
