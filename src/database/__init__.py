# src/database/__init__.py

# [修正] 导入新的连接管理器和核心模型
from .core.connection_manager import TypeDBConnectionManager
from .models import (
    ActionLogDocument,
    # [修正] 移除 DBEventDocument
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
    # Models
    "ActionLogDocument",
    "SummaryDocument",
    "GoalDocument",
    "ImageCacheDocument",
    "ThoughtChainDocument",
    "StickerDocument",
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
