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
        "focus": {
            "type": "object",
            "description": "专注于指定的目标（平台或会话）。",
            "properties": {
                "target_id": {
                    "type": "string",
                    "description": "要专注的目标ID。例如平台ID 'qq' 或会话ID 'qq_group_123456'。",
                },
                "motivation": {"type": "string"},
            },
            "required": ["target_id", "motivation"],
        },
        "return": {
            "type": "object",
            "description": "从当前注意力离开。例如从当前会话返回到会话所属的平台，或退出当前平台。",
            "properties": {"motivation": {"type": "string"}},
            "required": ["motivation"],
        },
        "shift_focus": {
            "type": "object",
            "description": "将你的注意力从当前会话切换到另一个会话, 需要完整ID，例如`qq_group_123456`。",
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
                    "description": "要专注的绝对路径，必须是使用'.'作为分隔符的完整路径，例如`qq.qq_group_123456`。",
                },
                "motivation": {"type": "string"},
            },
            "required": ["target_path", "motivation"],
        },
        "back": {
            "type": "object",
            "description": "将你的注意力拉回到`<attentional_trajectory>`中的上一个注意力焦点(T-1)。",
            "properties": {"motivation": {"type": "string"}},
            "required": ["motivation"],
        },
        "jump_to_history": {
            "type": "object",
            "description": "根据`<attentional_trajectory>`，直接跳转到由`history_index`指定的历史焦点。",
            "properties": {
                "history_index": {
                    "type": "integer",
                    "description": "导航日志中的时间索引 (例如 T-2 的索引是 -2)。",
                },
                "motivation": {"type": "string"},
            },
            "required": ["history_index", "motivation"],
        },
        "deep_think": {
            "type": "object",
            "description": "进行理性的深度思考，在遇到陌生、复杂、抽象问题，或高风险的决策时使用。",
            "properties": {
                "motivation": {"type": "string"},
                "opinions": {
                    "type": "array",
                    "description": "需要讨论的不同观点或策略，数量限制在2-5个。",
                    "maxItems": 5,
                    "items": {
                        "type": "object",
                        "properties": {
                            "tag": {
                                "type": "string",
                                "description": "你对此观点或策略的简短标签。",
                            },
                            "initial_thought": {
                                "type": "string",
                                "description": "你对此观点或策略的详细初始想法。",
                            },
                        },
                        "required": ["tag", "initial_thought"],
                    },
                },
            },
            "required": ["motivation", "opinions"],
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
            props["focus"] = self._CONSCIOUSNESS_CONTROLS_DEFINITIONS["focus"]
        elif level == "platform":
            props["focus"] = self._CONSCIOUSNESS_CONTROLS_DEFINITIONS["focus"]
            props["return"] = self._CONSCIOUSNESS_CONTROLS_DEFINITIONS["return"]
        elif level == "cellular":
            props["return"] = self._CONSCIOUSNESS_CONTROLS_DEFINITIONS["return"]
            props["shift_focus"] = self._CONSCIOUSNESS_CONTROLS_DEFINITIONS["shift_focus"]

        # back 和 jump_to_history 在任何层级都可用
        props["back"] = self._CONSCIOUSNESS_CONTROLS_DEFINITIONS["back"]
        props["jump_to_history"] = self._CONSCIOUSNESS_CONTROLS_DEFINITIONS["jump_to_history"]

        # teleport_focus 也应该是全局可用的
        props["teleport_focus"] = self._CONSCIOUSNESS_CONTROLS_DEFINITIONS["teleport_focus"]

        # 慢思考作为一种基础认知能力，在所有层级都应该可用
        props["deep_think"] = self._CONSCIOUSNESS_CONTROLS_DEFINITIONS["deep_think"]

        schema = {"type": "object", "properties": props, "maxProperties": 1}
        return schema, {}

    def get_level_consciousness_controls_descriptions(self, level: str) -> str:
        """根据可用的意识控制定义，动态生成自然语言描述."""
        # 1. 首先调用现有方法，以获取当前层级下真正可用的控制项。
        #    这避免了重复编写 `if/elif` 逻辑。
        schema, _ = self.get_level_consciousness_controls_definitions(level)
        available_controls = schema.get("properties", {})

        descs = []
        # 2. 遍历所有可用的控制项
        for name, definition in available_controls.items():
            # 从 'required' 字段中提取参数列表，并拼接成字符串
            params_list = definition.get("required", [])
            params_str = ", ".join(params_list)

            # 从 'description' 字段中直接提取功能描述
            description = definition.get("description", "（无可用描述）")

            # 3. 按照统一格式生成描述字符串
            #    格式为: " - `command(param1, param2)`: description"
            desc_line = f"      - `{name}({params_str})`: {description}"
            descs.append(desc_line)

        # 对结果进行排序，可以确保每次输出的顺序都一致
        return "\n".join(sorted(descs))

    def get_level_actions_descriptions(self, level: str) -> str:
        """返回核心平台的动作描述."""
        all_descs = "\n".join(self._ACTIONS_DESCRIPTIONS.values())
        return all_descs

    def get_level_actions_definitions(self, level: str) -> tuple[dict[str, Any], dict[str, Any]]:
        """核心平台的动作定义，现在包含所有工具."""
        props = self._ACTIONS_DEFINITIONS
        schema = {"type": "object", "properties": props}
        return schema, {}
