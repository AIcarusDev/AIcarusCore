# src/database/models.py
import json
import time
import uuid
from dataclasses import MISSING, asdict, dataclass, field, fields
from typing import Any, ClassVar, Optional

from aicarus_protocols import ConversationInfo as ProtocolConversationInfo
from aicarus_protocols import Event as ProtocolEvent
from src.common.custom_logging.logging_config import get_logger

logger = get_logger(__name__)


# --- 核心改造点：定义新的集合和图 ---
class CoreDBCollections:
    """一个中央管家，负责记下所有核心集合的名字和它们的类型."""

    # --- 点集合 (Vertex Collections) ---
    ENTITY_PROFILES = "EntityProfiles"  # (原 persons) 存放主观侧写
    ENTITIES = "Entities"  # (原 accounts) 存放所有客观实体 (包括人和会话)
    EVENTS = "events"
    ACTION_LOGS = "action_logs"
    CONVERSATION_SUMMARIES = "conversation_summaries"
    THOUGHTS_LEGACY = "thoughts_collection"  # 旧的思考先留着，免得出错
    THOUGHT_CHAIN = "thought_chain"  # 这就是我们全新的“思想点”集合！
    SYSTEM_STATE = "system_state"  # 用来存放指针的小盒子
    INTRUSIVE_POOL_COLLECTION = "intrusive_thoughts_pool"  # 侵入性思维池

    # --- 边集合 (Edge Collections) ---
    REPRESENTS = "represents"  # _from: EntityProfiles, _to: Entities (特指 Account 类型的 Entity)
    IS_PRESENT_IN = "is_present_in"  # _from: Entities (Account), _to: Entities (Conversation)
    RESIDES_ON = "resides_on"  # _from: Entities (Conversation), _to: Entities (Platform)
    PRECEDES_THOUGHT = "precedes_thought"  # 新的“线”，用来串点！
    LEADS_TO_ACTION = "leads_to_action"  # 这个也最好有

    # --- 图 (Graphs) ---
    MAIN_GRAPH_NAME = "entity_cognition_graph"  # (原 person_relation_graph)
    THOUGHT_GRAPH_NAME = "consciousness_graph"  # 给思想和行动也建个图

    INDEX_DEFINITIONS: ClassVar[dict[str, list[tuple[list[str], bool, bool]]]] = {
        EVENTS: [
            (["event_type", "timestamp"], False, False),
            (["platform", "bot_id", "timestamp"], False, False),
            (["conversation_id_extracted", "timestamp"], False, True),
            (["user_id_extracted", "timestamp"], False, True),
            (["timestamp"], False, False),
        ],
        ENTITY_PROFILES: [
            (["profile_id"], True, False),  # 原 person_id
        ],
        ENTITIES: [
            (["entity_uid"], True, False),  # 原 account_uid
            (["details.platform", "details.platform_id"], True, True),
            (["entity_type"], False, False),  # 为实体类型添加索引
            (["details.type"], False, True),  # 为会话类型添加稀疏索引
        ],
        THOUGHTS_LEGACY: [
            (["timestamp"], False, False),
            (["action.action_id"], True, True),
        ],
        ACTION_LOGS: [
            (["action_id"], True, False),
            (["timestamp"], False, False),
            (["result_details.sent_message_id"], False, True),
        ],
        CONVERSATION_SUMMARIES: [
            (["conversation_id", "timestamp"], False, False),
            (["timestamp"], False, False),
        ],
        THOUGHT_CHAIN: [
            (["timestamp"], False, False),
            (["source_type"], False, False),
            (["source_id"], False, True),
            (["action_id"], True, True),
        ],
        SYSTEM_STATE: [],  # 这个集合只有一条记录，不需要索引
    }

    @classmethod
    def get_all_collection_names(cls) -> set[str]:
        """返回所有核心集合的名称."""
        return {
            cls.ENTITY_PROFILES,
            cls.ENTITIES,
            cls.EVENTS,
            cls.ACTION_LOGS,
            cls.CONVERSATION_SUMMARIES,
            cls.THOUGHT_CHAIN,
            cls.SYSTEM_STATE,
            cls.INTRUSIVE_POOL_COLLECTION,
            cls.THOUGHTS_LEGACY,
            # Edges
            cls.REPRESENTS,
            cls.IS_PRESENT_IN,
            cls.RESIDES_ON,
            cls.PRECEDES_THOUGHT,
            cls.LEADS_TO_ACTION,
        }

    @classmethod
    def get_all_core_collection_configs(cls) -> dict[str, list[tuple[list[str], bool, bool]]]:
        """返回所有核心集合的配置，包括索引定义."""
        return cls.INDEX_DEFINITIONS

    @classmethod
    def get_edge_collection_names(cls) -> set[str]:
        """返回所有在图中作为“边”的集合的名称."""
        return {
            cls.REPRESENTS,
            cls.IS_PRESENT_IN,
            cls.RESIDES_ON,
            cls.PRECEDES_THOUGHT,
            cls.LEADS_TO_ACTION,
        }

    @classmethod
    def get_vertex_collection_names(cls) -> set[str]:
        """返回所有在图中作为“点”的集合的名称."""
        return {
            cls.ENTITY_PROFILES,
            cls.ENTITIES,  # conversations 集合已经光荣退休，其职责被 ENTITIES 继承
            cls.THOUGHT_CHAIN,
            cls.ACTION_LOGS,
        }


# ==============================================================================
# Phase 1.2: 定义 Details 强类型结构 (这部分是全新的，prpr)
# ==============================================================================


@dataclass
class BaseEntityDetails:
    """一个所有 Details 的基类.

    (虽然现在是空的，但为未来扩展留下无限可能，就像Galgame的隐藏线一样！)
    """

    pass


@dataclass
class AccountDetails(BaseEntityDetails):
    """为 "account" 类型定义的 Details，存放客观账户信息."""

    platform: str
    platform_id: str
    nickname: str | None = None
    avatar: str | None = None
    last_known_nickname: str | None = None
    friend_remark: str | None = None
    friend_request_pending: dict[str, Any] | None = None


@dataclass
class ConversationDetails(BaseEntityDetails):
    """为 "conversation" 类型定义的 Details，存放客观会话信息."""

    platform: str
    conversation_id: str
    type: str  # e.g., 'group' or 'private'
    name: str | None = None
    parent_id: str | None = None
    avatar: str | None = None
    # 这个 extra 就是我们的“神之手”，用来装平台特有的、非通用的垃圾！(￣▽￣)"
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class PlatformDetails(BaseEntityDetails):
    """为 "platform" 类型定义的 Details，存放客观平台信息."""

    platform_id: str
    display_name: str | None = None


DetailsUnion = AccountDetails | ConversationDetails | PlatformDetails


@dataclass
class EntityDocument:
    """新世界的基石！代表 'Entities' 集合中的一个客观实体节点，可以是账户或会话."""

    _key: str  # e.g., "qq_123456" or "qq_group_98765"
    entity_uid: str
    entity_type: str  # "account" or "conversation" or "platform"
    details: DetailsUnion
    _id: str | None = None  # 新增: 承载数据库返回的完整ID
    _rev: str | None = None  # 新增: 承载数据库返回的修订版本号
    created_at: int = field(default_factory=lambda: int(time.time() * 1000))
    # 注意：像 last_processed_timestamp 和 attention_profile 这种主观状态，已被移出客观实体
    # 它们应该由更高层的逻辑或专门的“状态”集合来管理

    def to_dict(self) -> dict[str, Any]:
        """将实例序列化为可存入DB的字典."""
        return asdict(self)

    @classmethod
    def _create_details_obj(
        cls, details_data: dict, details_class: type[DetailsUnion]
    ) -> DetailsUnion:
        """内部辅助方法：验证数据、过滤并创建 Details 数据类实例."""
        # 1. 找出目标数据类所有没有默认值的必填字段
        required_fields = {
            f.name
            for f in fields(details_class)
            # 直接使用 MISSING 常量，而不是 field.MISSING
            if f.default is MISSING and f.default_factory is MISSING
        }
        # 2. 检查传入的数据是否缺少了任何必填字段
        if missing_fields := required_fields - set(details_data.keys()):
            # 3. 如果有缺失，抛出带有详细信息的 ValueError
            raise ValueError(
                f"无法创建 {details_class.__name__} 实例。 "
                f"传入的 details 数据中缺少必填字段: {', '.join(sorted(missing_fields))}"
            )

        # 4. 从传入的数据中，只筛选出目标数据类认识的字段，忽略数据库中可能存在的其他旧字段
        known_fields = {f.name for f in fields(details_class)}
        filtered_data = {k: v for k, v in details_data.items() if k in known_fields}

        # 5. 使用过滤后的干净数据安全地创建实例
        return details_class(**filtered_data)

    @classmethod
    def from_dict(cls, data: dict) -> "EntityDocument":
        """从数据库字典反序列化为强类型对象."""
        entity_type = data.get("entity_type")
        details_data = data.get("details", {})
        details_obj: DetailsUnion

        try:
            if entity_type == "account":
                details_obj = cls._create_details_obj(details_data, AccountDetails)
            elif entity_type == "conversation":
                details_obj = cls._create_details_obj(details_data, ConversationDetails)
            elif entity_type == "platform":
                details_obj = cls._create_details_obj(details_data, PlatformDetails)
            else:
                raise ValueError(f"从数据库加载实体时遇到未知的 entity_type: {entity_type}")
        except ValueError as e:
            # 捕获验证错误并重新抛出，附加上下文信息
            raise ValueError(f"反序列化实体 '{data.get('_key')}' 的 details 字段时失败: {e}") from e

        # 1. 获取 EntityDocument 类自身定义的所有字段名称。
        defined_fields = {f.name for f in fields(cls)}

        # 2. 从数据库返回的 data 字典中，只筛选出那些我们类中定义过的字段。
        #    这样就能自动忽略掉数据库附带的 _id, _rev 等元数据。
        constructor_args = {k: v for k, v in data.items() if k in defined_fields}

        # 3. 用我们手动创建的 details_obj 替换掉筛选后的参数字典中可能存在的旧 details 字典。
        constructor_args["details"] = details_obj

        # 4. 使用这个干净、安全的参数字典来创建实例。
        return cls(**constructor_args)


@dataclass
class ThoughtChainDocument:
    """代表 thought_chain 集合中的一个“思想点”节点."""

    _key: str
    timestamp: str
    mood: str
    think: str
    goal: str | None
    source_type: str  # 'core' 或 'focus_chat'
    source_id: str | None = None  # 如果是 focus_chat，这里是 conversation_id

    # 包含执行的动作信息，如果有的话
    action_id: str | None = None
    action_payload: dict | None = None
    action_result: str | None = None
    messages_planned: int | None = None
    messages_sent: int | None = None

    def to_dict(self) -> dict[str, Any]:
        """将 dataclass 实例转换为字典."""
        return asdict(self)


@dataclass
class SubjectiveProfile:
    """一个实体的主观侧写档案，存放推断信息."""

    sex: str | None = None
    age: int | None = None
    area: str | None = None


@dataclass
class EntityProfileDocument:
    """代表 'EntityProfiles' 集合中的一个主观侧写节点."""

    _key: str  # profile_id
    profile_id: str
    profile: SubjectiveProfile = field(default_factory=SubjectiveProfile)
    created_at: int = field(default_factory=lambda: int(time.time() * 1000))
    updated_at: int = field(default_factory=lambda: int(time.time() * 1000))

    @classmethod
    def create_new(cls) -> "EntityProfileDocument":
        """创建一个新的 EntityProfileDocument 实例."""
        profile_id = f"profile_{uuid.uuid4()}"
        return cls(_key=profile_id, profile_id=profile_id)

    def to_dict(self) -> dict[str, Any]:
        """将 EntityProfileDocument 实例转换为字典."""
        return asdict(self)


@dataclass
class MembershipProperties:
    """代表 'memberships' 集合中的一个成员属性文档."""

    group_name: str | None = None
    cardname: str | None = None
    permission_level: str | None = None
    title: str | None = None
    last_active_timestamp: int = field(default_factory=lambda: int(time.time() * 1000))

    def to_dict(self) -> dict[str, Any]:
        """将 MembershipProperties 实例转换为字典."""
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass
class AttentionProfile:
    """代表 AI 对某个会话的注意力档案 (这是一个逻辑对象，不直接映射到单个集合)."""

    base_importance_score: float = 0.5
    ai_preference_score: float = 0.5
    relevant_topic_tags: list[str] = field(default_factory=list)
    last_ai_interaction_timestamp: int | None = None
    last_significant_event_timestamp: int | None = None
    cooldown_until_timestamp: int | None = None
    is_suspended_by_ai: bool = False
    suspension_reason: str | None = None
    ai_custom_notes: str | None = None

    @classmethod
    def get_default_profile(cls) -> "AttentionProfile":
        """返回一个具有默认值的 AttentionProfile 实例，用于新会话的初始化."""
        return cls(ai_custom_notes="新发现的会话，注意力档案待初始化。")

    def to_dict(self) -> dict[str, Any]:
        """将 AttentionProfile 实例转换为字典，以便能够存入数据库."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "AttentionProfile":
        """从字典创建 AttentionProfile 实例."""
        if data is None:
            return cls.get_default_profile()
        known_fields = {f.name for f in fields(cls)}
        filtered_data = {k: v for k, v in data.items() if k in known_fields}
        return cls(**filtered_data)


@dataclass
class EnrichedConversationInfo:
    """一个用于在服务层之间传递会话信息的DTO (数据传输对象).

    它不直接映射到任何一个数据库集合，而是根据需要从多个地方组装而成.
    """

    conversation_id: str
    platform: str
    bot_id: str
    type: str | None = None
    name: str | None = None
    parent_id: str | None = None
    avatar: str | None = None
    created_at: int = field(default_factory=lambda: int(time.time() * 1000))
    updated_at: int = field(default_factory=lambda: int(time.time() * 1000))
    last_processed_timestamp: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    attention_profile: AttentionProfile = field(
        default_factory=AttentionProfile.get_default_profile
    )
    bot_profile_in_this_conversation: dict[str, Any] | None = None

    # 这个类现在作为逻辑对象，它的构建和转换方法需要被上层服务重新定义，
    # 这里保留骨架以兼容旧的上层代码调用。
    @classmethod
    def from_protocol_and_event_context(
        cls,
        proto_conv_info: ProtocolConversationInfo | None,
        event_platform: str,
        event_bot_id: str,
    ) -> "EnrichedConversationInfo":
        """从协议层 `ConversationInfo` 和事件上下文创建实例."""
        current_time_ms = int(time.time() * 1000)

        if proto_conv_info and proto_conv_info.conversation_id:
            return cls(
                conversation_id=str(proto_conv_info.conversation_id),
                platform=event_platform,
                bot_id=event_bot_id,
                type=proto_conv_info.type,
                name=proto_conv_info.name,
                parent_id=proto_conv_info.parent_id,
                extra=proto_conv_info.extra if proto_conv_info.extra is not None else {},
                attention_profile=AttentionProfile.get_default_profile(),
                created_at=current_time_ms,
                updated_at=current_time_ms,
            )
        else:
            placeholder_conv_id = f"derived_missing_id_{event_platform}_{str(uuid.uuid4())[:8]}"
            logger.error(
                f"从协议传入的 ConversationInfo 对象缺失或无 conversation_id。"
                f"将创建临时的 EnrichedConversationInfo ID: '{placeholder_conv_id}'。"
            )
            return cls(
                conversation_id=placeholder_conv_id,
                platform=event_platform,
                bot_id=event_bot_id,
                type="unknown",
                attention_profile=AttentionProfile.get_default_profile(),
                created_at=current_time_ms,
                updated_at=current_time_ms,
            )


@dataclass
class DBEventDocument:
    """代表存储在数据库中的事件文档结构."""

    _key: str
    event_id: str
    event_type: str
    timestamp: int
    platform: str
    bot_id: str
    content: list[dict[str, Any]]
    user_info: dict[str, Any] | None = None
    conversation_info: dict[str, Any] | None = None
    raw_data: dict[str, Any] | None = None
    protocol_version: str = "1.6.0"
    user_id_extracted: str | None = None
    conversation_id_extracted: str | None = None
    person_id_associated: str | None = None
    motivation: str | None = None
    embedding: list[float] | None = field(default=None, repr=False)
    image_analysis: list[dict[str, Any]] | None = None
    status: str = "unread"

    @classmethod
    def from_protocol(cls, proto_event: ProtocolEvent) -> "DBEventDocument":
        """从 `aicarus_protocols.Event` v1.6.0 对象创建一个 `DBEventDocument` 实例."""
        if not isinstance(proto_event, ProtocolEvent):
            raise TypeError("输入对象必须是 aicarus_protocols.Event 的实例。")
        platform_id = proto_event.get_platform() or "unknown"
        uid_ext = (
            str(proto_event.user_info.user_id)
            if proto_event.user_info and proto_event.user_info.user_id
            else None
        )
        cid_ext = (
            str(proto_event.conversation_info.conversation_id)
            if proto_event.conversation_info and proto_event.conversation_info.conversation_id
            else None
        )
        content_as_dicts = (
            [seg.to_dict() for seg in proto_event.content] if proto_event.content else []
        )
        user_info_dict = proto_event.user_info.to_dict() if proto_event.user_info else None
        conversation_info_dict = (
            proto_event.conversation_info.to_dict() if proto_event.conversation_info else None
        )
        motivation_from_raw = None
        raw_data_dict = None

        if proto_event.raw_data:
            try:
                parsed_raw_data = json.loads(str(proto_event.raw_data))
                if isinstance(parsed_raw_data, dict):
                    raw_data_dict = parsed_raw_data
                    motivation_from_raw = raw_data_dict.get("motivation")
            except (json.JSONDecodeError, TypeError):
                raw_data_dict = {"_raw_content_as_string_": str(proto_event.raw_data)}

        return cls(
            _key=str(proto_event.event_id),
            event_id=str(proto_event.event_id),
            event_type=str(proto_event.event_type),
            timestamp=int(proto_event.time),
            platform=platform_id,
            bot_id=str(proto_event.bot_id),
            content=content_as_dicts,
            user_info=user_info_dict,
            conversation_info=conversation_info_dict,
            raw_data=raw_data_dict,
            protocol_version=__import__("aicarus_protocols").__version__ or "1.6.0",
            user_id_extracted=uid_ext,
            conversation_id_extracted=cid_ext,
            motivation=motivation_from_raw,
        )

    def to_dict(self) -> dict[str, Any]:
        """将此 DBEventDocument 实例转换为字典，用于数据库存储."""
        return asdict(self)

    def get_text_content_from_segs(self) -> str:
        """从 'content' (Seg字典列表) 中提取所有纯文本内容."""
        if not self.content:
            return ""
        text_parts = []
        for seg_dict in self.content:
            if seg_dict.get("type") == "text" and isinstance(seg_dict.get("data"), dict):
                text_parts.append(seg_dict["data"].get("text", ""))
        return "".join(text_parts).strip()


@dataclass
class ConversationSummaryDocument:
    """代表存储在数据库中的会话总结文档结构."""

    _key: str
    summary_id: str
    conversation_id: str
    timestamp: int
    platform: str
    bot_id: str
    summary_text: str
    event_ids_covered: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """将此 ConversationSummaryDocument 实例转换为字典，用于数据库存储."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> Optional["ConversationSummaryDocument"]:
        """从数据库文档字典创建 ConversationSummaryDocument 实例."""
        if not data:
            return None
        known_fields = {f.name for f in fields(cls)}
        filtered_data = {k: v for k, v in data.items() if k in known_fields}
        if "_key" not in filtered_data and "summary_id" in filtered_data:
            filtered_data["_key"] = filtered_data["summary_id"]
        elif "_key" not in filtered_data:
            logger.error(
                f"无法从字典创建 ConversationSummaryDocument：缺少 'summary_id' 或 '_key'。"
                f"数据: {data}"
            )
            return None
        return cls(**filtered_data)


@dataclass
class ActionRecordDocument:
    """代表存储在数据库中的 Action 执行记录的文档结构."""

    _key: str
    action_id: str
    action_type: str
    timestamp: int
    platform: str
    bot_id: str
    status: str = "pending"
    target_conversation_id: str | None = None
    target_user_id: str | None = None
    parameters: dict[str, Any] | None = None
    result_data: dict[str, Any] | None = None
    error_message: str | None = None
    initiated_by_event_id: str | None = None
    initiated_by_thought_id: str | None = None
    completed_at_timestamp: int | None = None

    def to_dict(self) -> dict[str, Any]:
        """将此 ActionRecordDocument 实例转换为字典，用于数据库存储."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> Optional["ActionRecordDocument"]:
        """从数据库文档字典创建 ActionRecordDocument 实例."""
        if not data:
            return None
        known_fields = {f.name for f in fields(cls)}
        filtered_data = {k: v for k, v in data.items() if k in known_fields}
        if "_key" not in filtered_data and "action_id" in filtered_data:
            filtered_data["_key"] = filtered_data["action_id"]
        elif "_key" not in filtered_data:
            logger.error(
                f"无法从字典创建 ActionRecordDocument：缺少 'action_id' 或 '_key'。数据: {data}"
            )
            return None
        return cls(**filtered_data)
