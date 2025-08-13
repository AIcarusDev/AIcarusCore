import time
import uuid
from typing import Any

from aicarus_protocols import Event, Seg
from src.common.custom_logging.logging_config import get_logger
from src.platform_builders.base_builder import BasePlatformBuilder

logger = get_logger(__name__)

class LinuxMCPBuilder(BasePlatformBuilder):
    """Linux-MCP 平台的构建器，是其在 Core 中的“外交大使”."""

    @property
    def is_tool_platform(self) -> bool:
        """这是一个工具平台，其能力应该在顶层(core)就可用."""
        return True

    @property
    def platform_id(self) -> str:
        """返回平台的唯一标识符."""
        return "linux_mcp"

    def build_action_event(
            self,
            action_name: str,
            params: dict[str, Any],
            bot_id: str
        ) -> Event | None:
        """将 Core 的指令转换为发往 Agent 的标准 Event."""
        final_event_type = f"action.{self.platform_id}.{action_name}"
        action_seg = Seg(type="action_params", data=params)

        return Event(
            event_id=str(uuid.uuid4()),
            event_type=final_event_type,
            time=int(time.time() * 1000),
            bot_id=bot_id, # 在这里通常就是 platform_id
            content=[action_seg],
        )

    def get_level_consciousness_controls_definitions(self, level: str) -> tuple[dict, dict]:
        """不提供专属的意识控制."""
        return {"type": "object", "properties": {}}, {}

    def get_level_consciousness_controls_descriptions(self, level: str) -> str:
        """不提供专属的意识控制描述."""
        return ""

    def get_level_actions_definitions(self, level: str) -> tuple[dict, dict]:
        """向 Core 的大脑注册所有原子操作的 Schema."""
        props = {
            "state_tool": {
                "type": "object",
                "description": "核心感知工具。获取当前沙盒桌面的完整状态，包括UI元素和屏幕截图。",
                "properties": {
                    "motivation": {"type": "string"}
                },
                "required": ["motivation"]
            },
            "click_tool": {
                "type": "object",
                "description": "在指定元素的坐标上执行鼠标左键单击。",
                "properties": {
                    "loc": {
                        "type": "array",
                        "description": "要点击的坐标，从 state_tool 返回的 `coords` 字段获取。",
                        "items": [{"type": "integer"}, {"type": "integer"}],
                        "minItems": 2,
                        "maxItems": 2
                    },
                    "motivation": {"type": "string"}
                },
                "required": ["loc", "motivation"],
            },
            "type_tool": {
                "type": "object",
                "description": "先点击指定元素的坐标以聚焦，然后输入文本。",
                "properties": {
                    "loc": {
                        "type": "array",
                        "description": "要点击的坐标，从 state_tool 返回的 `coords` 字段获取。",
                        "items": [{"type": "integer"}, {"type": "integer"}],
                        "minItems": 2,
                        "maxItems": 2
                    },
                    "text": {"type": "string", "description": "要输入的文本。"},
                    "motivation": {"type": "string"}
                },
                "required": ["loc", "text", "motivation"],
            },
            "key_tool": {
                "type": "object",
                "description": "按下指定的单个按键（例如 'enter', 'tab'）。",
                "properties": {
                    "key": {"type": "string", "description": "要按下的按键名称。"},
                    "motivation": {"type": "string"}
                },
                "required": ["key", "motivation"],
            },
        }
        schema = {"type": "object", "properties": props}
        return schema, {}

    def get_level_actions_descriptions(self, level: str) -> str:
        """用自然语言向 LLM 描述这些原子操作."""
        descs = [
            "    - `state_tool(motivation)`: '观察'沙盒环境，获取屏幕上所有可交互元素的列表和截图。",  # noqa: E501
            "    - `click_tool(loc, motivation)`: '点击'指定坐标。",
            "    - `type_tool(loc, text, motivation)`: '输入'文本到指定坐标。",
            "    - `key_tool(key, motivation)`: '按下'一个键盘按键。",
        ]
        return "\n".join(descs)
