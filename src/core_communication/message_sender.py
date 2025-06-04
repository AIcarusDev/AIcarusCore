# AIcarusCore/src/core_communication/message_sender.py
import json

# 枫：将 MessageBase 修改为 Event
from aicarus_protocols import Event  # 确保 aicarus_protocols 在 PYTHONPATH 或相对路径正确
from websockets.server import WebSocketServerProtocol

from src.common.custom_logging.logger_manager import get_logger  # 假设 logger_manager 的路径

logger = get_logger("AIcarusCore.MessageSender")


class MessageSender:
    """
    处理通过 WebSocket 发送消息的类。
    """

    def __init__(self) -> None:
        """
        初始化 MessageSender。
        """
        logger.info("MessageSender 初始化完成。")

    # 枫：将 message_to_send 的类型提示从 MessageBase 修改为 Event
    async def send_message(self, websocket: WebSocketServerProtocol, message_to_send: Event) -> bool:
        """
        通过指定的 WebSocket 连接发送 Event 对象。

        :param websocket: WebSocketServerProtocol 实例，用于发送消息。
        :param message_to_send: Event 对象，包含要发送的消息内容。
        :return: True 如果发送成功, False 如果发送失败。
        """
        if not websocket or websocket.closed:
            logger.warning("WebSocket 连接无效或已关闭，无法发送消息。")
            return False

        try:
            # 枫：Event 对象和旧的 MessageBase 对象都有 to_dict() 方法
            message_dict = message_to_send.to_dict()
            await websocket.send(json.dumps(message_dict))
            logger.debug(f"消息已成功发送至 {websocket.remote_address}: {message_dict}")
            return True
        except Exception as e:
            logger.error(f"通过 WebSocket 发送消息失败: {e}", exc_info=True)
            logger.error(f"试图发送的消息内容: {message_to_send.to_dict() if message_to_send else 'None'}")
            return False
