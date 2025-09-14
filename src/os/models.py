# 文件路径: src/os/models.py

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Literal


# --- 枚举类型 ---
class WindowStatus(Enum):
    """窗口状态的枚举，指示窗口的当前状态."""

    NORMAL = "normal"
    MINIMIZE = "minimize"
    MAXIMIZE = "maximize"


class ApplicationLifecycle(Enum):
    """应用程序生命周期的枚举，指示应用的当前状态."""

    RUNNING = "running"
    STOPPED = "stopped"


# 弹窗类型定义
PopupType = Literal["notification", "interactive", "modal"]

# --- 核心数据结构 ---


@dataclass
class UIElement:
    """代表一个可交互的UI元素，如按钮或快捷方式."""

    id: str  # 唯一标识符
    element_type: str  # 'button', 'shortcut', 'file', 'process'
    title: str  # 显示给AI看的文本

    # 这个 target_uid 是关键，它链接到系统内部的持久化ID
    # 例如，一个QQ快捷方式的 target_uid 是 "app-001"
    # 一个进入会话按钮的 target_uid 是 "qq_group_12345"
    target_uid: str


@dataclass
class Window:
    """代表一个窗口的完整内部状态."""

    name: str # 窗口的唯一、固定名称, e.g., "qq_main", "file_explorer_main"
    parent_app_id: str  # 所属应用的ID，例如 "app-001"
    title: str
    window_class: str  # 例如 "conversation_list", "conversation", "editor"
    status: WindowStatus = WindowStatus.NORMAL

    # z_order 和 last_focused_timestamp 是实现窗口层级和LRU策略的关键
    z_order: int = 0
    last_focused_timestamp: float = field(default_factory=time.time)

    # content_state 用于存储窗口的特定内容状态
    # e.g., for file_explorer: {"current_path": "/desktop/"}
    # e.g., for text_editor: {"tabs": [...], "active_tab_id": "..."}
    content_state: dict = field(default_factory=dict)

    # 弹窗相关属性
    is_popup: bool = False
    popup_type: PopupType | None = None
    # 瞬态弹窗的生命周期（认知周期数），None 表示持久存在
    transient_cycles_remaining: int | None = None


@dataclass
class Application:
    """代表一个应用程序的内部状态."""

    id: str  # 应用的唯一ID，例如 "app-qq"
    name: str  # 内部名称，例如 "qq"
    title: str  # 显示名称，例如 "QQ"
    is_utility: bool = False # 新增：是否为系统工具
    lifecycle: ApplicationLifecycle = ApplicationLifecycle.STOPPED
