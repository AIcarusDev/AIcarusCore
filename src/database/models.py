# src/database/models.py
import json
import time
import uuid
from dataclasses import asdict, dataclass, field, fields
from typing import Any, ClassVar, Optional

from aicarus_protocols import ConversationInfo as ProtocolConversationInfo
from aicarus_protocols import Event as ProtocolEvent
from aicarus_protocols import UserInfo as ProtocolUserInfo
from src.common.custom_logging.logging_config import get_logger

logger = get_logger(__name__)


# --- 核心改造点：定义新的集合和图 ---
class CoreDBCollections:
    """一个中央管家，负责记下所有核心集合的名字和它们的类型."""

    # 点集合 (Vertex Collections)
    PERSONS = "persons"
    ACCOUNTS = "accounts"
    CONVERSATIONS = "conversations"
    EVENTS = "events"
    ACTION_LOGS = "action_logs"
    CONVERSATION_SUMMARIES = "conversation_summaries"
    THOUGHTS_LEGACY = "thoughts_collection"  # 旧的思考先留着，免得出错
    THOUGHT_CHAIN = "thought_chain"  # 这就是我们全新的“思想点”集合！
    SYSTEM_STATE = "system_state"  # 用来存放指针的小盒子
    INTRUSIVE_POOL_COLLECTION = "intrusive_thoughts_pool"  # 侵入性思维池

    # 边集合 (Edge Collections)
    HAS_ACCOUNT = "has_account"
    PARTICIPATES_IN = "participates_in"
    PRECEDES_THOUGHT = "precedes_thought"  # 新的“线”，用来串点！
    LEADS_TO_ACTION = "leads_to_action"  # 这个也最好有

    # 图的名字
    MAIN_GRAPH_NAME = "person_relation_graph"
    THOUGHT_GRAPH_NAME = "consciousness_graph"  # 给思想和行动也建个图

    INDEX_DEFINITIONS: ClassVar[dict[str, list[tuple[list[str], bool, bool]]]] = {
        EVENTS: [
            (["event_type", "timestamp"], False, False),
            (["platform", "bot_id", "timestamp"], False, False),
            (["conversation_id_extracted", "timestamp"], False, True),
            (["user_id_extracted", "timestamp"], False, True),
            (["timestamp"], False, False),
        ],
        CONVERSATIONS: [
            (["platform", "type"], False, False),
            (["updated_at"], False, False),
            (["parent_id"], False, True),
            (["attention_profile.is_suspended_by_ai"], False, True),
            (["attention_profile.base_importance_score"], False, False),
        ],
        PERSONS: [
            (["person_id"], True, False),
        ],
        ACCOUNTS: [
            (["account_uid"], True, False),
            (["platform", "platform_id"], True, False),
        ],
        THOUGHTS_LEGACY: [  # 旧的也保留
            (["timestamp"], False, False),
            (["action.action_id"], True, True),
        ],
        ACTION_LOGS: [
            (["action_id"], True, False),
            (["timestamp"], False, False),
        ],
        CONVERSATION_SUMMARIES: [
            (["conversation_id", "timestamp"], False, False),
            (["timestamp"], False, False),
        ],
        # --- 新集合的索引 ---
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
        """返回所有核心集合的名称.

        这些集合是图数据库的基础，包含了所有重要的节点和边.

        Returns:
            set[str]: 包含所有核心集合名称的集合.
        """
        return {
            cls.PERSONS,
            cls.ACCOUNTS,
            cls.CONVERSATIONS,
            cls.EVENTS,
            cls.ACTION_LOGS,
            cls.CONVERSATION_SUMMARIES,
            cls.THOUGHTS_LEGACY,
            # --- 把新玩具加进来 ---
            cls.THOUGHT_CHAIN,
            cls.SYSTEM_STATE,
            cls.INTRUSIVE_POOL_COLLECTION,  # 别忘了这个新集合
            # --- 边集合 ---
            cls.HAS_ACCOUNT,
            cls.PARTICIPATES_IN,
            cls.PRECEDES_THOUGHT,
            cls.LEADS_TO_ACTION,
        }

    @classmethod
    def get_all_core_collection_configs(cls) -> dict[str, list[tuple[list[str], bool, bool]]]:
        """返回所有核心集合的配置，包括索引定义.

        Returns:
            dict[str, list[tuple[list[str], bool, bool]]]: 包含所有核心集合配置的字典.
        """
        return cls.INDEX_DEFINITIONS

    @classmethod
    def get_edge_collection_names(cls) -> set[str]:
        """返回所有在图中作为“边”的集合的名称.

        这些集合用于连接不同的点，形成关系网络.

        Returns:
            set[str]: 包含所有边集合名称的集合.
        """
        return {cls.HAS_ACCOUNT, cls.PARTICIPATES_IN, cls.PRECEDES_THOUGHT, cls.LEADS_TO_ACTION}

    @classmethod
    def get_vertex_collection_names(cls) -> set[str]:
        """返回所有在图中作为“点”的集合的名称."""
        return {cls.PERSONS, cls.ACCOUNTS, cls.CONVERSATIONS, cls.THOUGHT_CHAIN, cls.ACTION_LOGS}


# --- 新增模型：ThoughtChainDocument (思想点) ---
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

    def to_dict(self) -> dict[str, Any]:
        """将 dataclass 实例转换为字典."""
        return asdict(self)


# --- 已有模型保持不变，这里为了完整性全部贴出 ---


@dataclass
class PersonProfile:
    """一个'人'的档案，存放那些主观、推断或稳定的信息."""

    sex: str | None = None
    age: int | None = None
    area: str | None = None


@dataclass
class PersonDocument:
    """代表 'persons' 集合中的一个人节点.

    这个文档包含了个人的基本信息，如 person_id、profile 等.

    Attributes:
        _key (str): 个人的唯一标识符，通常是 person_id 的前缀加上 UUID.
        person_id (str): 个人的唯一标识符，通常是一个 UUID。
        profile (PersonProfile): 个人的档案信息.
        created_at (int): 个人信息创建的时间戳，单位为毫秒 (UTC).
        updated_at (int): 个人信息最后更新的时间戳，单位为毫秒 (UTC).
    """

    _key: str  # person_id
    person_id: str
    profile: PersonProfile = field(default_factory=PersonProfile)
    created_at: int = field(default_factory=lambda: int(time.time() * 1000))
    updated_at: int = field(default_factory=lambda: int(time.time() * 1000))

    @classmethod
    def create_new(cls) -> "PersonDocument":
        """创建一个新的 PersonDocument 实例.

        Returns:
            PersonDocument: 一个新的 PersonDocument 实例，具有唯一的 person_id.
        """
        person_id = f"person_{uuid.uuid4()}"
        return cls(_key=person_id, person_id=person_id)

    def to_dict(self) -> dict[str, Any]:
        """将 PersonDocument 实例转换为字典.

        Returns:
            dict[str, Any]: 包含所有属性的字典表示形式.
        """
        return asdict(self)


@dataclass
class AccountDocument:
    """代表 'accounts' 集合中的一个账户节点.

    这个文档包含了账户的基本信息，如平台、ID、昵称等.

    Attributes:
        _key (str): 账户的唯一标识符.
        account_uid (str): 账户的 UID.
        platform (str): 账户所在的平台.
        platform_id (str): 账户在平台上的 ID.
        nickname (str | None): 账户的昵称，如果有的话.
        avatar (str | None): 账户的头像 URL，如果有的话.
        created_at (int): 账户创建的时间戳，单位为毫秒 (UTC).
        last_known_nickname (str | None): 最后一次已知的昵称，用于跟踪昵称变化.
        from_user_info (ProtocolUserInfo): 从 ProtocolUserInfo 创建一个新的 AccountDocument 实例.
    """

    _key: str  # account_uid, e.g., 'qq_12345'
    account_uid: str
    platform: str
    platform_id: str  # The actual ID on the platform, e.g., '12345'
    nickname: str | None = None
    avatar: str | None = None
    created_at: int = field(default_factory=lambda: int(time.time() * 1000))
    last_known_nickname: str | None = None

    @classmethod
    def from_user_info(cls, user_info: ProtocolUserInfo, platform: str) -> "AccountDocument":
        """从 ProtocolUserInfo 创建一个新的 AccountDocument 实例.

        Args:
            user_info (ProtocolUserInfo): 用户信息对象.
            platform (str): 平台名称.

        Raises:
            ValueError: 如果 user_info 中没有 user_id.
        """
        if not user_info.user_id:
            raise ValueError("UserInfo必须有user_id才能创建AccountDocument")

        account_uid = f"{platform}_{user_info.user_id}"
        return cls(
            _key=account_uid,
            account_uid=account_uid,
            platform=platform,
            platform_id=user_info.user_id,
            nickname=user_info.user_nickname,
            last_known_nickname=user_info.user_nickname,
        )

    def to_dict(self) -> dict[str, Any]:
        """将 AccountDocument 实例转换为字典.

        Returns:
            dict[str, Any]: 包含所有属性的字典表示形式.
        """
        return asdict(self)


@dataclass
class MembershipProperties:
    """代表 'memberships' 集合中的一个成员属性文档.

    这个文档包含了成员在特定会话中的属性，如群组名称、卡片名称、权限级别等.

    Attributes:
        group_name (str | None): 成员所在群组的名称，如果适用.
        cardname (str | None): 成员在会话中的卡片名称，如果适用.
        permission_level (str | None): 成员在会话中的权限级别，如果适用.
        title (str | None): 成员在会话中的称谓，如果适用.
        avatar (str | None): 成员在会话中的头像，如果适用.
        last_active_timestamp (int): 成员最后活跃的时间戳，单位为毫秒 (UTC).
    """

    group_name: str | None = None
    cardname: str | None = None
    permission_level: str | None = None
    title: str | None = None
    last_active_timestamp: int = field(default_factory=lambda: int(time.time() * 1000))

    def to_dict(self) -> dict[str, Any]:
        """将 MembershipProperties 实例转换为字典.

        Returns:
            dict[str, Any]: 包含所有属性的字典表示形式.
        """
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass
class AttentionProfile:
    """代表 AI 对某个会话的注意力档案.

    这个档案包含了会话的重要性评分、AI偏好、相关话题标签等信息，
    用于动态调整 AI 对该会话的注意力和处理优先级.

    Attributes:
        base_importance_score (float): 会话的基础重要性评分 (范围0-1)，
            可由配置预设或由AI主意识动态调整。
        ai_preference_score (float): AI基于历史交互对此会话产生的偏好程度评分 (范围0-1)，
            由AI学习和调整。
        relevant_topic_tags (list[str]): AI为此会话标注的相关话题标签，用于基于内容的注意力加权。
        last_ai_interaction_timestamp (int | None): AI上次与此会话进行有效互动的时间戳 (毫秒, UTC)。
        last_significant_event_timestamp (int | None): 此会话中上次发生对AI而言“重要事件”
            （如被@）的时间戳 (毫秒, UTC)。
        cooldown_until_timestamp (int | None): 如果AI暂时将此会话置于“冷却”或“低优先级”状态，
           此字段记录该状态解除的时间戳 (毫秒, UTC)。
        is_suspended_by_ai (bool): 标记此会话是否被AI主动置于“暂停处理”或“忽略”的状态。
        suspension_reason (str | None): 如果被暂停，记录暂停的原因。
        ai_custom_notes (str | None): AI针对此会话记录的内部自定义备注或策略提示。
    """

    base_importance_score: float = 0.5
    ai_preference_score: float = 0.5
    relevant_topic_tags: list[str] = field(default_factory=list)
    last_ai_interaction_timestamp: int | None = None
    last_significant_event_timestamp: int | None = None
    cooldown_until_timestamp: int | None = None
    is_suspended_by_ai: bool = False
    suspension_reason: str | None = None
    ai_custom_notes: str | None = None
    # 下面这些字段可以根据需要添加，但目前先注释掉，等有需求再启用
    # interactions_last_24h: int = 0 # 最近24小时互动次数
    # ai_responses_last_24h: int = 0 # 最近24小时AI回复次数

    @classmethod
    def get_default_profile(cls) -> "AttentionProfile":
        """返回一个具有默认值的 AttentionProfile 实例，用于新会话的初始化."""
        return cls(
            ai_custom_notes="新发现的会话，注意力档案待初始化。"  # 为新会话设置一个默认备注
        )

    def to_dict(self) -> dict[str, Any]:
        """将 AttentionProfile 实例转换为字典，以便能够存入数据库."""
        return asdict(self)  # dataclasses.asdict 可以方便地将dataclass实例转为字典

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "AttentionProfile":
        """从字典创建 AttentionProfile 实例.

        如果传入的字典为 None，则返回一个默认的 AttentionProfile 实例.

        Args:
            data (dict[str, Any] | None): 包含 AttentionProfile 数据的字典。
                如果为 None，则使用默认配置.

        Returns:
            AttentionProfile: 创建的 AttentionProfile 实例.
        """
        if data is None:
            return cls.get_default_profile()  # 没有数据则使用默认配置

        # 为了更健壮地从字典创建实例，只使用dataclass中定义的字段，忽略多余的键
        known_fields = {f.name for f in fields(cls)}  # 获取dataclass定义的所有字段名
        filtered_data = {k: v for k, v in data.items() if k in known_fields}  # 只保留已知的字段
        return cls(**filtered_data)  # 使用过滤后的数据创建实例


@dataclass
class EnrichedConversationInfo:
    """代表 'conversations' 集合中的一个会话信息文档.

    这个文档包含了会话的基本信息、注意力档案、创建和更新时间等.

    Attributes:
        conversation_id (str): 会话的唯一标识符.
        platform (str): 会话所属的平台标识符，例如 "qq", "wechat" 等.
        bot_id (str): 处理此会话的机器人的唯一标识符.
        type (str | None): 会话类型，例如 "group", "private" 等.
        name (str | None): 会话的名称或标题.
        parent_id (str | None): 如果是子会话，指向父会话的 ID.
        avatar (str | None): 会话的头像 URL 或标识符.
        created_at (int): 会话创建的时间戳，单位为毫秒 (UTC).
        updated_at (int): 会话信息最后更新的时间戳，单位为毫秒 (UTC).
        last_processed_timestamp (int | None): AI最后处理此会话的时间戳, 单位为毫秒 (UTC).
        extra (dict[str, Any]): 额外的自定义字段，可以存储任意的会话相关信息.
        attention_profile (AttentionProfile): AI对该会话的注意力档案，包含
            注意力评分、偏好标签等信息.
        bot_profile_in_this_conversation (dict[str, Any] | None): 机器人在此会话中的配置文件信息.
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

    @classmethod
    def from_protocol_and_event_context(
        cls,
        proto_conv_info: ProtocolConversationInfo | None,
        event_platform: str,
        event_bot_id: str,
    ) -> "EnrichedConversationInfo":
        """从协议层 `ConversationInfo` 和事件上下文创建实例.

        Args:
            proto_conv_info (ProtocolConversationInfo | None): 协议层传入的会话信息对象。
            event_platform (str): 事件发生的平台标识符，例如 "qq", "wechat" 等。
            event_bot_id (str): 处理此事件的机器人的唯一标识符。

        Returns:
            EnrichedConversationInfo: 创建的会话信息实例。
        """
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

    def to_db_document(self) -> dict[str, Any]:
        """将此 EnrichedConversationInfo 实例转换为适合存入数据库的字典."""
        doc = asdict(self)
        doc["_key"] = str(self.conversation_id)
        return {k: v for k, v in doc.items() if v is not None}

    @classmethod
    def from_db_document(cls, doc: dict[str, Any] | None) -> Optional["EnrichedConversationInfo"]:
        """从数据库文档字典创建 EnrichedConversationInfo 实例."""
        if not doc:
            return None
        if "platform" not in doc:
            logger.warning(
                f"数据库文档 {doc.get('_key')} 缺少 'platform' 字段，"
                f"无法构建 EnrichedConversationInfo。"
            )
            return None
        known_fields = {f.name for f in fields(cls)}
        filtered_data = {k: v for k, v in doc.items() if k in known_fields}
        attention_profile_data = doc.get("attention_profile")
        if isinstance(attention_profile_data, dict):
            filtered_data["attention_profile"] = AttentionProfile.from_dict(attention_profile_data)
        else:
            filtered_data["attention_profile"] = AttentionProfile.get_default_profile()
        return cls(**filtered_data)


@dataclass
class DBEventDocument:
    """代表存储在数据库中的事件文档结构.

    这个文档结构用于存储从 aicarus_protocols.Event v1.6.0 协议对象转换而来的事件数据。
    包含事件的基本信息、内容、用户和会话信息等。

    Attributes:
        _key (str): 数据库文档的唯一键，通常是事件ID。
        event_id (str): 事件的唯一标识符。
        event_type (str): 事件的类型，例如 "message", "reaction" 等。
        timestamp (int): 事件发生的时间戳，单位为毫秒 (UTC)。
        platform (str): 事件发生的平台标识符，例如 "qq", "wechat" 等。
        bot_id (str): 处理此事件的机器人的唯一标识符。
        content (list[dict[str, Any]]): 事件内容的分段列表，每个段落是一个字典，包含类型和数据。
        user_info (dict[str, Any] | None): 事件相关的用户信息，如果有的话。
        conversation_info (dict[str, Any] | None): 事件相关的会话信息，如果有的话。
        raw_data (dict[str, Any] | None): 原始数据包，可能包含额外的上下文信息。
        protocol_version (str): 使用的协议版本，默认为 "1.6.0"。
        user_id_extracted (str | None): 从事件中提取的用户ID，如果有的话。
        conversation_id_extracted (str | None): 从事件中提取的会话ID，如果有的话。
        person_id_associated (str | None): 关联的个人ID，如果有的话。
        motivation (str | None): 从原始数据中提取的动机信息，如果有的话。
        embedding (list[float] | None): 事件内容的嵌入向量表示，默认为 None。
        status (str): 事件的状态，默认为 "unread"。
    """

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
                # 尝试把背包里的东西当JSON解析
                parsed_raw_data = json.loads(str(proto_event.raw_data))
                if isinstance(parsed_raw_data, dict):
                    raw_data_dict = parsed_raw_data
                    # 从解析后的字典里找 motivation
                    motivation_from_raw = raw_data_dict.get("motivation")
            except (json.JSONDecodeError, TypeError):
                # 如果背包里的不是JSON，就当成普通字符串存起来
                raw_data_dict = {"_raw_content_as_string_": str(proto_event.raw_data)}

        # --- ❤❤❤ 组装最终文档！❤❤❤ ---
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
            raw_data=raw_data_dict,  # 把解析后的字典存起来
            protocol_version=__import__("aicarus_protocols").__version__ or "1.6.0",
            user_id_extracted=uid_ext,
            conversation_id_extracted=cid_ext,
            # 如果从背包里掏出了动机，就用它！
            motivation=motivation_from_raw,
        )

    def to_dict(self) -> dict[str, Any]:
        """将此 DBEventDocument 实例转换为字典，用于数据库存储.

        Returns:
            dict[str, Any]: 包含事件信息的字典，适合存入数据库.
        """
        return asdict(self)

    def get_text_content_from_segs(self) -> str:
        """从 'content' (Seg字典列表) 中提取所有纯文本内容."""
        if not self.content:  # 如果内容列表为空
            return ""
        text_parts = []
        for seg_dict in self.content:  # self.content 应该是 List[Dict[str, Any]]
            if seg_dict.get("type") == "text" and isinstance(seg_dict.get("data"), dict):
                # 安全地获取 text 字段，如果不存在则添加空字符串
                text_parts.append(seg_dict["data"].get("text", ""))
        return "".join(text_parts).strip()  # 拼接并去除首尾空格


@dataclass
class ConversationSummaryDocument:
    """代表存储在数据库中的会话总结文档结构."""

    _key: str  # summary_id 将作为数据库文档的 _key
    summary_id: str  # 总结的唯一ID
    conversation_id: str  # 关联的会话ID
    timestamp: int  # 总结创建的时间戳 (毫秒, UTC)
    platform: str  # 会话所属平台
    bot_id: str  # 处理此会话的机器人ID
    summary_text: str  # 总结的文本内容
    event_ids_covered: list[str] = field(default_factory=list)  # 此总结覆盖的事件ID列表

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
                f"无法从字典创建 ConversationSummaryDocument："
                f"缺少 'summary_id' 或 '_key'。数据: {data}"
            )
            return None

        return cls(**filtered_data)


@dataclass
class ActionRecordDocument:
    """代表存储在数据库中的 Action 执行记录的文档结构."""

    _key: str  # action_id 将作为数据库文档的 _key
    action_id: str  # 动作的唯一ID
    action_type: str  # 动作类型，例如 "message.send", "group.kick"
    timestamp: int  # 动作创建或记录的时间戳 (毫秒, UTC)
    platform: str  # 动作执行的目标平台
    bot_id: str  # 执行此动作的机器人ID
    status: str = "pending"  # 动作的当前状态，例如: "pending", "processing", "success", "failed"

    # 关于动作目标的信息
    target_conversation_id: str | None = None  # 目标会话ID
    target_user_id: str | None = None  # 目标用户ID

    parameters: dict[str, Any] | None = None  # 执行此动作所需的具体参数
    result_data: dict[str, Any] | None = None  # 动作成功执行后返回的数据（如果有）
    error_message: str | None = None  # 动作执行失败时的错误信息

    # 可选的关联信息，用于追踪和调试
    initiated_by_event_id: str | None = None  # （如果适用）触发此动作的原始事件的ID
    initiated_by_thought_id: str | None = None  # （如果适用）触发此动作的AI思考过程的ID
    completed_at_timestamp: int | None = None  # 动作完成（成功或失败）的时间戳 (毫秒, UTC)

    def to_dict(self) -> dict[str, Any]:
        """将此 ActionRecordDocument 实例转换为字典，用于数据库存储."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> Optional["ActionRecordDocument"]:
        """从数据库文档字典创建 ActionRecordDocument 实例."""
        if not data:  # 如果输入数据为空
            return None

        # 为了更健壮地从字典创建实例，只使用dataclass中定义的字段
        known_fields = {f.name for f in fields(cls)}
        filtered_data = {k: v for k, v in data.items() if k in known_fields}

        # 确保 _key 字段存在，如果它等于 action_id
        if "_key" not in filtered_data and "action_id" in filtered_data:
            filtered_data["_key"] = filtered_data["action_id"]
        elif "_key" not in filtered_data:  # 如果两者都不存在，则无法创建有效记录
            logger.error(
                f"无法从字典创建 ActionRecordDocument：缺少 'action_id' 或 '_key'。数据: {data}"
            )
            return None

        return cls(**filtered_data)
