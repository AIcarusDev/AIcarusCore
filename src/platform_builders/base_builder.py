# src/platform_builders/base_builder.py
from abc import ABC, abstractmethod
from typing import Any


class BasePlatformBuilder(ABC):
    """平台构建器的基类，定义了所有平台构建器的通用接口和属性.

    这个类提供了一个统一的接口，所有具体平台的构建器都需要继承它.
    主要用于定义平台ID、获取层级动作和意识控制的JSON Schema定义和自然语言描述.
    """

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
