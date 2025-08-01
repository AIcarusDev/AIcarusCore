# ruff: noqa: E501
# src/platform_builders/core_builder.py
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
            "description": "专注于指定的目标（平台或会话）。",
            "properties": {
                "target_id": {
                    "type": "string",
                    "description": "要专注的目标ID。例如平台ID 'qq' 或会话ID '123456'。",
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
            "description": "直接专注于指定的目标。",
            "properties": {
                "target_path": {
                    "type": "string",
                    "description": "要专注的绝对路径，必须是使用'.'作为分隔符的完整路径，例如`qq.123456`。",
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
        },
        "summarize_url": {
            "type": "object",
            "description": "访问一个指定的网页URL。",
            "properties": {
                "url": {"type": "string", "description": "需要访问和总结的完整网页URL。"},
                "motivation": {"type": "string"},
            },
            "required": ["url", "motivation"],
        },
        "list_files": {
            "type": "object",
            "description": "列出指定路径下的文件和文件夹。",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "要查看的路径，相对于工作区根目录。使用'/'作为分隔符。'.' 代表当前目录。",
                },
                "motivation": {"type": "string"},
            },
            "required": ["path", "motivation"],
        },
        "read_file": {
            "type": "object",
            "description": "读取指定文件的内容。",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "要读取的文件的路径，相对于工作区根目录。",
                },
                "motivation": {"type": "string"},
            },
            "required": ["path", "motivation"],
        },
        "write_file": {
            "type": "object",
            "description": "向指定文件写入内容。如果文件不存在，会自动创建。",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "要写入的文件的路径，相对于工作区根目录。",
                },
                "content": {
                    "type": "string",
                    "description": "要写入的内容。这是一个标准的JSON字符串，换行符请使用'\\n'进行转义。",
                },
                "append": {
                    "type": "boolean",
                    "description": "是否以追加模式写入。True为追加到末尾，False为覆盖整个文件。默认为True。",
                    "default": True,
                },
                "motivation": {"type": "string"},
            },
            "required": ["path", "content", "motivation"],
        },
        "edit_file": {
            "type": "object",
            "description": "在指定文件中搜索并替换内容。",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "要编辑的文件的路径，相对于工作区根目录。",
                },
                "search_pattern": {"type": "string", "description": "要查找并替换的文本内容。"},
                "replace_string": {"type": "string", "description": "用来替换的新文本内容。"},
                "motivation": {"type": "string"},
            },
            "required": ["path", "search_pattern", "replace_string", "motivation"],
        },
        "get_aggregated_content": {
            "type": "object",
            "description": "扫描工作区内指定路径，将所有符合条件的文件内容聚合后，直接作为字符串返回。",
            "properties": {
                "source_path": {
                    "type": "string",
                    "description": "要扫描的源路径，相对于工作区根目录。例如 '.' 代表整个工作区。",
                },
                "extensions": {
                    "type": "array",
                    "description": "（可选）一个只包含指定文件扩展名的列表。如果省略，将使用默认配置。",
                    "items": {"type": "string"},
                },
                "ignore_items": {
                    "type": "array",
                    "description": "（可选）一个要忽略的文件或文件夹名称的列表。如果省略，将使用默认配置。",
                    "items": {"type": "string"},
                },
                "motivation": {"type": "string"},
            },
            "required": ["source_path", "motivation"],
        },
        "delete_workspace_file": {
            "type": "object",
            "description": "【危险操作】删除工作区内的指定文件。请谨慎使用！",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "要删除的文件的路径，【必须】相对于工作区根目录。",
                },
                "motivation": {"type": "string"},
            },
            "required": ["path", "motivation"],
        },
    }

    # --- 同样，更新自然语言描述，让LLM更容易理解 ---
    _ACTIONS_DESCRIPTIONS: ClassVar = {
        "web_search": "    - `web_search`: 进行一次互联网搜索，以获取外部信息。",
        "summarize_url": "    - `summarize_url`: 访问一个指定的网页URL，获取其中信息，需要提供网址（url）。",
        "list_files": "    - `list_files`: 列出工作区内指定路径的文件和目录。",
        "read_file": "    - `read_file`: 读取工作区内指定文件的内容。",
        "write_file": "    - `write_file`: 向工作区内的文件写入内容(可追加或覆盖)。",
        "edit_file": "    - `edit_file`: 替换文件内的指定文本。",
        "get_aggregated_content": "    - `get_aggregated_content`: 扫描并聚合工作区内的文件内容，直接返回一个包含所有内容的字符串。",
        "delete_workspace_file": "    - `delete_workspace_file`: 【危险】删除工作区内的指定文件。",
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

    def get_level_actions_descriptions(self, level: str) -> str:
        """返回核心平台的动作描述."""
        all_descs = "\n".join(self._ACTIONS_DESCRIPTIONS.values())
        return all_descs

    def get_level_actions_definitions(self, level: str) -> tuple[dict[str, Any], dict[str, Any]]:
        """核心平台的动作定义，现在包含所有工具."""
        props = self._ACTIONS_DEFINITIONS
        schema = {"type": "object", "properties": props}
        return schema, {}
