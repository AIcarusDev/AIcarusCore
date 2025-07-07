# src/action/providers/internal_tools_provider.py
from collections.abc import Callable, Coroutine
from typing import Any

from src.action.action_provider import ActionProvider
from src.tools.tool_registry import get_all_tools


class InternalToolsProvider(ActionProvider):
    """提供对内部工具的访问。
    它从工具注册表中获取所有可用的内部工具，并将其作为动作提供。
    """

    @property
    def name(self) -> str:
        return "internal"

    def get_actions(self) -> dict[str, Callable[..., Coroutine[Any, Any, Any]]]:
        """返回所有已注册的内部工具."""
        return get_all_tools()

    # --- ❤❤❤ 新增这个方法，让我也能提供“服务价目表”！❤❤❤ ---
    def get_action_definitions(self) -> dict[str, Any]:
        """为内部工具生成参数定义。
        理想情况下，每个工具函数自己都应该有一个schema定义。
        这里我们先做一个简化的实现。
        """
        # TODO: 未来让每个工具函数通过装饰器等方式提供自己的schema
        # 目前，我们为已知的工具硬编码schema

        definitions = {
            "web_search": {
                "type": "object",
                "description": "当需要从互联网查找最新信息、事实、定义、解释或任何当前未知的内容时使用此工具。",
                "properties": {
                    "query": {"type": "string", "description": "要搜索的关键词或问题。"},
                    "max_results": {
                        "type": "integer",
                        "description": "期望返回的最大结果数量 (可选, 默认 5)。",
                    },
                },
                "required": ["query"],
            },
            "report_action_failure": {
                "type": "object",
                "description": "当用户的行动意图非常模糊，或现有任何工具都无法实现它时调用此工具。",
                "properties": {
                    "reason_for_failure_short": {
                        "type": "string",
                        "description": "简要说明为什么这个动作无法执行。",
                    }
                },
                "required": ["reason_for_failure_short"],
            },
        }

        # 为其他未明确定义的工具提供一个通用schema
        all_tools = self.get_actions()
        for tool_name, tool_func in all_tools.items():
            if tool_name not in definitions:
                definitions[tool_name] = {
                    "type": "object",
                    "description": f"执行内部工具: {tool_name}。Doc: {tool_func.__doc__ or '无描述'}",
                    "properties": {
                        "params": {"type": "object", "description": "工具所需的参数字典。"}
                    },
                }
        return definitions
