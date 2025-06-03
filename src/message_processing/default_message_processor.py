# src/message_processing/default_message_processor.py
import asyncio
import json
from typing import Any, Dict, Optional, TYPE_CHECKING
from websockets.server import WebSocketServerProtocol

# v1.4.0 协议导入 - 替换旧的导入
from aicarus_protocols import Event, UserInfo, ConversationInfo, Seg, SegBuilder

from src.common.custom_logging.logger_manager import get_logger
from src.config.config_manager import get_typed_settings
from src.core_communication.core_ws_server import CoreWebsocketServer
from src.database.arangodb_handler import ArangoDBHandler
from src.config.global_config import global_config

# 使用TYPE_CHECKING避免循环导入
if TYPE_CHECKING:
    from src.action.action_handler import ActionHandler
    from src.core_logic.main import CoreLogic


class DefaultMessageProcessor:
    """默认的消息处理器，负责处理来自适配器的事件并分发到相应的处理逻辑"""

    def __init__(self, db_handler: Optional[ArangoDBHandler] = None):
        self.logger = get_logger(f"AIcarusCore.{self.__class__.__name__}")
        self.db_handler: Optional[ArangoDBHandler] = db_handler
        self.core_logic: Optional['CoreLogic'] = None
        self.action_handler: Optional['ActionHandler'] = None
        
    def set_dependencies(self, db_handler: ArangoDBHandler, core_logic: 'CoreLogic', action_handler: 'ActionHandler'):
        """设置依赖项"""
        self.db_handler = db_handler
        self.core_logic = core_logic
        self.action_handler = action_handler
        self.logger.info("消息处理器依赖项已设置")
    
    async def process_event(self, event: Event, websocket: WebSocketServerProtocol) -> None:
        """处理来自适配器的事件"""
        try:
            self.logger.debug(f"处理事件: {event.event_type}, ID: {event.event_id}")
            
            # 根据事件类型分发处理
            if event.event_type.startswith("message."):
                await self._handle_message_event(event, websocket)
            elif event.event_type.startswith("request."):
                await self._handle_request_event(event, websocket)
            elif event.event_type.startswith("action."):
                await self._handle_action_event(event, websocket)
            else:
                self.logger.warning(f"未知的事件类型: {event.event_type}")
                
        except Exception as e:
            self.logger.error(f"处理事件时发生错误: {e}", exc_info=True)
    
    async def _handle_message_event(self, event: Event, websocket: WebSocketServerProtocol) -> None:
        """处理消息事件"""
        try:
            # 保存原始消息到数据库
            await self._save_message_to_database(event)
            
            # 转发给核心逻辑处理
            if self.core_logic:
                # 这里可以调用核心逻辑的消息处理方法
                pass
                
        except Exception as e:
            self.logger.error(f"处理消息事件时发生错误: {e}", exc_info=True)
    
    async def _handle_request_event(self, event: Event, websocket: WebSocketServerProtocol) -> None:
        """处理请求事件（如好友申请、群邀请等）"""
        try:
            # 保存请求信息
            await self._save_request_to_database(event)
            
            # 转发给核心逻辑处理
            if self.core_logic:
                # 这里可以调用核心逻辑的请求处理方法
                pass
                
        except Exception as e:
            self.logger.error(f"处理请求事件时发生错误: {e}", exc_info=True)
    
    async def _handle_action_event(self, event: Event, websocket: WebSocketServerProtocol) -> None:
        """处理动作事件"""
        try:
            # 转发给动作处理器
            if self.action_handler:
                await self.action_handler.handle_action_request(event)
                
        except Exception as e:
            self.logger.error(f"处理动作事件时发生错误: {e}", exc_info=True)
    
    async def _save_message_to_database(self, event: Event) -> None:
        """将消息事件保存到数据库"""
        try:
            if not self.db_handler:
                self.logger.warning("数据库处理器未设置，无法保存消息")
                return
            
            # 构建消息数据
            message_data = {
                "event_id": event.event_id,
                "event_type": event.event_type,
                "timestamp": event.time,
                "platform": event.platform,
                "bot_id": event.bot_id,
                "conversation_id": event.conversation_info.conversation_id if event.conversation_info else None,
                "sender_id": event.user_info.user_id if event.user_info else None,
                "sender_nickname": event.user_info.user_nickname if event.user_info else None,
                "group_id": event.conversation_info.conversation_id if event.conversation_info and event.conversation_info.type.value == "group" else None,
                "group_name": event.conversation_info.name if event.conversation_info and event.conversation_info.type.value == "group" else None,
                "message_content": [seg.to_dict() for seg in event.content],
                "raw_data": event.raw_data,
                # 兼容旧字段名
                "message_id": event.event_id,
                "post_type": "message",
                "sub_type": event.event_type.split(".")[-1] if "." in event.event_type else "normal"
            }
            
            # 保存到数据库
            success = await self.db_handler.save_raw_chat_message(message_data)
            if success:
                self.logger.debug(f"消息已保存到数据库: {event.event_id}")
            else:
                self.logger.warning(f"保存消息到数据库失败: {event.event_id}")
                
        except Exception as e:
            self.logger.error(f"保存消息到数据库时发生错误: {e}", exc_info=True)
    
    async def _save_request_to_database(self, event: Event) -> None:
        """将请求事件保存到数据库"""
        try:
            if not self.db_handler:
                self.logger.warning("数据库处理器未设置，无法保存请求")
                return
            
            # 构建请求数据
            request_data = {
                "event_id": event.event_id,
                "event_type": event.event_type,
                "timestamp": event.time,
                "platform": event.platform,
                "bot_id": event.bot_id,
                "requester_id": event.user_info.user_id if event.user_info else None,
                "requester_nickname": event.user_info.user_nickname if event.user_info else None,
                "content": [seg.to_dict() for seg in event.content],
                "raw_data": event.raw_data
            }
            
            # 这里可以保存到专门的请求表或集合
            # 暂时保存到消息表，添加标识
            request_data["post_type"] = "request"
            request_data["sub_type"] = event.event_type.split(".")[-1] if "." in event.event_type else "unknown"
            
            success = await self.db_handler.save_raw_chat_message(request_data)
            if success:
                self.logger.debug(f"请求已保存到数据库: {event.event_id}")
            else:
                self.logger.warning(f"保存请求到数据库失败: {event.event_id}")
                
        except Exception as e:
            self.logger.error(f"保存请求到数据库时发生错误: {e}", exc_info=True)
