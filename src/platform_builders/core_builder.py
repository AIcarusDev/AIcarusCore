# ruff: noqa: E501
# src/platform_builders/core_builder.py (修改后的完整文件)
from typing import Any, ClassVar

from aicarus_protocols import Event
from src.common.custom_logging.logging_config import get_logger
from src.platform_builders.base_builder import BasePlatformBuilder

logger = get_logger(__name__)


class CoreBuilder(BasePlatformBuilder):
    """核心系统内部动作和意识控制的构建器."""

    # --- v2.0 全新指令集的 JSON Schema 定义 ---
    _CONSCIOUSNESS_CONTROLS_DEFINITIONS: ClassVar = {
        "push_focus": {
            "type": "object",
            "description": "将注意力聚焦到指定的目标（平台或会话）。",
            "properties": {
                "target_id": {
                    "type": "string",
                    "description": "要聚焦的目标ID。例如平台ID 'qq' 或会话ID '123456'。",
                },
                "motivation": {"type": "string"},
            },
            "required": ["target_id", "motivation"],
        },
        "pop_focus": {
            "type": "object",
            "description": "从当前注意力焦点返回。例如从当前会话返回到会话所属的平台，或退出当前平台。",
            "properties": {"motivation": {"type": "string"}},
            "required": ["motivation"],
        },
        "swap_focus": {
            "type": "object",
            "description": "将你的注意力从当前会话切换到另一个会话。",
            "properties": {
                "target_id": {
                    "type": "string",
                    "description": "要切换到的新会话ID。",
                },
                "motivation": {"type": "string"},
            },
            "required": ["target_id", "motivation"],
        },
        "teleport_focus": {
            "type": "object",
            "description": "直接将你的注意力聚焦到指定的目标。",
            "properties": {
                "target_path": {
                    "type": "string",
                    "description": "要聚焦的绝对路径，必须是使用'.'作为分隔符的完整路径，例如`qq.123456`。",
                },
                "motivation": {"type": "string"},
            },
            "required": ["target_path", "motivation"],
        },
        "back": {
            "type": "object",
            "description": "将你的注意力拉回到`<navigation_log>`中的上一个注意力焦点(T-1)。",
            "properties": {"motivation": {"type": "string"}},
            "required": ["motivation"],
        },
        "jump_to_history": {
            "type": "object",
            "description": "根据`<navigation_log>`，直接跳转到由`history_index`指定的历史焦点。",
            "properties": {
                "history_index": {
                    "type": "integer",
                    "description": "导航日志中的时间索引 (例如 T-2 的索引是 -2)。",
                },
                "motivation": {"type": "string", "description": "你为什么要进行这次“跳跃”？"},
            },
            "required": ["history_index", "motivation"],
        },
    }

    # --- 核心动作定义保持不变 ---
    _ACTIONS_DEFINITIONS: ClassVar = {
        "web_search": {
            "type": "object",
            "properties": {"query": {"type": "string"}, "motivation": {"type": "string"}},
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

    def build_action_event(
        self, action_name: str, params: dict[str, Any], bot_id: str
    ) -> Event | None:
        """根据动作名称和参数，构建一个平台专属的、可执行的 Event 对象."""
        logger.warning(f"CoreBuilder 的 build_action_event 被意外调用！Action: {action_name}。")
        return None

    def get_level_consciousness_controls_definitions(
        self, level: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """根据指定的层级，返回该层级可用的和【内在控制】的JSON Schema定义."""
        props = {}
        if level == "core":
            props["push_focus"] = self._CONSCIOUSNESS_CONTROLS_DEFINITIONS["push_focus"]
        elif level == "platform":
            props["push_focus"] = self._CONSCIOUSNESS_CONTROLS_DEFINITIONS["push_focus"]
            props["pop_focus"] = self._CONSCIOUSNESS_CONTROLS_DEFINITIONS["pop_focus"]
        elif level == "cellular":
            props["pop_focus"] = self._CONSCIOUSNESS_CONTROLS_DEFINITIONS["pop_focus"]
            props["swap_focus"] = self._CONSCIOUSNESS_CONTROLS_DEFINITIONS["swap_focus"]

        # back 和 jump_to_history 在任何层级都可用
        props["back"] = self._CONSCIOUSNESS_CONTROLS_DEFINITIONS["back"]
        props["jump_to_history"] = self._CONSCIOUSNESS_CONTROLS_DEFINITIONS["jump_to_history"]

        # teleport_focus 也应该是全局可用的
        props["teleport_focus"] = self._CONSCIOUSNESS_CONTROLS_DEFINITIONS["teleport_focus"]

        schema = {"type": "object", "properties": props, "maxProperties": 1}
        return schema, {}

    def get_level_consciousness_controls_descriptions(self, level: str) -> str:
        """返回核心平台的意识控制描述."""
        descs = [
            "    - `push_focus(target_id, motivation)`: 深入到下一层焦点。只需提供目标ID。",
            "    - `pop_focus(motivation)`: 从当前焦点返回上一层。",
            "    - `swap_focus(target_id, motivation)`: 平级切换到另一个会话，只需提供目标会话ID。",
            "    - `teleport_focus(target_path, motivation)`: 强制跳转焦点。注意 `target_path` 必须是使用'.'分隔的完整路径！",
            "    - `back(motivation)`: 回溯到上一个焦点 (T-1)。",
            "    - `jump_to_history(history_index, motivation)`: 跳转到指定的历史焦点。",
        ]

        return "\n".join(descs)

    def get_level_actions_definitions(self, level: str) -> tuple[dict[str, Any], dict[str, Any]]:
        """核心平台的动作定义保持不变，只提供 web_search 动作."""
        props = {"web_search": self._ACTIONS_DEFINITIONS["web_search"]}
        schema = {"type": "object", "properties": props}
        return schema, {}

    def get_level_actions_descriptions(self, level: str) -> str:
        """返回核心平台的动作描述."""
        return self._ACTIONS_DESCRIPTIONS["web_search"]
