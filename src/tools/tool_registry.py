# src/tools/tool_registry.py
from collections.abc import Callable, Coroutine
from typing import Any

from .failure_reporter import report_action_failure

# 导入所有可用的工具函数
from .web_searcher import search_web

# 工具名称到其实际异步函数实现的映射
# 注意：这里的key必须与 ACTION_DECISION_PROMPT_TEMPLATE 中描述的工具名称完全一致
TOOL_FUNCTION_MAP: dict[str, Callable[..., Coroutine[Any, Any, Any]]] = {
    "web_search": search_web,
    "report_action_failure": report_action_failure,
    # 其他工具的映射...
}


def get_tool_function(name: str) -> Callable[..., Coroutine[Any, Any, Any]] | None:
    """根据名称获取工具的异步函数."""
    return TOOL_FUNCTION_MAP.get(name)


def get_all_tools() -> dict[str, Callable[..., Coroutine[Any, Any, Any]]]:
    """获取所有可用工具的名称到函数的映射字典."""
    return TOOL_FUNCTION_MAP
