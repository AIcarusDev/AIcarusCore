from typing import Any, ClassVar

from aicarus_protocols import Event
from src.common.custom_logging.logging_config import get_logger
from src.platform_builders.base_builder import BasePlatformBuilder

logger = get_logger(__name__)


class CoreBuilder(BasePlatformBuilder):
    """核心系统内部动作和意识控制的构建器.

    负责定义所有层级通用的核心能力（如web_search）以及意识导航指令.
    """

    # --- 统一存储所有可能的定义和描述 ---

    _CONSCIOUSNESS_CONTROLS_DEFINITIONS: ClassVar = {
        "focus": {
            "type": "object",
            "properties": {
                "conversation_id": {"type": "string"},
                "motivation": {"type": "string"}
            },
            "required": ["conversation_id", "motivation"],
        },
        "return": {
            "type": "object",
            "properties": {
                "motivation": {"type": "string"}
            },
            "required": ["motivation"],
        },
        "shift": {
            "type": "object",
            "properties": {
                "conversation_id": {"type": "string"},
                "motivation": {"type": "string"}
            },
            "required": ["conversation_id", "motivation"],
        },
        "peek": {
            "type": "object",
            "properties": {
                "conversation_id": {"type": "string"},
                "motivation": {"type": "string"}
            },
            "required": ["conversation_id", "motivation"],
        },
    }

    _CONSCIOUSNESS_CONTROLS_DESCRIPTIONS: ClassVar = {
        "focus_platform": "- `focus`: 深入到一个具体的平台，需要提供目标平台的ID。",
        "focus_conversation": "- `focus`: 深入到当前平台下一个具体的会话，需要提供目标会话的ID。",
        "shift": "- `shift`: 在当前层级进行横向移动，例如从一个会话切换到另一个会话。",
        "peek": "- `peek`: “窥视”另一个会话或平台的信息，但保持当前的主要注意力焦点不变。",  # noqa: E501
        "shift": "- `shift`: 将注意力从当前会话，直接转移到本平台内的另一个会话，需要提供目标会话的ID。",  # noqa: E501
        "return_from_cellular": "- `return(motivation)`: 暂时退出当前会话，返回到平台。",
        "return_from_platform": "- `return(motivation)`: 暂时退出当前平台。",
    }

    _ACTIONS_DEFINITIONS: ClassVar = {
        "web_search": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "motivation": {"type": "string"}
            },
            "required": ["query", "motivation"],
        }
    }

    _ACTIONS_DESCRIPTIONS: ClassVar = {
        "web_search": "    - `web_search`: 进行一次互联网搜索，以获取外部信息。"
    }

    @property
    def platform_id(self) -> str:
        """返回平台ID."""
        return "core"

    def build_action_event(self, action_name: str, params: dict[str, Any], bot_id: str) -> Event | None:
        """
        核心构建器不负责构建发送给外部适配器的Event。
        核心动作（如web_search）由ActionHandler内部直接处理。
        这个方法只是为了满足抽象基类的接口要求。
        """
        logger.warning(
            f"CoreBuilder 的 build_action_event 被意外调用！"
            f"Action: {action_name}, Params: {params}。"
            "这通常不应该发生。"
        )
        # 核心动作不通过这种方式构建事件，所以返回None
        return None

    def get_level_consciousness_controls_definitions(
        self, level: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """根据层级，提供可用的意识控制JSON Schema."""
        props = {}
        if level == "core":
            props["focus"] = {  # 顶层用 platform_id
                "type": "object",
                "properties": {
                    "platform_id": {"type": "string"},
                    "motivation": {"type": "string"}
                },
                "required": ["platform_id", "motivation"],
            }
        elif level == "platform":
            props["focus"] = {  # 中层用 conversation_id
                "type": "object",
                "properties": {
                    "conversation_id": {"type": "string"},
                    "motivation": {"type": "string"},
                },
                "required": ["conversation_id", "motivation"],
            }
            props["return"] = self._CONSCIOUSNESS_CONTROLS_DEFINITIONS["return"]
        elif level == "cellular":
            props["return"] = self._CONSCIOUSNESS_CONTROLS_DEFINITIONS["return"]
            props["shift"] = self._CONSCIOUSNESS_CONTROLS_DEFINITIONS["shift"]

        schema = {"type": "object", "properties": props, "maxProperties": 1} if props else {}
        return schema, {}

    def get_level_consciousness_controls_descriptions(self, level: str) -> str:
        """根据层级，提供可用的意识控制自然语言描述."""
        descs = []
        if level == "core":
            descs.append(
                "    - `focus(platform_id, motivation)`: 专注于一个具体的平台，需要提供目标平台的ID。"
            )
        elif level == "platform":
            descs.append(
                "    - `focus(conversation_id, motivation)`: "
                "深入到本平台下一个具体的会话，需要提供目标会话的ID。"
            )
            descs.append(self._CONSCIOUSNESS_CONTROLS_DESCRIPTIONS["return_from_platform"])
        elif level == "cellular":
            descs.append(self._CONSCIOUSNESS_CONTROLS_DESCRIPTIONS["return_from_cellular"])
            descs.append(self._CONSCIOUSNESS_CONTROLS_DESCRIPTIONS["shift"])

        return "\n".join(descs) or "你当前没有可用的导航指令。"

    def get_level_actions_definitions(self, level: str) -> tuple[dict[str, Any], dict[str, Any]]:
        """为所有层级提供核心动作（如web_search）的JSON Schema."""
        # web_search 在所有层级都可用
        props = {"web_search": self._ACTIONS_DEFINITIONS["web_search"]}
        schema = {"type": "object", "properties": props}
        return schema, {}

    def get_level_actions_descriptions(self, level: str) -> str:
        """为所有层级提供核心动作（如web_search）的自然语言描述."""
        return self._ACTIONS_DESCRIPTIONS["web_search"]
