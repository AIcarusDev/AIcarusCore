# src/platform_builders/core_builder.py (修改后的完整文件)
from typing import Any, ClassVar

from aicarus_protocols import Event
from src.common.custom_logging.logging_config import get_logger
from src.platform_builders.base_builder import BasePlatformBuilder

logger = get_logger(__name__)


class CoreBuilder(BasePlatformBuilder):
    """核心系统内部动作和意识控制的构建器 (v2.0 堆栈版)。"""

    # --- v2.0 全新指令集的 JSON Schema 定义 ---
    _CONSCIOUSNESS_CONTROLS_DEFINITIONS: ClassVar = {
        "push_focus": {
            "type": "object",
            "properties": {
                "target_path": {"type": "string", "description": "要聚焦的目标路径 (绝对或相对路径)。"},
                "motivation": {"type": "string"},
            },
            "required": ["target_path", "motivation"],
        },
        "pop_focus": {
            "type": "object",
            "properties": {"motivation": {"type": "string", "description": "你为什么要返回上一层？"}},
            "required": ["motivation"],
        },
        "swap_focus": {
            "type": "object",
            "properties": {
                "target_path": {"type": "string", "description": "要切换到的同层级目标路径。"},
                "motivation": {"type": "string"},
            },
            "required": ["target_path", "motivation"],
        },
        "teleport_focus": {
            "type": "object",
            "properties": {
                "target_path": {"type": "string", "description": "要直接进入到某个绝对路径。"},
                "motivation": {"type": "string"},
            },
            "required": ["target_path", "motivation"],
        },
        "back": {
            "type": "object",
            "properties": {"motivation": {"type": "string", "description": "你为什么要回溯到上一个焦点？"}},
            "required": ["motivation"],
        },
        "jump_to_history": {
            "type": "object",
            "properties": {
                "history_index": {"type": "integer", "description": "导航日志中的时间索引 (例如 T-2 的索引是 -2)。"},
                "motivation": {"type": "string"},
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
        return "core"

    def build_action_event(self, action_name: str, params: dict[str, Any], bot_id: str) -> Event | None:
        logger.warning(
            f"CoreBuilder 的 build_action_event 被意外调用！Action: {action_name}。"
        )
        return None

    def get_level_consciousness_controls_definitions(self, level: str) -> tuple[dict[str, Any], dict[str, Any]]:
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
        descs = []
        # --- v2.0 全新指令集的自然语言描述 ---
        descs.append("    - `push_focus(target_path, motivation)`: **下潜/聚焦**。进入到下一层焦点。")
        descs.append("    - `pop_focus(motivation)`: **上浮/返回**。从当前层级退栈，返回上一层。")
        descs.append("    - `swap_focus(target_path, motivation)`: **交换/切换**。切换到同层级的另一个目标。")
        descs.append("    - `teleport_focus(target_path, motivation)`: **传送/跃迁**。无视当前位置，直接跳转到任意绝对路径。")
        descs.append("    - `back(motivation)`: **回溯**。根据<navigation_log>，返回到历史记录中的【上一个】焦点。")
        descs.append("    - `jump_to_history(history_index, motivation)`: **跳跃**。根据<navigation_log>，直接跳转到历史记录中的【指定】焦点。")

        return "\n".join(descs)

    def get_level_actions_definitions(self, level: str) -> tuple[dict[str, Any], dict[str, Any]]:
        props = {"web_search": self._ACTIONS_DEFINITIONS["web_search"]}
        schema = {"type": "object", "properties": props}
        return schema, {}

    def get_level_actions_descriptions(self, level: str) -> str:
        return self._ACTIONS_DESCRIPTIONS["web_search"]
