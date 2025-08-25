# src/prompt_builder/schema_builder.py
from typing import TYPE_CHECKING, Any

# 导入新的 AIC-OS 模型和服务
from src.aicos.models import WindowStatus
from src.common.custom_logging.logging_config import get_logger

if TYPE_CHECKING:
    # 这是 SchemaBuilder 的新核心依赖
    from src.aicos.window_manager import WindowManager

logger = get_logger(__name__)


class SchemaBuilder:
    """[AIC-OS 重构版]一个动态的、上下文感知的 Schema 构建器.

    它的核心使命不再是基于抽象的 'level' 来提供固定的动作列表，
    而是根据 AIC-OS 当前的“视觉”状态（由 ui_mapping 和 WindowManager 提供），
    为 LLM 生成一个精确的、受限的、包含动态枚举的操作指令集。

    这从根本上确保了 AI 只能“思考”去操作它当前“看得到”的 UI 元素。
    """

    def __init__(
        self,
        # 删除了旧的依赖，现在它只需要 WindowManager
        window_manager: "WindowManager",
    ) -> None:
        """初始化 SchemaBuilder.

        Args:
            window_manager: 窗口管理器实例，用于获取当前可见的窗口状态。
        """
        self.window_manager = window_manager
        logger.info("SchemaBuilder (AIC-OS 重构版) 已初始化。")

    def build_response_schema(
        self,
        ui_mapping: dict[str, dict],
    ) -> dict[str, Any]:
        """构建 LLM 响应的最终 JSON Schema.

        这是该类的主要入口点，它会编排所有子 schema 的构建。

        Args:
            ui_mapping: 由 AICOSStateGenerator 生成的、当前帧所有可交互UI元素的映射。
                格式: {
                        "temp-ui-id-123": {
                            "action_type": "click",
                            "target_uid": "persistent-id-abc"
                            },
                            ...}

        Returns:
            一个完整的、可供 LLM 使用的 JSON Schema 字典。
        """
        # 1. 构建动态的外部动作 schema
        external_action_properties = self._build_external_action_schema(ui_mapping)

        # 2. 构建静态的内部动作 schema (例如 manage_goals, deep_think)
        internal_action_properties = self._build_internal_action_schema()

        # 3. 组装最终的 schema
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

        # 只有在有可用内部动作时，才添加 internal_action 字段
        if internal_action_properties:
            final_schema["properties"]["internal_action"] = {
                "type": "object",
                "description": "影响你内部状态或长期计划的动作。每轮只能选择执行一个。",
                "properties": internal_action_properties,
                "maxProperties": 1,
            }

        # 只有在有可用外部动作时，才添加 external_action 字段
        if external_action_properties:
            final_schema["properties"]["external_action"] = {
                "type": "object",
                "description": "与 AIC-OS 虚拟界面交互的动作。每轮只能选择执行一个。",
                "properties": external_action_properties,
                "maxProperties": 1,
            }

        return final_schema

    def _build_internal_action_schema(self) -> dict:
        """构建内部动作 (internal_action) 的 schema.

        这些动作通常是静态的，与 UI 无关。
        """
        # 这里我们直接从 CoreBuilder 借用定义，因为它们是核心的、与UI无关的能力
        from src.platform_builders.core_builder import CoreBuilder

        internal_actions = {}

        # 如果需要 manage_goals
        if "manage_goals" in CoreBuilder._CONSCIOUSNESS_CONTROLS_DEFINITIONS:
            internal_actions["manage_goals"] = CoreBuilder._CONSCIOUSNESS_CONTROLS_DEFINITIONS[
                "manage_goals"
            ]

        # 如果需要 deep_think
        if "deep_think" in CoreBuilder._CONSCIOUSNESS_CONTROLS_DEFINITIONS:
            internal_actions["deep_think"] = CoreBuilder._CONSCIOUSNESS_CONTROLS_DEFINITIONS[
                "deep_think"
            ]

        return internal_actions

    def _build_external_action_schema(self, ui_mapping: dict[str, dict]) -> dict:
        """[新核心逻辑] 动态构建外部动作 (external_action) 的 schema.

        它会解析 ui_mapping，为每个动作生成带有精确枚举的 schema。
        """
        properties = {}

        # --- 动作 1: Click ---
        # 从 ui_mapping 中筛选出所有类型为 'click' 的 UI 元素 ID
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
                        # 魔法发生的地方：将所有可点击的ID作为枚举，LLM只能从中选择！
                        "enum": sorted(clickable_ids),
                    },
                    "motivation": {"type": "string"},
                },
                "required": ["target_id", "motivation"],
            }

        # --- 动作 2: Double Click ---
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
                        "enum": sorted(double_clickable_ids),  # 动态枚举
                    },
                    "motivation": {"type": "string"},
                },
                "required": ["target_id", "motivation"],
            }

        # --- 动作 3: Send Message ---
        # 从 WindowManager 获取所有当前可见（非最小化）的聊天窗口
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
                        "enum": sorted([w.id for w in visible_chat_windows]),  # 动态枚举
                    },
                    # 链式指令的 schema 保持不变，可以硬编码或从别处导入
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
