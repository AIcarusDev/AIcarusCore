# src/action/action_provider.py
from abc import ABC, abstractmethod
from collections.abc import Callable, Coroutine
from typing import Any


class ActionProvider(ABC):
    """动作提供者的抽象基类。
    所有动作子模块（插件）都应继承此类，以向 ActionHandler 注册其能力。
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """返回该 Provider 的唯一名称。
        这通常是一个层级化的名称，例如 'platform.qq' 或 'internal.web'。
        ActionHandler 将使用这个名称作为动作的前缀。
        """
        pass

    @abstractmethod
    def get_actions(self) -> dict[str, Callable[..., Coroutine[Any, Any, Any]]]:
        """返回一个字典，其中包含了该提供者支持的所有动作。
        - key (str): 动作的名称 (例如 'send_message', 'search')。
        - value (Callable): 一个异步函数，用于执行该动作。

        完整的动作名称将由 Provider.name 和这里的 key 组合而成，
        例如 'platform.qq.send_message'。
        """
        pass
