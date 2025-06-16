# src/action/providers/internal_tools_provider.py
from collections.abc import Callable, Coroutine
from typing import Any

from src.action.action_provider import ActionProvider
from src.tools.tool_registry import get_all_tools


class InternalToolsProvider(ActionProvider):
    """
    提供对内部工具的访问。
    它从工具注册表中获取所有可用的内部工具，并将其作为动作提供。
    """

    @property
    def name(self) -> str:
        return "internal"

    def get_actions(self) -> dict[str, Callable[..., Coroutine[Any, Any, Any]]]:
        """
        返回所有已注册的内部工具。
        """
        # get_all_tools() 应该返回一个类似 {'tool_name': tool_function} 的字典
        # 我们假设 tool_registry 已经处理好了工具的加载
        # 注意：get_tool_function 在这里可能不是最合适的，更好的方式是有一个
        # get_all_tools 的函数来一次性获取所有工具。我们先假设有这样一个函数。
        # 如果没有，我们就直接从 tool_registry.py 导入那个工具字典。
        # 看了下 tool_registry.py，它有一个 get_all_tools 函数。

        return get_all_tools()
