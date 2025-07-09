# 新建文件: src/platform_builders/core_builder.py
# 这个文件只是一个示例方向，暂时没有实际功能。
# 细节会根据实际需求进行调整。
# 只是方向上的参考，具体逻辑和实现会在后续迭代中完善。
# -*- coding: utf-8 -*-
"""
# CoreBuilder 是一个平台构建器，用于处理核心系统内部的动作。
它不代表任何外部平台，而是负责将主意识的内部决策（如搜索、聚焦）
翻译成标准的、可被内部系统识别的 Event。
"""
import time
import uuid
from typing import Any

from aicarus_protocols import Event, Seg
from src.common.custom_logging.logging_config import get_logger
from src.config import config
from src.platform_builders.base_builder import BasePlatformBuilder

logger = get_logger(__name__)


class CoreBuilder(BasePlatformBuilder):
    """
    核心系统内部动作的构建器。
    它不代表任何外部平台，而是负责将主意识的内部决策（如搜索、聚焦）
    翻译成标准的、可被内部系统识别的 Event。
    """

    @property
    def platform_id(self) -> str:
        # 它的平台ID是特殊的 "core"，代表这是系统核心
        return "core"

    def build_action_event(self, action_name: str, params: dict[str, Any]) -> Event | None:
        """
        把核心动作翻译成标准Event。
        注意：这里的 event_type 是 action.core.{action_name}
        """
        # 为了简单，所有核心动作都用一个通用模板
        final_event_type = f"action.{self.platform_id}.{action_name}"
        action_seg = Seg(type="action_params", data=params)

        return Event(
            event_id=str(uuid.uuid4()),
            event_type=final_event_type,
            time=int(time.time() * 1000),
            bot_id=config.persona.bot_name or "Aicarus",
            content=[action_seg],
            conversation_info=None, # 核心动作通常不与特定会话绑定
        )
    # 这些动作只是示例方向，暂时没有启用
    def get_action_definitions(self) -> dict[str, Any]:
        """定义主意识唯一可用的“超能力”."""
        return {
            "web_search": {
                "type": "object",
                "description": "当需要获取未知信息、验证想法或对某个话题感到好奇时使用。",
                "properties": {
                    "query": {"type": "string", "description": "要搜索的关键词或问题。"},
                    "motivation": {"type": "string", "description": "你为什么要搜索这个。"},
                },
                "required": ["query", "motivation"],
            },
            "focus_on_conversation": { # 名字改得更清晰
                "type": "object",
                "description": "当你想查看某个会话的详细内容并可能进行互动时使用。",
                "properties": {
                    "conversation_id": {"type": "string", "description": "从<unread_summary>中选择你想聚焦的会话ID。"},
                    "motivation": {"type": "string", "description": "你为什么要关注这个会话。"},
                },
                "required": ["conversation_id", "motivation"],
            },
            "get_internal_list": { # 名字也改清晰
                "type": "object",
                "description": "获取你的好友列表或群聊列表（例如你想找人聊天但忘了QQ号）。",
                "properties": {
                    "list_type": {
                        "type": "string",
                        "enum": ["friend", "group"],
                        "description": "要获取的列表类型。",
                    },
                    "motivation": {"type": "string", "description": "你为什么要获取这个列表。"},
                },
                "required": ["list_type", "motivation"],
            },
            "do_nothing": {
                "type": "object",
                "description": "当你决定不采取任何外部行动，只想在内心默默思考时，选择此项。",
                "properties": {
                    "motivation": {"type": "string", "description": "你决定保持沉默的内心想法或原因。"},
                },
                "required": ["motivation"],
            }
        }