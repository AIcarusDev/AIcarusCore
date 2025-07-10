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
        return "core"

    def get_level_specific_definitions(self, level: str) -> tuple[dict, dict]:
        # 在顶层，只有 'core' 这一个层级
        if level == "core":
            controls_schema = {
                "type": "object",
                "properties": {
                    "focus": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "motivation": {"type": "string"},
                        },
                        "required": ["path", "motivation"],
                    }
                },
                "maxProperties": 1
            }
            actions_schema = {
                "type": "object",
                "properties": {
                    "web_search": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string"},
                            "motivation": {"type": "string"},
                        },
                        "required": ["query", "motivation"],
                    }
                }
            }
            return controls_schema, actions_schema
        return {}, {} # 其他层级无可用动作

    def get_level_specific_descriptions(self, level: str) -> tuple[str, str]:
        if level == "core":
            controls_desc = "- `focus`: 当你想查看某个平台的详细情况时（例如QQ），使用此指令将注意力聚焦于该平台。"
            actions_desc = "- `web_search`: 当需要获取未知信息、验证想法或对某个话题感到好奇时使用。"
            return controls_desc, actions_desc
        return "无", "无"