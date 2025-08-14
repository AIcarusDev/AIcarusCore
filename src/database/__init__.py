# src/database/__init__.py

# [修正] 导入新的连接管理器和核心模型
from .core.connection_manager import TypeDBConnectionManager
from .models import (
    ActionLogDocument,
    CoreDBCollections,
    EnrichedConversationInfo, # <-- 导出 EnrichedConversationInfo
    GoalDocument,
    ImageCacheDocument,
    StickerDocument,
    SummaryDocument,
    ThoughtChainDocument,
)

# [修正] 从服务中导出所有服务类
from .services import (
    ActionLogStorageService,
    EntityGraphService,
    EventStorageService,
    GoalStorageService,
    ImageAnalysisCacheService,
    StickerStorageService,
    SummaryStorageService,
    ThoughtStorageService,
)

# [修正] 更新 __all__ 列表
__all__ = [
    # Connection Manager
    "TypeDBConnectionManager",
    # Core Definitions
    "CoreDBCollections",
    # Models
    "ActionLogDocument",
    "SummaryDocument",
    "GoalDocument",
    "ImageCacheDocument",
    "ThoughtChainDocument",
    "StickerDocument",
    "EnrichedConversationInfo", # <-- 将其加入 __all__
    # Services
    "ActionLogStorageService",
    "EntityGraphService",
    "EventStorageService",
    "GoalStorageService",
    "ImageAnalysisCacheService",
    "StickerStorageService",
    "SummaryStorageService",
    "ThoughtStorageService",
]