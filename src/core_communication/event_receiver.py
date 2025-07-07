# src/core_communication/event_receiver.py
import json
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from aicarus_protocols import Event as ProtocolEvent
from src.common.custom_logging.logging_config import get_logger
from websockets.server import WebSocketServerProtocol

if TYPE_CHECKING:
    from src.action.action_handler import ActionHandler

logger = get_logger(__name__)

# 定义回调函数的类型别名，以便清晰地表示其期望的签名
AdapterEventCallback = Callable[[ProtocolEvent, WebSocketServerProtocol, bool], Awaitable[None]]


class EventReceiver:
    """负责处理从适配器接收到的原始消息，解析它们，并分发到相应的处理器."""

    def __init__(
        self,
        event_handler_callback: AdapterEventCallback,
        action_handler_instance: "ActionHandler",
        # adapter_clients_info 将由外部传入和管理，EventReceiver 只读取它
        adapter_clients_info: dict[str, dict[str, Any]],
    ) -> None:
        self._event_handler_callback = event_handler_callback
        self.action_handler = action_handler_instance
        self.adapter_clients_info = adapter_clients_info
        logger.info("EventReceiver 初始化完成。")

    def _needs_persistence(self, event: ProtocolEvent) -> bool:
        """判断一个事件是否需要被持久化."""
        non_persistent_types = ["meta.lifecycle.connect", "meta.lifecycle.disconnect"]
        # action_response 应该被持久化，因为它代表了一个已完成动作的事实
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
        """处理单条来自适配器的消息。

        Args:
            message_str: 接收到的原始消息字符串。
            websocket: 发送消息的WebSocket连接。
            adapter_id: 发送消息的适配器ID。
            display_name: 适配器的显示名称。
        """
        logger.debug(
            f"EventReceiver 正在处理来自 '{display_name}({adapter_id})' 的消息: {message_str[:200]}..."
        )

        try:
            message_dict = json.loads(message_str)
            msg_event_type = message_dict.get("event_type")

            # 1. 处理生命周期事件 (除了 connect，因为它在注册阶段处理)
            if msg_event_type == "meta.lifecycle.disconnect":
                logger.info(f"收到来自适配器 '{display_name}({adapter_id})' 的主动断开通知。")
                # 实际的断开逻辑（unregister, close）应该由连接管理器在更高层处理
                # 这里只记录日志，并可以触发一个关闭流程
                # await self.connection_manager.unregister_adapter(websocket, "适配器主动下线")
                return

            # 2. 处理动作响应 (Action Response)
            if msg_event_type and msg_event_type.startswith("action_response."):
                if self.action_handler:
                    await self.action_handler.handle_action_response(message_dict)
                else:
                    logger.error("收到 action_response 但 ActionHandler 未初始化！")
                return  # 动作响应处理完毕，直接返回

            # 3. 处理标准的 AIcarus 事件
            if "event_id" in message_dict and msg_event_type and "content" in message_dict:
                try:
                    aicarus_event = ProtocolEvent.from_dict(message_dict)
                    # 调用注册的回调函数（即 DefaultMessageProcessor.process_event）
                    await self._event_handler_callback(
                        aicarus_event, websocket, self._needs_persistence(aicarus_event)
                    )
                except Exception as e_parse:
                    logger.error(
                        f"解析或处理 Event 时出错: {e_parse}. 数据: {message_dict}", exc_info=True
                    )
            else:
                logger.warning(f"收到的消息结构不像标准的 AIcarus Event. 数据: {message_dict}")

        except json.JSONDecodeError:
            logger.error(
                f"从适配器 '{display_name}({adapter_id})' 解码 JSON 失败. 原始消息: {message_str[:200]}"
            )
        except Exception as e:
            logger.error(
                f"处理来自适配器 '{display_name}({adapter_id})' 的消息时发生错误: {e}",
                exc_info=True,
            )
