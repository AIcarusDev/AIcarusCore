# src/tools/tool_registry.py
from typing import Callable, Coroutine, Any, Dict, List

# 导入所有可用的工具函数
from .web_searcher import search_web
from .failure_reporter import report_action_failure
from .platform_actions import send_reply_message
# 更多工具...

# 工具的 Schema 定义
# 这部分内容从 action_handler.py 中迁移过来
AVAILABLE_TOOLS_SCHEMA = [
    {
        "function_declarations": [
            {
                "name": "web_search",
                "description": "当需要从互联网查找最新信息、具体事实、定义、解释或任何当前未知的内容时使用此工具。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "要搜索的关键词或问题。"},
                        "max_results": {"type": "integer", "description": "期望返回的最大结果数量，默认为5。"}
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "report_action_failure",
                "description": "当一个明确提出的行动意图因为没有合适的工具时，使用此工具来生成一个反馈信息。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "reason_for_failure_short": {
                            "type": "string",
                            "description": "对动作失败原因的简短说明。",
                        }
                    },
                    "required": ["reason_for_failure_short"],
                },
            },
            {
                "name": "send_reply_message",
                "description": "当需要通过适配器向用户发送回复消息时使用此工具。例如，回答用户的问题，或在执行完一个动作后通知用户。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "target_user_id": {"type": "string", "description": "目标用户的ID (如果是私聊回复)。"},
                        "target_group_id": {"type": "string", "description": "目标群组的ID (如果是群聊回复)。"},
                        "message_content_text": {"type": "string", "description": "要发送的纯文本消息内容。"},
                        "reply_to_message_id": {
                            "type": "string",
                            "description": "[可选] 如果是回复特定消息，请提供原始消息的ID。",
                        },
                    },
                    "required": ["message_content_text"],
                },
            },
            # 其他工具的定义...
        ]
    }
]

# 工具名称到其实际异步函数实现的映射
# 注意：这里的key必须与上面Schema中定义的 'name' 完全一致
TOOL_FUNCTION_MAP: Dict[str, Callable[..., Coroutine[Any, Any, Any]]] = {
    "web_search": search_web,
    "report_action_failure": report_action_failure,
    "send_reply_message": send_reply_message,
    # 其他工具的映射...
}

def get_tool_schemas() -> List[Dict[str, Any]]:
    """返回所有可用工具的JSON Schema列表。"""
    return AVAILABLE_TOOLS_SCHEMA

def get_tool_function(name: str) -> Callable[..., Coroutine[Any, Any, Any]] | None:
    """根据名称获取工具的异步函数。"""
    return TOOL_FUNCTION_MAP.get(name)