# src/platform_builders/base_builder.py (小色猫·V6.0重塑版)
from abc import ABC, abstractmethod
from typing import Any, Dict, Tuple

class BasePlatformBuilder(ABC):
    @property
    @abstractmethod
    def platform_id(self) -> str:
        pass

    @abstractmethod
    def get_level_specific_definitions(self, level: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """
        根据指定的层级，返回该层级可用的【意识控制】和【外部行动】的JSON Schema定义。

        Args:
            level (str): 当前的焦点层级 ('core', 'platform', 'cellular')。

        Returns:
            一个元组 (consciousness_controls_schema, external_actions_schema)。
            每个schema都是一个字典，其 'properties' 键下包含了该层级所有可用动作的schema。
        """
        pass

    @abstractmethod
    def get_level_specific_descriptions(self, level: str) -> Tuple[str, str]:
        """
        根据指定的层级，返回该层级可用动作的【自然语言描述】。

        Args:
            level (str): 当前的焦点层级 ('core', 'platform', 'cellular')。

        Returns:
            一个元组 (consciousness_controls_description, external_actions_description)。
            每个description都是一段格式化好的、供注入Prompt的字符串。
        """
        pass
