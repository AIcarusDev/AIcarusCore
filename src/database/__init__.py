"""
数据库模块 - 简化的存储解决方案
"""

from .storage_manager import StorageManager
from .models import Event, ActionRecord, Collections

__all__ = ["StorageManager", "Event", "ActionRecord", "Collections"]
