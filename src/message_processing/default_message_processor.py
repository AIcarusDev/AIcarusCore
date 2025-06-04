# src/message_processing/default_message_processor.py
import time
import uuid
from typing import TYPE_CHECKING, Optional, List, Dict, Any # 确保 List 被导入

# v1.4.0 协议导入
from aicarus_protocols import Event, Seg, SegBuilder, UserInfo, ConversationInfo, ConversationType
from websockets.server import WebSocketServerProtocol

from src.common.custom_logging.logger_manager import get_logger
from src.database.arangodb_handler import ArangoDBHandler

# 使用TYPE_CHECKING避免循环导入
if TYPE_CHECKING:
    from src.core_communication.core_ws_server import CoreWebsocketServer


class DefaultMessageProcessor:
    """默认的消息处理器，负责处理来自适配器的事件并分发到相应的处理逻辑"""

    def __init__(
        self,
        db_handler: ArangoDBHandler,
        core_websocket_server: Optional['CoreWebsocketServer'] = None
    ) -> None:
        """初始化消息处理器"""
        self.logger = get_logger(f"AIcarusCore.{self.__class__.__name__}")
        self.db_handler = db_handler
        self.core_comm_layer = core_websocket_server
        self.logger.info("DefaultMessageProcessor 初始化完成")
        if self.core_comm_layer:
            self.logger.info("DefaultMessageProcessor 已获得 CoreWebsocketServer 实例的引用。")
        else:
            self.logger.warning("DefaultMessageProcessor 未获得 CoreWebsocketServer 实例的引用，将无法主动发送动作。")


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
        self.logger.debug(f"Entering process_event. Type of event: {type(event)}")
        try:
            self.logger.debug(f"处理事件: {event.event_type}, ID: {event.event_id}")
            if hasattr(event, 'user_info'):
                self.logger.debug(f"Type of event.user_info: {type(event.user_info)}")
            if hasattr(event, 'conversation_info'):
                self.logger.debug(f"Type of event.conversation_info: {type(event.conversation_info)}")

            if needs_persistence:
                event_data_dict = event.to_dict() # 获取事件的字典表示
                save_success = await self.db_handler.save_event_v14(event_data_dict) # 保存字典

                if not save_success:
                    self.logger.error(f"保存事件失败: {event.event_id}")
                else:
                    self.logger.debug(f"事件保存成功: {event.event_id}")
            else:
                self.logger.debug(f"事件不需要持久化: {event.event_type} (ID: {event.event_id})")

            if event.event_type.startswith("message."):
                should_continue_processing = await self._handle_message_event(event, websocket)
                if not should_continue_processing:
                    return
            elif event.event_type.startswith("request."):
                await self._handle_request_event(event, websocket)
            elif event.event_type.startswith("action_response."):
                await self._handle_action_response_event(event, websocket)
            else:
                self.logger.debug(f"未针对此事件类型 '{event.event_type}' 设置特定的处理程序，但事件已记录（如果需要）。")

        except Exception as e:
            self.logger.error(f"处理事件时发生错误: {e}", exc_info=True)

    def _extract_text_from_event_content(self, seg_object_list: List[Seg]) -> str:
        """
        从 Event.content (Seg对象列表) 中提取所有文本内容。
        """
        try:
            text_parts = []
            for seg_obj in seg_object_list: # seg_obj 已经是 Seg 对象
                if seg_obj.type == "text":
                    text_parts.append(seg_obj.data.get("text", ""))
                # 可选：处理其他类型的 Seg 以提取文本表示，例如 @某人
                # elif seg_obj.type == "at":
                #     display_name = seg_obj.data.get('display_name', '')
                #     user_id_for_at = seg_obj.data.get('user_id', '')
                #     text_parts.append(display_name or f"@{user_id_for_at}")
            return "".join(text_parts).strip() # 使用 join 更适合拼接文本段
        except Exception as e:
            self.logger.error(f"从事件内容提取文本时发生错误: {e}", exc_info=True)
            return ""

    async def _handle_message_event(self, event: Event, websocket: WebSocketServerProtocol) -> bool:
        """
        处理消息事件。
        如果消息被特殊处理（如硬编码命令），则返回 False，表示不应再进行后续通用处理。
        否则返回 True。
        """
        try:
            # event.content 已经是 List[Seg] 类型
            text_content = self._extract_text_from_event_content(event.content)
            sender_nickname_log = "UnknownUser"
            if event.user_info and event.user_info.user_nickname:
                sender_nickname_log = event.user_info.user_nickname
            elif event.user_info and event.user_info.user_id:
                sender_nickname_log = event.user_info.user_id
            
            self.logger.info(f"收到消息事件 ({event.event_type}) from {sender_nickname_log}: '{text_content[:50]}...'")

            if text_content == "完整测试":
                self.logger.info(f"检测到硬编码命令 '完整测试' from event ID: {event.event_id}")

                if not self.core_comm_layer:
                    self.logger.error("CoreWebsocketServer 实例未设置，无法为 '完整测试' 发送动作。")
                    return True

                original_sender_info = event.user_info
                original_conversation_info = event.conversation_info
                self.logger.debug(f"Type of original_conversation_info in _handle_message_event: {type(original_conversation_info)}") # 新增日志
                original_message_id = event.get_message_id() # 使用 Event 对象的辅助方法

                if not original_sender_info or not original_sender_info.user_id:
                    self.logger.error("无法获取原始发送者信息，无法执行 '完整测试' 的回复动作。")
                    return True

                # 1. 构造并发送引用回复@消息
                reply_content_segments: List[Seg] = []
                if original_message_id:
                    reply_content_segments.append(SegBuilder.reply(message_id=original_message_id))
                
                display_name_for_at = original_sender_info.user_nickname or original_sender_info.user_cardname or ""
                reply_content_segments.append(SegBuilder.at(user_id=original_sender_info.user_id, display_name=display_name_for_at))
                reply_content_segments.append(SegBuilder.text(" 测试成功"))

                reply_action_event = Event(
                    event_id=f"action_reply_{uuid.uuid4()}",
                    event_type="action.message.send",
                    time=time.time() * 1000.0,
                    platform=event.platform,
                    bot_id=event.bot_id,
                    user_info=None, 
                    conversation_info=original_conversation_info,
                    content=reply_content_segments,
                    raw_data=None
                )
                
                self.logger.debug(f"为 '完整测试' 准备的回复动作事件: {reply_action_event.to_dict()}")
                send_reply_success = await self.core_comm_layer.send_action_to_specific_adapter(websocket, reply_action_event)
                if send_reply_success:
                    self.logger.info("为 '完整测试' 的回复动作已成功发送给适配器。")
                else:
                    self.logger.error("为 '完整测试' 的回复动作发送失败。")

                # 2. 构造并发送戳一戳动作
                poke_target_user_id = original_sender_info.user_id
                poke_target_group_id: Optional[str] = None
                if original_conversation_info and original_conversation_info.type == ConversationType.GROUP:
                    poke_target_group_id = original_conversation_info.conversation_id
                
                poke_action_data: Dict[str, Any] = {"target_user_id": poke_target_user_id}
                if poke_target_group_id:
                    poke_action_data["target_group_id"] = poke_target_group_id
                
                poke_action_content_seg = Seg(
                    type="action.user.poke", 
                    data=poke_action_data
                )

                poke_action_event = Event(
                    event_id=f"action_poke_{uuid.uuid4()}",
                    event_type="action.user.poke", 
                    time=time.time() * 1000.0,
                    platform=event.platform,
                    bot_id=event.bot_id,
                    user_info=None, 
                    conversation_info=original_conversation_info, 
                    content=[poke_action_content_seg], # <<--- 直接传递 List[Seg] (即使只有一个元素)
                    raw_data=None
                )

                self.logger.debug(f"为 '完整测试' 准备的戳一戳动作事件: {poke_action_event.to_dict()}")
                send_poke_success = await self.core_comm_layer.send_action_to_specific_adapter(websocket, poke_action_event)
                if send_poke_success:
                    self.logger.info("为 '完整测试' 的戳一戳动作已成功发送给适配器。")
                else:
                    self.logger.error("为 '完整测试' 的戳一戳动作发送失败。")

                return False 

            return True

        except Exception as e:
            self.logger.error(f"处理消息事件时发生错误: {e}", exc_info=True)
            return True

    async def _handle_request_event(self, event: Event, websocket: WebSocketServerProtocol) -> None:
        """处理请求事件"""
        try:
            sender_id_log = "UnknownUser"
            if event.user_info and event.user_info.user_id:
                sender_id_log = event.user_info.user_id
            self.logger.info(f"收到请求事件: {event.event_type} from user {sender_id_log}")
            
            if event.event_type == "request.friend.add":
                self.logger.info(f"检测到好友请求事件 from {sender_id_log}. 准备自动同意。")
                if not self.core_comm_layer:
                    self.logger.error("CoreWebsocketServer 实例未设置，无法自动同意好友请求。")
                    return

                # 第一个（也应该是唯一一个）Seg 包含了请求参数
                if not event.content or not isinstance(event.content[0], dict):
                    self.logger.error("好友请求事件内容格式不正确，无法获取 request_flag。")
                    return
                
                request_params_seg = Seg.from_dict(event.content[0]) 
                request_flag = request_params_seg.data.get("request_flag")

                if not request_flag:
                    self.logger.error("好友请求事件中缺少 request_flag，无法自动同意。")
                    return

                # 构造同意好友请求的动作内容 Seg
                # Seg.type 应指明具体动作，例如 "action.request.friend.approve"
                # Seg.data 包含动作参数
                approve_action_content_seg_data: Dict[str, Any] = {
                    "request_flag": request_flag,
                    "remark": "AIcarus自动好友~" # 可选的备注
                }
                approve_action_content_seg = Seg(
                    type="action.request.friend.approve", 
                    data=approve_action_content_seg_data
                )
                
                # 构造顶层 Event
                approve_action_event = Event(
                    event_id=f"action_approve_friend_{uuid.uuid4()}",
                    event_type="action.request.friend.approve", # Event 的顶层类型也指明是同意好友请求
                    time=time.time() * 1000.0,
                    platform=event.platform, # 动作应发往原始平台
                    bot_id=event.bot_id,     # 使用原始机器人的ID
                    user_info=event.user_info, # 可以将被请求的用户信息（即请求者）传递回去
                    conversation_info=None,    # 好友请求通常没有特定的会话上下文用于此动作
                    content=[approve_action_content_seg.to_dict()] # 将Seg对象转为字典
                )
                self.logger.debug(f"准备自动同意好友请求的动作事件: {approve_action_event.to_dict()}")
                send_success = await self.core_comm_layer.send_action_to_specific_adapter(websocket, approve_action_event)
                if send_success:
                    self.logger.info(f"自动同意好友请求 {sender_id_log} 的动作已发送。")
                else:
                    self.logger.error(f"自动同意好友请求 {sender_id_log} 的动作发送失败。")

        except Exception as e:
            self.logger.error(f"处理请求事件时发生错误: {e}", exc_info=True)

    async def _handle_action_response_event(self, event: Event, websocket: WebSocketServerProtocol) -> None:
        """处理来自适配器的动作响应事件"""
        try:
            self.logger.info(f"收到动作响应事件: {event.event_type}, ID: {event.event_id}")
            if event.content and isinstance(event.content[0], dict):
                action_response_seg = Seg.from_dict(event.content[0])
                response_data = action_response_seg.data
                original_event_id = response_data.get("original_event_id")
                status_code = response_data.get("status_code")
                message = response_data.get("message")
                data_payload = response_data.get("data") # 成功时可能返回的数据

                self.logger.debug(
                    f"Adapter action response for original_event_id '{original_event_id}': "
                    f"status_code={status_code}, message='{message}', data='{data_payload}'"
                )
                
                # TODO: 在这里可以将动作执行结果更新到数据库中的 ActionLog 或相关 Thought 文档
                # 例如，如果 ActionHandler 创建了一个 ActionLog 条目，可以通过 original_event_id 找到它并更新状态。
                # if self.db_handler and original_event_id:
                #     await self.db_handler.update_action_log_status(
                #         action_id=original_event_id, # 假设 original_event_id 是 action_id
                #         status="success" if status_code == 200 else "failure", # 简化状态
                #         response_data=data_payload,
                #         error_message=message if status_code != 200 else None
                #     )

        except Exception as e:
            self.logger.error(f"处理动作响应事件时发生错误: {e}", exc_info=True)
