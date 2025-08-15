# src/database/models.py
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Literal


@dataclass
class EnrichedConversationInfo:
    """一个数据传输对象 (DTO)，用于在创建 ChatSession 时传递丰富的会话上下文.

    它不是一个直接映射到数据库集合的模型。
    """

    platform: str
    bot_id: str
    conversation_id: str
    type: str | None = None
    name: str | None = None
    parent_id: str | None = None
    avatar: str | None = None
    extra: dict = field(default_factory=dict)


# 这些 dataclass 是我们在 schema.tql 中定义的实体属性的 Python 表现形式。
# 它们提供了类型安全，并使得服务层代码更清晰。
@dataclass
class AccountDetails:
    """'account' 实体的 details 属性."""

    platform: str
    platform_id: str
    nickname: str | None = None
    last_known_nickname: str | None = None
    friend_remark: str | None = None
    friend_request_pending: dict | None = None


@dataclass
class PlatformDetails:
    """'platform' 实体的 details 属性."""

    platform_id: str
    display_name: str


@dataclass
class ConversationDetails:
    """'conversation' 实体的 details 属性."""

    platform: str
    conversation_id: str
    type: str  # 'group' or 'private'
    name: str | None = None
    parent_id: str | None = None
    avatar: str | None = None
    extra: dict = field(default_factory=dict)


@dataclass
class MembershipProperties:
    """'is_present_in' 边的属性."""

    group_name: str | None = None
    cardname: str | None = None
    permission_level: str | None = None
    title: str | None = None
    last_active_timestamp: int | None = None

    def to_dict(self) -> dict[str, Any]:
        """将 dataclass 转换为字典，并移除值为 None 的字段."""
        return {k: v for k, v in asdict(self).items() if v is not None}


# 实体类型字面量
EntityTypeLiteral = Literal["account", "platform", "conversation", "unknown"]

# 将实体类型映射到其对应的 Details dataclass
ENTITY_TYPE_TO_DETAILS_CLASS: dict[EntityTypeLiteral, type] = {
    "account": AccountDetails,
    "platform": PlatformDetails,
    "conversation": ConversationDetails,
}


@dataclass
class EntityDocument:
    """一个统一的领域模型，代表从 'entities' 集合中获取的任何实体.

    它包含所有实体共有的字段，以及一个类型化的 'details' 字段。
    """

    _key: str
    entity_uid: str
    entity_type: EntityTypeLiteral
    details: AccountDetails | PlatformDetails | ConversationDetails | dict | None
    created_at: int = field(default_factory=lambda: int(time.time() * 1000))
    # 为 last_read_timestamp 添加默认值
    last_read_timestamp: float = 0.0
    # 为 bot_profile_in_this_conversation 添加默认值
    bot_profile_in_this_conversation: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """将 EntityDocument 转换为适合存入数据库的字典."""
        data = asdict(self)
        if hasattr(self.details, "to_dict"):
            data["details"] = self.details.to_dict()  # type: ignore
        elif isinstance(self.details, dict):
            data["details"] = self.details
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EntityDocument":
        """从字典创建 EntityDocument 实例，并自动转换 'details' 字段."""
        if not data:
            # 返回一个空的或默认的实例，或者抛出异常，取决于业务需求
            # 这里我们选择返回一个带有 'unknown' 类型的默认实例
            return cls(
                _key="unknown",
                entity_uid="unknown",
                entity_type="unknown",
                details={},
            )
        entity_type = data.get("entity_type", "unknown")
        details_data = data.get("details")
        details_class = ENTITY_TYPE_TO_DETAILS_CLASS.get(entity_type)  # type: ignore

        details_obj = None
        if details_class and isinstance(details_data, dict):
            # 使用 inspect 来动态地只传递 dataclass 需要的字段
            import inspect

            sig = inspect.signature(details_class)
            valid_keys = {p.name for p in sig.parameters.values()}
            filtered_data = {k: v for k, v in details_data.items() if k in valid_keys}
            details_obj = details_class(**filtered_data)
        elif isinstance(details_data, dict):
            details_obj = details_data  # 如果没有对应的dataclass，则保留为字典

        # 使用 pop 来避免将它们传递给构造函数两次
        data.pop("details", None)
        return cls(details=details_obj, **data)


class CoreDBCollections:
    """数据库核心集合的“唯一真实来源” (Single Source of Truth).

    所有核心集合和服务都应在这里注册它们的名称。
    这遵循了我们在 mention.md 中定下的“神圣契约”.
    """

    # 文档集合
    EVENTS = "events"
    THOUGHT_CHAIN = "thought_chain"
    ACTION_LOG = "action_log"
    SUMMARIES = "summaries"
    IMAGE_CACHE = "image_cache"
    GOALS = "goals"
    STICKER_COLLECTION = "sticker_collection"
    # 这是一个特殊的集合，用于存储像“最新思想点指针”这样的系统元数据
    SYSTEM_POINTERS = "system_pointers"
    INTRUSIVE_THOUGHTS = "intrusive_thoughts"

    # 边集合
    PRECEDES_THOUGHT = "precedes_thought"  # 思想链的前后关系
    ACTION_TRIGGERED_BY = "action_triggered_by"  # 动作由哪个思想触发

    @classmethod
    def get_all_collections(cls) -> list[str]:
        """获取所有已定义的集合名称."""
        return [
            cls.EVENTS,
            cls.THOUGHT_CHAIN,
            cls.ACTION_LOG,
            cls.SUMMARIES,
            cls.IMAGE_CACHE,
            cls.GOALS,
            cls.STICKER_COLLECTION,
            cls.SYSTEM_POINTERS,
            cls.INTRUSIVE_THOUGHTS,
        ]

    @classmethod
    def get_all_edge_collections(cls) -> list[str]:
        """获取所有已定义的边集合名称."""
        return [
            cls.PRECEDES_THOUGHT,
            cls.ACTION_TRIGGERED_BY,
        ]


# ==================== [ 结束新增 ] ====================


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
