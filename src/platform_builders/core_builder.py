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
            "description": "【下潜/聚焦】深入到下一层焦点。将 target_path 指定的目标压入注意力堆栈顶部。",
            "properties": {
                "target_path": {
                    "type": "string",
                    "description": "要“潜入”的目标路径。可以是绝对路径(如`qq.123456`)或相对路径(如`123456`)。",
                },
                "motivation": {"type": "string", "description": "你为什么要进入这一层？"},
            },
            "required": ["target_path", "motivation"],
        },
        "pop_focus": {
            "type": "object",
            "description": "【上浮/返回】从当前焦点返回。从注意力堆栈顶部弹出现有焦点，返回到结构上的上一层。",
            "properties": {
                "motivation": {"type": "string", "description": "你为什么要离开当前层级？"}
            },
            "required": ["motivation"],
        },
        "swap_focus": {
            "type": "object",
            "description": "【平级切换焦点】替换堆栈顶部的当前焦点为另一个同层级的目标，不改变堆栈深度。",
            "properties": {
                "target_path": {
                    "type": "string",
                    "description": "要切换到的新会话ID或同层级路径。",
                },
                "motivation": {"type": "string", "description": "你为什么要切换到这个目标？"},
            },
            "required": ["target_path", "motivation"],
        },
        "teleport_focus": {
            "type": "object",
            "description": "【强制跳转焦点】清空当前的整个注意力堆栈，然后将指定的目标路径作为新的唯一焦点。",
            "properties": {
                "target_path": {
                    "type": "string",
                    "description": "要传送到的绝对路径，例如`qq.group.88888888`。",
                },
                "motivation": {"type": "string", "description": "是什么情况让你必须使用此指令？"},
            },
            "required": ["target_path", "motivation"],
        },
        "back": {
            "type": "object",
            "description": "【回溯到上一个焦点】根据<navigation_log>，将焦点设置到历史记录中的前一个位置 (T-1)。",
            "properties": {
                "motivation": {"type": "string", "description": "你为什么要退回上一步？"}
            },
            "required": ["motivation"],
        },
        "jump_to_history": {
            "type": "object",
            "description": "【跳转到指定的历史焦点】根据<navigation_log>，直接跳转到由`history_index`指定的历史焦点。",
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

        schema = {"type": "object", "properties": props, "maxProperties": 1} if props else {}
        return schema, {}

    def get_level_consciousness_controls_descriptions(self, level: str) -> str:
        """返回核心平台的意识控制描述."""
        descs = []
        # --- v2.0 全新指令集的自然语言描述 ---
        descs.append(
            "    - `push_focus(target_path, motivation)`: 深入到下一层焦点。将 `target_path` 指定的目标压入注意力堆栈顶部。"
        )
        descs.append(
            "    - `pop_focus(motivation)`: 从当前焦点返回。从注意力堆栈顶部弹出现有焦点，返回到结构上的上一层。"
        )
        descs.append(
            "    - `swap_focus(target_path, motivation)`: 平级切换焦点。替换堆栈顶部的当前焦点为另一个同层级的目标，不改变堆栈深度。"
        )
        descs.append(
            "    - `teleport_focus(target_path, motivation)`: 强制跳转焦点。清空当前的整个注意力堆栈，然后将指定的目标路径作为新的唯一焦点。"
        )
        descs.append(
            "    - `back(motivation)`: 回溯到上一个焦点。根据`<navigation_log>`，将焦点设置到历史记录中的前一个位置 (T-1)。"
        )
        descs.append(
            "    - `jump_to_history(history_index, motivation)`: 跳转到指定的历史焦点。根据`<navigation_log>`，直接跳转到由`history_index`指定的历史焦点。"
        )

        return "\n".join(descs)

    def get_level_actions_definitions(self, level: str) -> tuple[dict[str, Any], dict[str, Any]]:
        """核心平台的动作定义保持不变，只提供 web_search 动作."""
        props = {"web_search": self._ACTIONS_DEFINITIONS["web_search"]}
        schema = {"type": "object", "properties": props}
        return schema, {}

    def get_level_actions_descriptions(self, level: str) -> str:
        """返回核心平台的动作描述."""
        return self._ACTIONS_DESCRIPTIONS["web_search"]
