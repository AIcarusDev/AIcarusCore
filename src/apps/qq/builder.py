# 文件路径: src/apps/qq/builder.py

import time
import uuid
from xml.etree.ElementTree import Element

from aicarus_protocols import Event, Seg
from src.os.models import Window
from src.services.action.components.base_builder import BasePlatformBuilder  # [修改] 修正导入路径
from src.services.database.services.entity_graph_service import EntityGraphService
from src.services.database.services.event_storage_service import EventStorageService

# [新增] 导入QQ专属的渲染器和依赖
from .qq_renderer import QQWindowRenderer


class QQBuilder(BasePlatformBuilder):
    """QQ 平台的构建器，负责向 Core 注册 QQAdapter 的能力."""

    @property
    def platform_id(self) -> str:
        """返回平台ID."""
        return "qq"

    # [新增] 实现渲染器接口
    async def render_window_content(
        self,
        parent_element: Element,
        current_path: list[str],
        window: Window,
        bot_ids_map: dict,
        entity_service: EntityGraphService,
        event_service: EventStorageService,
        ui_mapping: dict,
        generate_semantic_id: callable,
    ) -> None:
        """实现基类的渲染接口，委托给QQWindowRenderer处理."""
        renderer = QQWindowRenderer(entity_service, event_service, ui_mapping, generate_semantic_id)
        await renderer.render_content(parent_element, current_path, window, bot_ids_map)

    def get_action_definitions(self) -> dict:
        """定义 QQ 平台的所有动作."""
        # 这个方法现在只定义了 send_message
        # 其他如 get_list 等，会由 AIC-OS 的 UI 交互自动生成
        return {
            "send_message": {
                "type": "object",
                "description": "在指定的、当前可见的聊天窗口中发送消息。",
                "properties": {
                    "target_window_id": {
                        "type": "string",
                        "description": "必须是当前屏幕上可见的聊天窗口的ID。",
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
        }

    def build_action_event(self, action_name: str, params: dict, bot_id: str) -> Event | None:
        """将 Core 的指令转换成发往 Adapter 的标准 Event."""
        if action_name == "send_message":
            # send_message 的逻辑现在由 DecisionDispatcher 直接处理，这里可以留空或返回一个通用结构
            # 为保持一致性，我们仍然构建一个事件
            final_event_type = f"action.{self.platform_id}.{action_name}"
            action_seg = Seg(type="action_params", data=params)
            return Event(
                event_id=str(uuid.uuid4()),
                event_type=final_event_type,
                time=int(time.time() * 1000),
                bot_id=bot_id,
                content=[action_seg],
            )
        return None
