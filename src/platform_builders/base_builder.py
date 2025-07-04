# src/platform_builders/base_builder.py (小色猫·V6.0重塑版)
from abc import ABC, abstractmethod
from typing import Any

from aicarus_protocols import Event


class BasePlatformBuilder(ABC):
    """
    平台事件构建器的抽象基类 (V6.0 命名空间统治版)。
    所有平台的“翻译官”都得有这张新版资格证，不然直接下岗！
    """

    @property
    @abstractmethod
    def platform_id(self) -> str:
        """
        返回此构建器服务的平台ID (e.g., 'napcat')。
        必须和你 Adapter 在 Core 注册的 ID 一模一样，懂？
        """
        pass

    @abstractmethod
    def build_action_event(self, action_name: str, params: dict[str, Any]) -> Event | None:
        """
        【全新职责】把一个平台内唯一的“动作别名”和参数，翻译成一个带有完整命名空间的标准Event。
        这是最重要的活儿，干不好就滚蛋！

        Args:
            action_name (str): 平台内唯一的动作名 (例如 'send_message', 'kick_member')。
            params (Dict[str, Any]): LLM为这个动作提供的参数字典。

        Returns:
            一个构造好的、带有完整命名空间 (如 'action.napcat.send_message') 的 aicarus_protocols.Event 对象，
            或者在无法翻译时返回 None。
        """
        pass

    @abstractmethod
    def get_action_definitions(self) -> dict[str, Any]:
        """
        【全新职责】提供一份你这个平台所有“玩法”的参数定义清单。
        返回一个字典，key是动作别名(如'send_message')，value是该动作的JSON Schema参数定义。
        这是给ActionHandler用来动态构建给LLM的超级工具的，写不好LLM就看不懂你！
        """
        pass
