# src/database/__init__.py

# 导出连接管理器、新的服务类、以及相关的核心模型和常量类
from .core.connection_manager import ArangoDBConnectionManager, CoreDBCollections, StandardCollection
from .models import (
    AccountDocument,
    ActionRecordDocument,
    AttentionProfile,
    ConversationSummaryDocument,
    DBEventDocument,
    EnrichedConversationInfo,
    MembershipProperties,
    PersonDocument,
    PersonProfile,
)
from .services.action_log_storage_service import ActionLogStorageService
from .services.conversation_storage_service import ConversationStorageService
from .services.event_storage_service import EventStorageService
from .services.person_storage_service import PersonStorageService
from .services.thought_storage_service import ThoughtStorageService

__all__ = [
    "ArangoDBConnectionManager",
    "CoreDBCollections",
    "StandardCollection",
    # 服务
    "ActionLogStorageService",
    "ConversationStorageService",
    "EventStorageService",
    "PersonStorageService",
    "ThoughtStorageService",
    # 模型
    "PersonDocument",
    "AccountDocument",
    "MembershipProperties",
    "PersonProfile",
    "AttentionProfile",
    "EnrichedConversationInfo",
    "DBEventDocument",
    "ActionRecordDocument",
    "ConversationSummaryDocument",
]
