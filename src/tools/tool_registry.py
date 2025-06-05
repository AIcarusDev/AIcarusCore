# src/tools/tool_registry.py
from typing import Callable, Coroutine, Any, Dict, List

# 导入所有可用的工具函数
from .web_searcher import search_web
from .failure_reporter import report_action_failure
from .platform_actions import send_reply_message
# 更多工具...

# 【注意】AVAILABLE_TOOLS_SCHEMA 变量现在不再是必需的，
# 因为工具的描述和参数信息已经直接包含在
# src/action/prompts.py -> ACTION_DECISION_PROMPT_TEMPLATE 中了。
# 如果你希望保留它作为代码内部的参考或用于其他目的，可以不删除。
# 为了简化，这里我们将其注释掉或删除。

# 工具名称到其实际异步函数实现的映射
# 注意：这里的key必须与 ACTION_DECISION_PROMPT_TEMPLATE 中描述的工具名称完全一致
TOOL_FUNCTION_MAP: Dict[str, Callable[..., Coroutine[Any, Any, Any]]] = {
    "web_search": search_web,
    "report_action_failure": report_action_failure,
    "send_reply_message": send_reply_message,
    # 其他工具的映射...
}

# get_tool_schemas 函数也可以移除了，因为它依赖于 AVAILABLE_TOOLS_SCHEMA
# def get_tool_schemas() -> List[Dict[str, Any]]:
#     """返回所有可用工具的JSON Schema列表。"""
#     return AVAILABLE_TOOLS_SCHEMA

def get_tool_function(name: str) -> Callable[..., Coroutine[Any, Any, Any]] | None:
    """根据名称获取工具的异步函数。"""
    return TOOL_FUNCTION_MAP.get(name)