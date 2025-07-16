from typing import Any

from src.common.custom_logging.logging_config import get_logger
from src.platform_builders.base_builder import BasePlatformBuilder

logger = get_logger(__name__)


class CoreBuilder(BasePlatformBuilder):
    """核心系统内部动作和意识控制的构建器。
    负责定义所有层级通用的核心能力（如web_search）以及意识导航指令。
    """

    # --- 统一存储所有可能的定义和描述 ---

    _CONSCIOUSNESS_CONTROLS_DEFINITIONS = {
        "focus": {
            "type": "object",
            "properties": {"conversation_id": {"type": "string"}, "motivation": {"type": "string"}},
            "required": ["conversation_id", "motivation"]
        },
        "return": {
            "type": "object",
            "properties": {"motivation": {"type": "string"}},
            "required": ["motivation"]
        },
        "shift": {
            "type": "object",
            "properties": {"conversation_id": {"type": "string"}, "motivation": {"type": "string"}},
            "required": ["conversation_id", "motivation"]
        },
        "peek": {
            "type": "object",
            "properties": {"conversation_id": {"type": "string"}, "motivation": {"type": "string"}},
            "required": ["conversation_id", "motivation"]
        }
    }

    _CONSCIOUSNESS_CONTROLS_DESCRIPTIONS = {
        "focus_platform": "- `focus`: 深入到一个具体的平台，需要提供目标平台的ID。",
        "focus_conversation": "- `focus`: 深入到当前平台下一个具体的会话，需要提供目标会话的ID。",
        "return": "- `return`: 返回到上一个层级。在底层时返回中层，在中层时返回顶层。",
        "shift": "- `shift`: 在当前层级进行横向移动，例如从一个会话切换到另一个会话。",
        "peek": "- `peek`: “窥视”另一个会话或平台的信息，但保持当前的主要注意力焦点不变。（高阶功能）",
        "shift": "- `shift`: 将注意力从当前会话，直接转移到本平台内的另一个会话，需要提供目标会话的ID。",
        "return_from_cellular": "- `return(motivation)`: 暂时退出当前会话，返回到平台。",
        "return_from_platform": "- `return(motivation)`: 暂时退出当前平台。",
    }

    _ACTIONS_DEFINITIONS = {
        "web_search": {
            "type": "object",
            "properties": {"query": {"type": "string"}, "motivation": {"type": "string"}},
            "required": ["query", "motivation"]
        }
    }

    _ACTIONS_DESCRIPTIONS = {
        "web_search": "- `web_search`: 进行一次互联网搜索，以获取外部信息。"
    }

    @property
    def platform_id(self) -> str:
        return "core"

    def get_level_consciousness_controls_definitions(self, level: str) -> tuple[dict[str, Any], dict[str, Any]]:
        """根据层级，提供可用的意识控制JSON Schema。"""
        props = {}
        if level == "core":
            props["focus"] = { # 顶层用 platform_id
                "type": "object",
                "properties": {"platform_id": {"type": "string"}, "motivation": {"type": "string"}},
                "required": ["platform_id", "motivation"]
            }
        elif level == "platform":
            props["focus"] = { # 中层用 conversation_id
                "type": "object",
                "properties": {"conversation_id": {"type": "string"}, "motivation": {"type": "string"}},
                "required": ["conversation_id", "motivation"]
            }
            props["return"] = self._CONSCIOUSNESS_CONTROLS_DEFINITIONS["return"]
        elif level == "cellular":
            props["return"] = self._CONSCIOUSNESS_CONTROLS_DEFINITIONS["return"]
            props["shift"] = self._CONSCIOUSNESS_CONTROLS_DEFINITIONS["shift"]

        schema = {"type": "object", "properties": props, "maxProperties": 1} if props else {}
        return schema, {}

    def get_level_consciousness_controls_descriptions(self, level: str) -> str:
        descs = []
        if level == "core":
            descs.append("- `focus(platform_id, motivation)`: 专注于一个具体的平台，需要提供目标平台的名称。")
        elif level == "platform":
            descs.append("- `focus(conversation_id, motivation)`: 深入到本平台下一个具体的会话，需要提供目标会话的ID。")
            descs.append(self._CONSCIOUSNESS_CONTROLS_DESCRIPTIONS["return_from_platform"])
        elif level == "cellular":
            descs.append(self._CONSCIOUSNESS_CONTROLS_DESCRIPTIONS["return_from_cellular"])
            descs.append(self._CONSCIOUSNESS_CONTROLS_DESCRIPTIONS["shift"])

        return "\n".join(descs) or "你当前没有可用的导航指令。"

    def get_level_actions_definitions(self, level: str) -> tuple[dict[str, Any], dict[str, Any]]:
        """为所有层级提供核心动作（如web_search）的JSON Schema。"""
        # web_search 在所有层级都可用
        props = {"web_search": self._ACTIONS_DEFINITIONS["web_search"]}
        schema = {"type": "object", "properties": props}
        return schema, {}

    def get_level_actions_descriptions(self, level: str) -> tuple[str, str]:
        """为所有层级提供核心动作（如web_search）的自然语言描述。"""
        return self._ACTIONS_DESCRIPTIONS["web_search"], ""
