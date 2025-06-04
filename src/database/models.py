"""
数据库模型 - 完全对齐通信协议结构
简化设计，避免过度复杂化
"""
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
import time
from src.common.utils import MessageContentProcessor  # 更新导入路径


@dataclass
class Event:
    """
    核心事件存储模型 - 直接对应协议 Event 对象
    这是我们的主要存储单元
    """
    event_id: str
    event_type: str  # message.group.normal, action.send, notice.group_increase 等
    timestamp: int   # 毫秒时间戳
    platform: str
    bot_id: str
    
    # 核心数据字段 - 直接存储协议原始结构
    content: List[Dict[str, Any]]  # Seg 列表
    user_info: Optional[Dict[str, Any]] = None
    conversation_info: Optional[Dict[str, Any]] = None
    raw_data: Optional[Dict[str, Any]] = None
    
    # 提取的便于查询的字段
    user_id: Optional[str] = None  # 从 user_info 提取
    conversation_id: Optional[str] = None  # 从 conversation_info 提取
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典格式用于数据库存储"""
        return {
            "_key": self.event_id,
            "event_id": self.event_id,
            "event_type": self.event_type,
            "timestamp": self.timestamp,
            "platform": self.platform,
            "bot_id": self.bot_id,
            "content": self.content,
            "user_info": self.user_info,
            "conversation_info": self.conversation_info,
            "raw_data": self.raw_data,
            "user_id": self.user_id,
            "conversation_id": self.conversation_id
        }
    
    @classmethod
    def from_protocol_event(cls, protocol_event: Dict[str, Any]) -> 'Event':
        """从协议 Event 对象创建存储模型"""
        # 提取用户ID和会话ID用于查询优化
        user_id = None
        if protocol_event.get("user_info"):
            user_id = protocol_event["user_info"].get("user_id")
        
        conversation_id = None
        if protocol_event.get("conversation_info"):
            conversation_id = protocol_event["conversation_info"].get("conversation_id")
        
        return cls(
            event_id=protocol_event["event_id"],
            event_type=protocol_event["event_type"],
            timestamp=protocol_event["time"],
            platform=protocol_event["platform"],
            bot_id=protocol_event["bot_id"],
            content=protocol_event.get("content", []),
            user_info=protocol_event.get("user_info"),
            conversation_info=protocol_event.get("conversation_info"),
            raw_data=protocol_event.get("raw_data"),
            user_id=user_id,
            conversation_id=conversation_id
        )
    
    def get_text_content(self) -> str:
        """提取事件的纯文本内容"""
        if self.content:
            return MessageContentProcessor.extract_text_content(self.content)
        return ""


@dataclass 
class ActionRecord:
    """
    动作执行记录 - 用于追踪动作执行状态
    保持简单，只记录必要信息
    """
    action_id: str
    action_type: str  # action.message.send, action.group.kick 等
    timestamp: int
    platform: str
    bot_id: str
    target_conversation_id: Optional[str] = None
    target_user_id: Optional[str] = None
    status: str = "pending"  # pending, success, failed
    response_data: Optional[Dict[str, Any]] = None
    error_message: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典格式用于数据库存储"""
        return {
            "_key": self.action_id,
            "action_id": self.action_id,
            "action_type": self.action_type,
            "timestamp": self.timestamp,
            "platform": self.platform,
            "bot_id": self.bot_id,
            "target_conversation_id": self.target_conversation_id,
            "target_user_id": self.target_user_id,
            "status": self.status,
            "response_data": self.response_data,
            "error_message": self.error_message
        }


# 数据库集合名称常量
class Collections:
    EVENTS = "events"
    ACTIONS = "actions"


# 预定义索引结构
DATABASE_INDEXES = {
    Collections.EVENTS: [
        ["event_type", "timestamp"],  # 按类型和时间查询
        ["platform", "bot_id", "timestamp"],  # 按平台和机器人查询
        ["conversation_id", "timestamp"],  # 按会话查询
        ["user_id", "timestamp"],  # 按用户查询
        ["timestamp"],  # 纯时间查询
    ],
    Collections.ACTIONS: [
        ["action_type", "timestamp"],
        ["status", "timestamp"],
        ["platform", "bot_id", "timestamp"],
        ["target_conversation_id", "timestamp"],
    ]
}
