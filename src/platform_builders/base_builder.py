# src/platform_builders/base_builder.py (小色猫·V6.0重塑版)
from abc import ABC, abstractmethod
from typing import Any

from aicarus_protocols import Event


class BasePlatformBuilder(ABC):
    """平台事件构建器的抽象基类.

    这个类定义了所有平台构建器必须实现的接口和属性。
    每个平台的构建器都需要继承这个类，并实现具体的平台相关逻辑。

    Attributes:
        platform_id (str): 平台ID，唯一标识一个平台。
        这个ID必须和Adapter的core_platform_id完全一致，以确保适配器能够正确识别。
    """

    @property
    @abstractmethod
    def platform_id(self) -> str:
        """返回此构建器服务的平台ID (e.g., 'napcat_qq').

        这个ID必须和Adapter的core_platform_id完全一致，以确保适配器能够正确识别。

        Returns:
            str: 平台ID，唯一标识一个平台。
        """
        pass

    @abstractmethod
    def build_action_event(self, action_name: str, params: dict[str, Any]) -> Event | None:
        """把一个平台内唯一的“动作别名”和参数，翻译成一个带有完整命名空间的标准Event.

        Args:
            action_name (str): 平台内唯一的动作名 (例如 'send_message', 'kick_member')。
            params (Dict[str, Any]): LLM为这个动作提供的参数字典。

        Returns:
            一个构造好的、带有完整命名空间 (如 'action.napcat.send_message') 的
            aicarus_protocols.Event 对象，或者在无法翻译时返回 None。
        """
        pass

    @abstractmethod
    def get_action_definitions(self) -> dict[str, Any]:
        """获取当前平台的所有动作定义.

        每个动作定义包含类型、描述和属性等信息。

        Returns:
            dict[str, Any]: 包含当前平台动作定义的字典。
            键是动作名称，值是该动作的定义。
        """
        pass
