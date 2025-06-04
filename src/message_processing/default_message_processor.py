# src/message_processing/default_message_processor.py
from typing import TYPE_CHECKING

# v1.4.0 协议导入 - 替换旧的导入
from aicarus_protocols import Event
from websockets.server import WebSocketServerProtocol

from src.common.custom_logging.logger_manager import get_logger
from src.database.arangodb_handler import ArangoDBHandler

# 使用TYPE_CHECKING避免循环导入
if TYPE_CHECKING:
    pass


class DefaultMessageProcessor:
    """默认的消息处理器，负责处理来自适配器的事件并分发到相应的处理逻辑"""

    def __init__(self, db_handler: ArangoDBHandler) -> None:
        """初始化消息处理器"""
        self.logger = get_logger(f"AIcarusCore.{self.__class__.__name__}")
        self.db_handler = db_handler
        self.logger.info("DefaultMessageProcessor 初始化完成")

    async def process_event(
        self, event: Event, websocket: WebSocketServerProtocol, needs_persistence: bool = True
    ) -> None:
        """
        处理来自适配器的事件

        参数:
            event: 要处理的事件对象
            websocket: 发送此事件的WebSocket连接
            needs_persistence: 是否需要将此事件持久化到数据库，默认为True
        """
        try:
            self.logger.info(f"处理事件: {event.event_type}, ID: {event.event_id}")

            # 只有当needs_persistence为True时才保存事件到数据库
            if needs_persistence:
                # 保存事件到数据库
                event_data = event.to_dict()
                save_success = await self.db_handler.save_event_v14(event_data)

                if not save_success:
                    self.logger.error(f"保存事件失败: {event.event_id}")
                else:
                    self.logger.debug(f"事件保存成功: {event.event_id}")
            else:
                self.logger.debug(f"事件不需要持久化: {event.event_type} (ID: {event.event_id})")
                return

            # 根据事件类型进行处理
            if event.event_type.startswith("message."):
                await self._handle_message_event(event, websocket)
            elif event.event_type.startswith("request."):
                await self._handle_request_event(event, websocket)
            else:
                self.logger.debug(f"未知事件类型: {event.event_type}")

        except Exception as e:
            self.logger.error(f"处理事件时发生错误: {e}", exc_info=True)

    async def _handle_message_event(self, event: Event, websocket: WebSocketServerProtocol) -> None:
        """处理消息事件"""
        try:
            # 提取消息文本内容
            text_content = self._extract_text_from_content(event.content)

            if text_content:
                self.logger.debug(f"消息内容: {text_content[:100]}...")

        except Exception as e:
            self.logger.error(f"处理消息事件时发生错误: {e}", exc_info=True)

    async def _handle_request_event(self, event: Event, websocket: WebSocketServerProtocol) -> None:
        """处理请求事件"""
        try:
            self.logger.info(f"收到请求事件: {event.event_type}")
            # 这里可以添加处理请求事件的逻辑

        except Exception as e:
            self.logger.error(f"处理请求事件时发生错误: {e}", exc_info=True)

    def _extract_text_from_content(self, content: list) -> str:
        """从内容中提取文本"""
        try:
            text_parts = []
            for segment in content:
                if isinstance(segment, dict):
                    if segment.get("type") == "text":
                        text_parts.append(segment.get("data", {}).get("text", ""))
                    elif segment.get("type") == "at":
                        text_parts.append(f"@{segment.get('data', {}).get('display_name', '用户')}")
                    elif segment.get("type") == "image":
                        text_parts.append("[图片]")
                    elif segment.get("type") == "voice":
                        text_parts.append("[语音]")

            return " ".join(text_parts).strip()

        except Exception as e:
            self.logger.error(f"提取文本内容时发生错误: {e}", exc_info=True)
            return ""
