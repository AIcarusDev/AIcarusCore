# 文件路径: src/prompting/schema_builder.py

from typing import TYPE_CHECKING, Any

from src.common.custom_logging.logging_config import get_logger
from src.os.models import WindowStatus

if TYPE_CHECKING:
    from src.mind.abilities.deliberation_service import DeliberationService
    from src.mind.abilities.information_retrieval_service import InformationRetrievalService
    from src.mind.goal_manager import GoalManager
    from src.os.services.filesystem_service import FileSystemService
    from src.os.window_manager import WindowManager

logger = get_logger(__name__)


class SchemaBuilder:
    """[AIC-OS 精炼版] 动态的、上下文感知的 Schema 构建器."""

    def __init__(
        self,
        window_manager: "WindowManager",
        filesystem_service: "FileSystemService",
        info_retrieval_service: "InformationRetrievalService",
        goal_manager: "GoalManager",
        deliberation_service: "DeliberationService",
    ) -> None:
        self.window_manager = window_manager
        self.filesystem_service = filesystem_service
        self.info_retrieval_service = info_retrieval_service
        self.goal_manager = goal_manager
        self.deliberation_service = deliberation_service
        logger.info("SchemaBuilder (AIC-OS 精炼版) 已初始化。")

    def build_response_schema(self, ui_mapping: dict[str, Any]) -> dict[str, Any]:
        """构建 LLM 响应的最终 JSON Schema."""
        # 1. 构建 UI 交互动作
        ui_action_properties = self._build_ui_interaction_schema(ui_mapping)

        # 2. 从服务获取核心能力动作
        core_action_properties = {}
        core_action_properties.update(self.filesystem_service.get_actions_schema())
        core_action_properties.update(self.info_retrieval_service.get_actions_schema())

        # 3. 合并所有外部动作
        external_action_properties = {**ui_action_properties, **core_action_properties}

        # 4. 构建内部动作
        internal_action_properties = self._build_internal_action_schema()

        # 5. 组装最终 schema
        final_schema = {
            "type": "object",
            "properties": {
                "internal_state": {
                    "type": "object",
                    "description": "你的内心状态，这是你思考的核心。",
                    "properties": {
                        "mood": {"type": "string", "description": "你当前的情绪状态和原因。"},
                        "think": {
                            "type": "string",
                            "description": "你对当前所有情况的详细思考过程。",
                        },
                        "intent": {
                            "type": "string",
                            "description": "你当前最直接的、短期的意图或打算。",
                        },
                    },
                    "required": ["mood", "think", "intent"],
                }
            },
            "required": ["internal_state"],
        }

        if internal_action_properties:
            final_schema["properties"]["internal_action"] = {
                "type": "object",
                "description": "影响你内部状态或长期计划的动作。每轮只能选择执行一个。",
                "properties": internal_action_properties,
                "maxProperties": 1,
            }

        if external_action_properties:
            final_schema["properties"]["external_action"] = {
                "type": "object",
                "description": "与 AIC-OS 虚拟界面交互或使用核心能力的动作。每轮只能选择执行一个。",
                "properties": external_action_properties,
                "maxProperties": 1,
            }

        return final_schema

    def _build_internal_action_schema(self) -> dict:
        """构建内部动作 (internal_action) 的 schema."""
        internal_actions = {}
        internal_actions.update(self.goal_manager.get_actions_schema())
        internal_actions.update(self.deliberation_service.get_actions_schema())
        return internal_actions

    def _build_ui_interaction_schema(self, ui_mapping: dict[str, dict]) -> dict:
        """动态构建纯 UI 交互动作 (click, send_message 等) 的 schema."""
        properties = {}
        clickable_ids = [
            uid for uid, mapping in ui_mapping.items() if mapping.get("action_type") == "click"
        ]
        if clickable_ids:
            properties["click"] = {
                "description": "单击一个可见的UI元素，如按钮、菜单项等。",
                "type": "object",
                "properties": {
                    "target_id": {
                        "type": "string",
                        "description": "必须是当前屏幕上可见的可单击元素的ID。",
                        "enum": sorted(clickable_ids),
                    },
                    "motivation": {"type": "string"},
                },
                "required": ["target_id", "motivation"],
            }

        double_clickable_ids = [
            uid
            for uid, mapping in ui_mapping.items()
            if mapping.get("action_type") == "double_click"
        ]
        if double_clickable_ids:
            properties["double_click"] = {
                "description": "双击一个UI元素，通常用于打开应用或文件。",
                "type": "object",
                "properties": {
                    "target_id": {
                        "type": "string",
                        "description": "必须是当前屏幕上可见的可双击元素的ID。",
                        "enum": sorted(double_clickable_ids),
                    },
                    "motivation": {"type": "string"},
                },
                "required": ["target_id", "motivation"],
            }

        visible_chat_windows = [
            w
            for w in self.window_manager.get_all_windows_sorted()
            if w.status in [WindowStatus.NORMAL, WindowStatus.MAXIMIZE]
            and w.window_class == "conversation"
        ]
        if visible_chat_windows:
            properties["send_message"] = {
                "description": "在指定的、当前可见的聊天窗口中发送消息。",
                "type": "object",
                "properties": {
                    "target_window_id": {
                        "type": "string",
                        "description": "必须是当前屏幕上可见的聊天窗口的ID。",
                        "enum": sorted([w.id for w in visible_chat_windows]),
                    },
                    "steps": {
                        "type": "array",
                        "description": "构建消息的指令序列。",
                        "items": {
                            "type": "object",
                            "properties": {
                                "command": {
                                    "type": "string",
                                    "enum": ["reply", "at", "text", "sticker", "send_and_break"],
                                },
                                "params": {"type": "object"},
                            },
                            "required": ["command", "params"],
                        },
                    },
                    "motivation": {"type": "string"},
                },
                "required": ["target_window_id", "steps", "motivation"],
            }
        return properties
