# src/platform_builders/base_builder.py
from abc import ABC, abstractmethod
from typing import Any

from aicarus_protocols import Event


class BasePlatformBuilder(ABC):
    """平台构建器的基类，定义了所有平台构建器的通用接口和属性.

    这个类提供了一个统一的接口，所有具体平台的构建器都需要继承它.
    主要用于定义平台ID、获取层级动作和意识控制的JSON Schema定义和自然语言描述.
    """

    @property
    def needs_on_connect_inspection(self) -> bool:
        """声明此平台是否需要在连接时进行“上线安检”.

        默认返回 False.
        """
        return False

    @property
    def is_person_platform(self) -> bool:
        """声明此平台是否代表一个具有社交身份的“人物”或“马甲”.

        默认返回 False.
        对于纯粹的功能扩展或工具类平台（如Termux），应返回 False.
        """
        return False

    @property
    def is_tool_platform(self) -> bool:
        """这个平台是否是一个纯粹的“工具平台”?

        如果是，它的能力应该在更高层级就被展示出来。
        默认返回 False.
        """
        return False

    @property
    @abstractmethod
    def platform_id(self) -> str:
        """返回平台ID."""
        pass

    @abstractmethod
    def get_level_actions_definitions(self, level: str) -> tuple[dict[str, Any], dict[str, Any]]:
        """根据指定的层级，返回该层级可用的和【外部行动】的JSON Schema定义.

        Args:
            level (str): 当前的焦点层级 ('core', 'platform', 'cellular').

        Returns:
            一个元组 (external_actions_schema).
            每个schema都是一个字典，其 'properties' 键下包含了该层级所有可用动作的schema.
        """
        pass

    @abstractmethod
    def build_action_event(
        self, action_name: str, params: dict[str, Any], bot_id: str
    ) -> Event | None:
        """根据动作名称和参数，构建一个平台专属的、可执行的 Event 对象.

        Args:
            action_name (str): 动作的名称.
            params (dict[str, Any]): 动作所需的参数.
            bot_id (str): 执行此动作的自身的平台ID.

        Returns:
            一个封装好的 Event 对象，如果无法构建则返回 None.
        """
        pass

    @abstractmethod
    def get_level_actions_descriptions(self, level: str) -> str:
        """根据指定的层级，返回该层级可用动作的【自然语言描述】.

        Args:
            level (str): 当前的焦点层级 ('core', 'platform', 'cellular').

        Returns:
            一段格式化好的、供注入Prompt的字符串.
        """
        pass

    @abstractmethod
    def get_level_consciousness_controls_definitions(
        self, level: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """根据指定的层级，返回该层级可用的和【内在控制】的JSON Schema定义.

        Args:
            level (str): 当前的焦点层级 ('core', 'platform', 'cellular').

        Returns:
            一个元组 (consciousness_controls_schema).
            每个schema都是一个字典，其 'properties' 键下包含了该层级所有可用内在控制的schema.
        """
        pass

    @abstractmethod
    def get_level_consciousness_controls_descriptions(self, level: str) -> str:
        """根据指定的层级，返回该层级可用内在控制的【自然语言描述】.

        Args:
            level (str): 当前的焦点层级 ('core', 'platform', 'cellular').

        Returns:
            一段格式化好的、供注入Prompt的字符串.
        """
        pass
