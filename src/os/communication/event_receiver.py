# 文件路径: src/services/core_communication/event_receiver.py

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from aicarus_protocols import Event as ProtocolEvent
from src.bootstrap.container import ServiceContainer
from src.common.custom_logging.logging_config import get_logger
from websockets.server import WebSocketServerProtocol

if TYPE_CHECKING:
    from src.services.action.action_handler import ActionHandler

logger = get_logger(__name__)

AdapterEventCallback = Callable[[ProtocolEvent, WebSocketServerProtocol, bool], Awaitable[None]]


class EventReceiver:
    """[重构版] 智能事件路由器.

    它负责接收所有原始消息，解析它们，然后并行地分发到两个独立的流水线：
    1. OS 实时反应流水线 (通过 PlatformBuilder)。
    2. Mind 感知流水线 (通过 DefaultMessageProcessor)。
    """

    def __init__(
        self,
        mind_event_callback: AdapterEventCallback,
        action_handler_instance: "ActionHandler",
        service_container: "ServiceContainer",
    ) -> None:
        self._mind_event_callback = mind_event_callback
        self.action_handler = action_handler_instance
        self.container = service_container
        self._background_tasks = set()
        logger.info("EventReceiver (智能路由器版) 初始化完成。")

    def _needs_persistence(self, event: ProtocolEvent) -> bool:
        """判断一个事件是否需要被持久化."""
        non_persistent_types = ["meta.lifecycle.connect", "meta.lifecycle.disconnect"]
        if event.event_type.startswith("action_response."):
            return True
        return event.event_type not in non_persistent_types

    async def handle_message(
        self,
        message_str: str,
        websocket: WebSocketServerProtocol,
        adapter_id: str,
        display_name: str,
    ) -> None:
        """处理单条来自适配器的消息."""
        logger.debug(
            f"EventReceiver 正在处理来自 '{display_name}({adapter_id})' "
            f"的消息: {message_str[:200]}..."
        )

        try:
            message_dict = json.loads(message_str)
            msg_event_type = message_dict.get("event_type")

            # [新增] 开发者平台事件路由
            if msg_event_type and msg_event_type.startswith("devplatform."):
                logger.debug(f"路由开发者平台事件: {msg_event_type}")
                # 假设 debugging_service 已在容器中注册
                await self.container.debugging_service.handle_dev_event(message_dict)
                return  # 结束处理，不进入常规流水线

            if msg_event_type and msg_event_type.startswith("action_response."):
                await self.action_handler.handle_action_response(message_dict)
                return

            if "event_id" in message_dict and msg_event_type:
                # --- 兼容性修复区域开始 ---
                # 1. 兼容旧的 'timestamp' 字段
                if "time" not in message_dict and "timestamp" in message_dict:
                    message_dict["time"] = message_dict["timestamp"]

                # 2. 为缺失的 'bot_id' 提供一个明确的默认值
                if "bot_id" not in message_dict:
                    platform = message_dict.get("platform", "unknown")
                    message_dict["bot_id"] = f"unknown_bot_on_{platform}"

                # 3. 为缺失的 'content' 提供一个空的列表作为默认值
                if "content" not in message_dict:
                    message_dict["content"] = []
                # --- 兼容性修复区域结束 ---

                aicarus_event = ProtocolEvent.from_dict(message_dict)
                # --- [核心修改] 并行分发 ---
                # 1. 分发给 Mind 感知流水线 (异步，不阻塞)
                task = asyncio.create_task(
                    self._mind_event_callback(
                        aicarus_event, websocket, self._needs_persistence(aicarus_event)
                    )
                )
                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)

                # 2. 分发给 OS 实时反应流水线
                builder = self.container.application_manager.get_builder_by_name(adapter_id)
                if builder and hasattr(builder, "handle_os_level_event"):
                    await builder.handle_os_level_event(aicarus_event, self.container)

            else:
                logger.warning(f"收到的消息结构不像标准的 AIcarus Event: {message_dict}")

        except json.JSONDecodeError:
            logger.error(
                f"从适配器 '{display_name}({adapter_id})' 解码 JSON 失败: {message_str[:200]}"
            )
        except Exception as e:
            logger.error(
                f"处理来自适配器 '{display_name}({adapter_id})' 的消息时发生错误: {e}",
                exc_info=True,
            )
