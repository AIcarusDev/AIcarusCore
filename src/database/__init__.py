"""
数据库模块 - 简化的存储解决方案
"""

from .models import ActionRecord, Collections, Event
from .storage_manager import StorageManager

__all__ = ["StorageManager", "Event", "ActionRecord", "Collections"]
