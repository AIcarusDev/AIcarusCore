# src/database/__init__.py

# 导出连接管理器、新的服务类、以及相关的核心模型和常量类
from .core.connection_manager import ArangoDBConnectionManager, CoreDBCollections
from .models import (
    ActionRecordDocument,
    AttentionProfile,
    DBEventDocument,
    EnrichedConversationInfo,
)  # 从 models.py 导入
from .services.conversation_storage_service import ConversationStorageService
from .services.event_storage_service import EventStorageService
from .services.thought_storage_service import ThoughtStorageService

__all__ = [
    "ArangoDBConnectionManager",  # 底层数据库连接和通用操作管理器
    "CoreDBCollections",  # 核心集合名称和索引定义的常量类
    "ConversationStorageService",  # 会话信息（包含注意力档案）的存储服务
    "EventStorageService",  # 事件的存储服务
    "ThoughtStorageService",  # 思考（主意识思考和侵入性思维）的存储服务
    "AttentionProfile",  # 注意力及偏好档案的数据模型
    "EnrichedConversationInfo",  # 运行时整合了注意力档案的会话信息对象
    "DBEventDocument",  # 代表数据库中事件文档的运行时对象 (如果与协议对象有差异或需要特定DB逻辑)
    "ActionRecordDocument",  # 代表数据库中动作执行记录的运行时对象
]
