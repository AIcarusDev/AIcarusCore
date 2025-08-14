# src/database/models.py
import time
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class ThoughtChainDocument:
    """代表 thought-chain-node 实体的一个数据类."""

    _key: str
    timestamp: str
    mood: str
    think: str
    intent: str | None
    source_type: str
    source_id: str | None = None
    action_id: str | None = None
    action_payload: dict | None = None
    action_result: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """将 ThoughtChainDocument 转换为字典."""
        return asdict(self)


@dataclass
class ActionLogDocument:
    """代表 action-log 实体的一个数据类."""

    _key: str  # action_id
    action_type: str
    timestamp: int
    platform: str
    bot_id: str
    status: str = "pending"
    response_timestamp: int | None = None
    response_time_ms: int | None = None
    error_info: str | None = None
    result_details: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """将 ActionLogDocument 转换为字典."""
        return asdict(self)


@dataclass
class SummaryDocument:
    """代表 summary 实体的一个数据类."""

    _key: str  # summary_id
    conversation_uid: str
    timestamp: int
    summary_text: str
    event_ids_covered: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """将 SummaryDocument 转换为字典."""
        return asdict(self)


@dataclass
class ImageCacheDocument:
    """代表 image-cache 实体的一个数据类."""

    _key: str  # image_hash
    analysis_result: dict[str, Any]
    version: str
    timestamp: int

    def to_dict(self) -> dict[str, Any]:
        """将 ImageCacheDocument 转换为字典."""
        return asdict(self)


@dataclass
class GoalDocument:
    """代表 goal 实体的一个数据类."""

    _key: str  # goal_id
    goal_text: str
    reason_text: str
    status: str = "active"
    created_at: int = field(default_factory=lambda: int(time.time() * 1000))
    updated_at: int = field(default_factory=lambda: int(time.time() * 1000))

    def to_dict(self) -> dict[str, Any]:
        """将 GoalDocument 转换为字典."""
        return asdict(self)


@dataclass
class StickerDocument:
    """代表 sticker 实体的一个数据类."""

    _key: str  # sticker_uid, e.g., "qq_sticker_001"
    sticker_id: str  # 纯数字ID, e.g., "001"
    filename: str
    impression: str
    image_hash: str
    perceptual_hash: str
    added_at: int

    def to_dict(self) -> dict[str, Any]:
        """将 StickerDocument 转换为字典."""
        return asdict(self)
