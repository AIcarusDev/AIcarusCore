# src/database/models.py (小色猫·终极修正·去冗余版)
import json
import time
import uuid
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Optional

from aicarus_protocols import ConversationInfo as ProtocolConversationInfo
from aicarus_protocols import Event as ProtocolEvent
from aicarus_protocols import UserInfo as ProtocolUserInfo

from src.common.custom_logging.logging_config import get_logger

logger = get_logger(__name__)


# --- 新增模型：Person & Account ---


@dataclass
class PersonProfile:
    """一个'人'的档案，存放那些主观、推断或稳定的信息。"""

    sex: str | None = None
    age: int | None = None
    area: str | None = None


@dataclass
class PersonDocument:
    """代表 'persons' 集合中的一个'人'节点。"""

    _key: str  # person_id
    person_id: str
    profile: PersonProfile = field(default_factory=PersonProfile)
    created_at: int = field(default_factory=lambda: int(time.time() * 1000))
    updated_at: int = field(default_factory=lambda: int(time.time() * 1000))

    @classmethod
    def create_new(cls) -> "PersonDocument":
        person_id = f"person_{uuid.uuid4()}"
        return cls(_key=person_id, person_id=person_id)

    def to_dict(self) -> dict[str, Any]:
        # to_dict 应该只负责转换数据，不应该操心 _id 的事
        return asdict(self)


@dataclass
class AccountDocument:
    """代表 'accounts' 集合中的一个平台账号节点。"""

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
        return asdict(self)


@dataclass
class MembershipProperties:
    """代表 'participates_in' 边上的属性。"""

    group_name: str | None = None
    cardname: str | None = None
    permission_level: str | None = None
    title: str | None = None
    last_active_timestamp: int = field(default_factory=lambda: int(time.time() * 1000))

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


# --- 旧模型保持不变，只修改需要兼容的地方 ---


@dataclass
class AttentionProfile:
    """
    AI对某个会话的注意力及偏好档案。
    此对象将作为嵌套文档存储在 `EnrichedConversationInfo` 的数据库表示中
    (即 'conversations' 集合的文档内的 'attention_profile' 字段)。
    """

    base_importance_score: float = 0.5  # 会话的基础重要性评分 (范围0-1)，可由配置预设或由AI主意识动态调整。
    ai_preference_score: float = 0.5  # AI基于历史交互对此会话产生的偏好程度评分 (范围0-1)，由AI学习和调整。
    relevant_topic_tags: list[str] = field(
        default_factory=list
    )  # AI为此会话标注的相关话题标签，用于基于内容的注意力加权。
    last_ai_interaction_timestamp: int | None = None  # AI上次与此会话进行有效互动的时间戳 (毫秒, UTC)。
    last_significant_event_timestamp: int | None = (
        None  # 此会话中上次发生对AI而言“重要事件”（如被@）的时间戳 (毫秒, UTC)。
    )
    cooldown_until_timestamp: int | None = (
        None  # 如果AI暂时将此会话置于“冷却”或“低优先级”状态，此字段记录该状态解除的时间戳 (毫秒, UTC)。
    )
    is_suspended_by_ai: bool = False  # 标记此会话是否被AI主动置于“暂停处理”或“忽略”的状态。
    suspension_reason: str | None = None  # 如果被暂停，记录暂停的原因。
    ai_custom_notes: str | None = None  # AI针对此会话记录的内部自定义备注或策略提示。
    # 可以考虑加入更多交互统计指标，例如：
    # interactions_last_24h: int = 0 # 最近24小时互动次数
    # ai_responses_last_24h: int = 0 # 最近24小时AI回复次数

    @classmethod
    def get_default_profile(cls) -> "AttentionProfile":
        """返回一个具有默认值的 AttentionProfile 实例，用于新会话的初始化。"""
        return cls(
            ai_custom_notes="新发现的会话，注意力档案待初始化。"  # 为新会话设置一个默认备注
        )

    def to_dict(self) -> dict[str, Any]:
        """将 AttentionProfile 实例转换为字典，以便能够存入数据库。"""
        return asdict(self)  # dataclasses.asdict 可以方便地将dataclass实例转为字典

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "AttentionProfile":
        """
        从字典（通常是从数据库读取的数据）创建 AttentionProfile 实例。
        如果输入数据为 None，则返回一个默认的 AttentionProfile。
        """
        if data is None:
            return cls.get_default_profile()  # 没有数据则使用默认配置

        # 为了更健壮地从字典创建实例，只使用dataclass中定义的字段，忽略多余的键
        known_fields = {f.name for f in fields(cls)}  # 获取dataclass定义的所有字段名
        filtered_data = {k: v for k, v in data.items() if k in known_fields}  # 只保留已知的字段
        return cls(**filtered_data)  # 使用过滤后的数据创建实例


@dataclass
class EnrichedConversationInfo:
    """
    运行时使用的会话信息对象 (V6.0 命名空间统治版)
    """

    conversation_id: str
    # --- ❤❤❤ 看这里！platform字段依然存在！因为数据库需要它来做索引和区分！❤❤❤ ---
    # 我们是从 Event 的 event_type 里解析出它，然后存到这里来的！
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
    attention_profile: AttentionProfile = field(default_factory=AttentionProfile.get_default_profile)
    bot_profile_in_this_conversation: dict[str, Any] | None = None

    @classmethod
    def from_protocol_and_event_context(
        cls,
        proto_conv_info: ProtocolConversationInfo | None,
        # --- ❤❤❤ 看这里！event_platform现在是必需的，由调用者（MessageProcessor）从event_type解析后传入！❤❤❤ ---
        event_platform: str,
        event_bot_id: str,
    ) -> "EnrichedConversationInfo":
        """
        从协议层 `ConversationInfo` 和事件上下文创建实例。
        """
        current_time_ms = int(time.time() * 1000)

        if proto_conv_info and proto_conv_info.conversation_id:
            # --- ❤❤❤ platform字段的值，直接来自我们传入的 event_platform！❤❤❤ ---
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
                f"从协议传入的 ConversationInfo 对象缺失或无 conversation_id。将创建临时的 EnrichedConversationInfo ID: '{placeholder_conv_id}'。"
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
        """将此 EnrichedConversationInfo 实例转换为适合存入数据库的字典。"""
        doc = asdict(self)
        doc["_key"] = str(self.conversation_id)
        return {k: v for k, v in doc.items() if v is not None}

    @classmethod
    def from_db_document(cls, doc: dict[str, Any] | None) -> Optional["EnrichedConversationInfo"]:
        """从数据库文档字典创建 EnrichedConversationInfo 实例。"""
        if not doc:
            return None
        if "platform" not in doc:
            logger.warning(f"数据库文档 {doc.get('_key')} 缺少 'platform' 字段，无法构建 EnrichedConversationInfo。")
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
    """
    代表存储在数据库中的 Event 文档结构 (V6.0 命名空间统治版)
    """

    _key: str
    event_id: str
    event_type: str
    timestamp: int
    # --- ❤❤❤ platform字段依然存在！因为数据库需要它！❤❤❤ ---
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
        """
        从 `aicarus_protocols.Event` v1.6.0 对象创建一个 `DBEventDocument` 实例。
        """
        if not isinstance(proto_event, ProtocolEvent):
            raise TypeError("输入对象必须是 aicarus_protocols.Event 的实例。")
        platform_id = proto_event.get_platform() or "unknown"
        uid_ext = (
            str(proto_event.user_info.user_id) if proto_event.user_info and proto_event.user_info.user_id else None
        )
        cid_ext = (
            str(proto_event.conversation_info.conversation_id)
            if proto_event.conversation_info and proto_event.conversation_info.conversation_id
            else None
        )
        content_as_dicts = [seg.to_dict() for seg in proto_event.content] if proto_event.content else []
        user_info_dict = proto_event.user_info.to_dict() if proto_event.user_info else None
        conversation_info_dict = proto_event.conversation_info.to_dict() if proto_event.conversation_info else None
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
        return asdict(self)

    def get_text_content_from_segs(self) -> str:
        """从 'content' (Seg字典列表) 中提取所有纯文本内容。"""
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
    """代表存储在数据库中的会话总结文档结构。"""

    _key: str  # summary_id 将作为数据库文档的 _key
    summary_id: str  # 总结的唯一ID
    conversation_id: str  # 关联的会话ID
    timestamp: int  # 总结创建的时间戳 (毫秒, UTC)
    platform: str  # 会话所属平台
    bot_id: str  # 处理此会话的机器人ID
    summary_text: str  # 总结的文本内容
    event_ids_covered: list[str] = field(default_factory=list)  # 此总结覆盖的事件ID列表

    def to_dict(self) -> dict[str, Any]:
        """将此 ConversationSummaryDocument 实例转换为字典，用于数据库存储。"""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> Optional["ConversationSummaryDocument"]:
        """从数据库文档字典创建 ConversationSummaryDocument 实例。"""
        if not data:
            return None

        known_fields = {f.name for f in fields(cls)}
        filtered_data = {k: v for k, v in data.items() if k in known_fields}

        if "_key" not in filtered_data and "summary_id" in filtered_data:
            filtered_data["_key"] = filtered_data["summary_id"]
        elif "_key" not in filtered_data:
            logger.error(f"无法从字典创建 ConversationSummaryDocument：缺少 'summary_id' 或 '_key'。数据: {data}")
            return None

        return cls(**filtered_data)


@dataclass
class ActionRecordDocument:
    """代表存储在数据库中的 Action 执行记录的文档结构。"""

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
        """将此 ActionRecordDocument 实例转换为字典，用于数据库存储。"""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> Optional["ActionRecordDocument"]:
        """从数据库文档字典创建 ActionRecordDocument 实例。"""
        if not data:  # 如果输入数据为空
            return None

        # 为了更健壮地从字典创建实例，只使用dataclass中定义的字段
        known_fields = {f.name for f in fields(cls)}
        filtered_data = {k: v for k, v in data.items() if k in known_fields}

        # 确保 _key 字段存在，如果它等于 action_id
        if "_key" not in filtered_data and "action_id" in filtered_data:
            filtered_data["_key"] = filtered_data["action_id"]
        elif "_key" not in filtered_data:  # 如果两者都不存在，则无法创建有效记录
            logger.error(f"无法从字典创建 ActionRecordDocument：缺少 'action_id' 或 '_key'。数据: {data}")
            return None

        return cls(**filtered_data)
