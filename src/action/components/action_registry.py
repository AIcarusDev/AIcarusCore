# src/action/components/action_registry.py
from collections.abc import Callable, Coroutine
from typing import Any

from src.action.action_provider import ActionProvider
from src.common.custom_logging.logging_config import get_logger

logger = get_logger(__name__)


class ActionRegistry:
    """负责管理和注册所有可用的动作。
    它从不同的 ActionProvider 加载动作，并提供一个统一的查询接口。
    """

    def __init__(self) -> None:
        self._action_registry: dict[str, Callable[..., Coroutine[Any, Any, Any]]] = {}
        logger.info(f"{self.__class__.__name__} instance created.")

    def register_provider(self, provider: ActionProvider) -> None:
        """从一个动作提供者注册其提供的所有动作。

        Args:
            provider: 实现了 ActionProvider 接口的实例。
        """
        actions = provider.get_actions()
        for action_name, action_func in actions.items():
            # 平台和内部工具不加前缀，其他提供者（如插件）使用 "provider_name.action_name" 格式
            full_action_name = (
                action_name
                if provider.name in ["platform", "internal"]
                else f"{provider.name}.{action_name}"
            )

            if full_action_name in self._action_registry:
                logger.warning(
                    f"动作 '{full_action_name}' 已存在，将被新的提供者 '{provider.name}' 覆盖。"
                )
            self._action_registry[full_action_name] = action_func
            logger.info(f"成功注册动作: {full_action_name} (来自: {provider.name})")

    def get_action(self, action_name: str) -> Callable[..., Coroutine[Any, Any, Any]] | None:
        """根据动作名称查找并返回对应的可调用动作函数。

        Args:
            action_name: 要查找的动作的完整名称。

        Returns:
            如果找到，则返回对应的异步函数；否则返回 None。
        """
        return self._action_registry.get(action_name)

    def get_all_actions(self) -> dict[str, Callable[..., Coroutine[Any, Any, Any]]]:
        """返回当前注册的所有动作的字典。

        Returns:
            一个从动作名称到动作函数的映射字典。
        """
        return self._action_registry.copy()
