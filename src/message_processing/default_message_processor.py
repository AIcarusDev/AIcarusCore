# src/message_processing/default_message_processor.py
from typing import TYPE_CHECKING

# v1.4.0 协议导入
from aicarus_protocols import Event, Seg, EventBuilder, SegBuilder # 枫：增加了 EventBuilder 和 SegBuilder
from websockets.server import WebSocketServerProtocol

from src.common.custom_logging.logger_manager import get_logger
from src.database.arangodb_handler import ArangoDBHandler
from src.core_communication.message_sender import MessageSender # 枫：导入 MessageSender

# 使用TYPE_CHECKING避免循环导入
if TYPE_CHECKING:
    pass


class DefaultMessageProcessor:
    """默认的消息处理器，负责处理来自适配器的事件并分发到相应的处理逻辑"""

    def __init__(self, db_handler: ArangoDBHandler) -> None:
        """初始化消息处理器"""
        self.logger = get_logger(f"AIcarusCore.{self.__class__.__name__}")
        self.db_handler = db_handler
        # 枫：如果 MessageSender 需要频繁使用，可以考虑在这里初始化 self.message_sender = MessageSender()
        # 枫：但由于 MessageSender 目前看起来是无状态的，在需要时创建实例也OK
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
            self.logger.debug(f"处理事件: {event.event_type}, ID: {event.event_id}")

            if needs_persistence:
                event_data = event.to_dict()
                save_success = await self.db_handler.save_event_v14(event_data)

                if not save_success:
                    self.logger.error(f"保存事件失败: {event.event_id}")
                else:
                    self.logger.debug(f"事件保存成功: {event.event_id}")
            else:
                self.logger.debug(f"事件不需要持久化: {event.event_type} (ID: {event.event_id})")
                # 枫：如果不需要持久化，原代码是直接 return，但如果 "test测试" 消息不需要持久化也需要响应，
                # 枫：则下面的逻辑需要调整。目前按原逻辑，仅持久化事件会进入后续处理。
                # 枫：如果希望所有消息都经过处理逻辑（无论是否持久化），可以把 return 移到这个条件块之后。
                # 枫：或者，把下面的处理逻辑移出 needs_persistence 判断块。
                # 枫：为了最小改动，暂时维持原逻辑结构，即只有 needs_persistence=True 的事件才会触发下面的处理。
                return

            if event.event_type.startswith("message."):
                await self._handle_message_event(event, websocket)
            elif event.event_type.startswith("request."):
                await self._handle_request_event(event, websocket)
            elif event.event_type.startswith("action."):
                await self._handle_action_event(event, websocket)
            else:
                self.logger.debug(f"未知事件类型: {event.event_type}")

        except Exception as e:
            self.logger.error(f"处理事件时发生错误: {e}", exc_info=True)

    async def _handle_message_event(self, event: Event, websocket: WebSocketServerProtocol) -> None:
        """处理消息事件"""
        try:
            text_content = self._extract_text_from_content(event.content)

            if text_content:
                self.logger.info(f"消息内容: {text_content}...")
            
            # 枫：当接收到“test测试”消息后，发送“测试成功”的动作到adapter
            if text_content == "test测试":
                self.logger.info(f"接收到特定消息 'test测试' from {event.user_info.user_id if event.user_info else 'UnknownUser'} in {event.conversation_info.conversation_id if event.conversation_info else 'UnknownConversation'}，准备发送 '测试成功' 动作。")

                # 1. 构建动作事件的内容
                # 根据 AIcarus-Message-Protocol v1.4.0，发送消息的动作，其 content 是一个 Seg 列表
                action_segs = [SegBuilder.text("测试成功")]

                # 2. 使用 EventBuilder 创建动作事件 (action event)
                # action_type="message.send" 会让 EventBuilder 将 event_type 设置为 "action.message.send"
                action_event_to_send = EventBuilder.create_action_event(
                    action_type="message.send", 
                    platform=event.platform,          # 从接收到的事件中获取平台信息
                    bot_id=event.bot_id,              # 从接收到的事件中获取机器人ID
                    content=action_segs,              # 要发送的内容
                    conversation_info=event.conversation_info # 将动作发送到原始消息所在的会话
                    # user_info 通常在代表机器人自身执行通用操作时不需要特别指定，除非动作本身是针对特定用户的
                )
                self.logger.debug(f"构建的 '测试成功' 动作事件: {action_event_to_send.to_dict()}")

                # 3. 实例化 MessageSender 并发送
                # MessageSender 在初始化时会打印日志 "MessageSender 初始化完成。"
                sender = MessageSender() 
                try:
                    # 枫：这里的 action_event_to_send 是 v1.4.0 的 Event 类型。
                    # 枫：MessageSender 的 send_message 期望 MessageBase 类型，但它内部只调用了 to_dict()，
                    # 枫：Event 类型也有 to_dict()，所以这里应该能工作。
                    send_op_success = await sender.send_message(websocket, action_event_to_send)
                    if send_op_success:
                        self.logger.info(f"成功通过 MessageSender 发送 '测试成功' 动作。原始事件 ID: {event.event_id}")
                    else:
                        # 枫：send_message 返回 False 时，MessageSender 内部已经记录了 warning 或 error
                        self.logger.warning(f"MessageSender 发送 '测试成功' 动作返回 False。原始事件 ID: {event.event_id}")
                except Exception as e_send:
                    self.logger.error(f"调用 MessageSender 发送 '测试成功' 动作时发生异常: {e_send}", exc_info=True)
                
                # 枫：原有的 pass 已被替换为实际逻辑，所以不需要了。

        except Exception as e:
            self.logger.error(f"处理消息事件时发生错误: {e}", exc_info=True)

    async def _handle_action_event(self, event: Event, websocket: WebSocketServerProtocol) -> None:
        """处理动作事件"""
        try:
            text_content = self._extract_text_from_content(event.content)

            if text_content:
                self.logger.info(f"动作内容: {text_content}...")

        except Exception as e:
            self.logger.error(f"处理动作事件时发生错误: {e}", exc_info=True)

    async def _handle_request_event(self, event: Event, websocket: WebSocketServerProtocol) -> None:
        """处理请求事件"""
        try:
            self.logger.info(f"收到请求事件: {event.event_type}")
            # 这里可以添加处理请求事件的逻辑

        except Exception as e:
            self.logger.error(f"处理请求事件时发生错误: {e}", exc_info=True)

    def _extract_text_from_content(self, content: list) -> str: # 枫：参数 content 应该是 List[Seg] 类型
        """从内容中提取文本"""
        try:
            text_parts = []
            for segment in content: # 枫：这里的 segment 应该是 Seg 对象
                if isinstance(segment, Seg):
                    if segment.type == "text":
                        text_parts.append(segment.data.get("text", ""))
                    elif segment.type == "at":
                        # 枫：更安全地访问 nested data
                        at_display = segment.data.get('display_name', segment.data.get('user_id', 'Unknown'))
                        text_parts.append(f"@{at_display}")
                    elif segment.type == "image":
                        text_parts.append("[图片]")
                    elif segment.type == "voice":
                        text_parts.append("[语音]")
                    # 枫：可以考虑添加对其他 Seg类型的文本化处理，如果需要的话
            
            return " ".join(text_parts).strip()

        except Exception as e:
            self.logger.error(f"提取文本内容时发生错误: {e}", exc_info=True)
            return ""
