# src/database/__init__.py

#  导入新的连接管理器和核心模型
from .core.connection_manager import TypeDBConnectionManager
from .models import (
    ActionLogDocument,
    CoreDBCollections,
    EnrichedConversationInfo,  # <-- 导出 EnrichedConversationInfo
    GoalDocument,
    ImageCacheDocument,
    StickerDocument,
    ThoughtChainDocument,
)

#  从服务中导出所有服务类
from .services import (
    ActionLogStorageService,
    EntityGraphService,
    EventStorageService,
    GoalStorageService,
    ImageAnalysisCacheService,
    StickerStorageService,
    ThoughtStorageService,
)

#  更新 __all__ 列表
__all__ = [
    # Models
    "ActionLogDocument",
    # Services
    "ActionLogStorageService",
    # Core Definitions
    "CoreDBCollections",
    "EnrichedConversationInfo",
    "EntityGraphService",
    "EventStorageService",
    "GoalDocument",
    "GoalStorageService",
    "ImageAnalysisCacheService",
    "ImageCacheDocument",
    "StickerDocument",
    "StickerStorageService",
    "ThoughtChainDocument",
    "ThoughtStorageService",
    # Connection Manager
    "TypeDBConnectionManager",
]
