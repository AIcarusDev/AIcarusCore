# src/database/models.py (小色猫·基因重组版)
import json
import time
import uuid
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Optional

# 导入我们全新的、纯洁的协议对象！
from aicarus_protocols import ConversationInfo as ProtocolConversationInfo
from aicarus_protocols import Event as ProtocolEvent

from src.common.custom_logging.logging_config import get_logger

logger = get_logger(__name__)


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
                f"从协议传入的 ConversationInfo 对象缺失或无 conversation_id。 "
                f"平台: '{event_platform}', 机器人ID: '{event_bot_id}'. "
                f"将创建一个临时的 EnrichedConversationInfo ID: '{placeholder_conv_id}'。"
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

        # 兼容旧的枚举类型处理
        conv_type_str = doc.get("type")
        if conv_type_str and not isinstance(conv_type_str, str):
            conv_type_str = str(conv_type_str)  # 强制转字符串

        # 构造函数现在需要 platform，确保从doc中获取
        if "platform" not in doc:
            logger.warning(f"数据库文档 {doc.get('_key')} 缺少 'platform' 字段，无法构建 EnrichedConversationInfo。")
            return None

        # 使用过滤后的数据创建实例，确保只传入dataclass定义的字段
        known_fields = {f.name for f in fields(cls)}
        filtered_data = {k: v for k, v in doc.items() if k in known_fields}

        # 特殊处理 attention_profile，确保它能从字典正确转换
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
    protocol_version: str = "1.6.0"  # 更新协议版本
    user_id_extracted: str | None = None
    conversation_id_extracted: str | None = None
    motivation: str | None = None
    embedding: list[float] | None = field(default=None, repr=False) # 句子嵌入向量，默认不显示在repr中
    status: str = "unread"

    @classmethod
    def from_protocol(cls, proto_event: ProtocolEvent) -> "DBEventDocument":
        """
        从 `aicarus_protocols.Event` v1.6.0 对象创建一个 `DBEventDocument` 实例。
        """
        if not isinstance(proto_event, ProtocolEvent):
            raise TypeError("输入对象必须是 aicarus_protocols.Event 的实例。")

        # --- ❤❤❤ 最终高潮点！从 event_type 中解析出 platform！❤❤❤ ---
        platform_id = proto_event.get_platform()
        if not platform_id:
            # 这是一个防御性措施，理论上在 MessageProcessor 已经检查过了
            logger.error(f"无法从事件类型 '{proto_event.event_type}' 中解析平台ID，将使用 'unknown' 作为备用。")
            platform_id = "unknown"

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

        raw_data_dict = None
        if proto_event.raw_data:
            if isinstance(proto_event.raw_data, dict):
                raw_data_dict = proto_event.raw_data
            else:
                try:
                    # 尝试将原始数据解析为JSON
                    raw_data_dict = json.loads(str(proto_event.raw_data))
                    if not isinstance(raw_data_dict, dict):
                        raw_data_dict = {"_raw_content_as_string_": str(proto_event.raw_data)}
                except (json.JSONDecodeError, TypeError):
                    raw_data_dict = {"_raw_content_as_string_": str(proto_event.raw_data)}

        return cls(
            _key=str(proto_event.event_id),
            event_id=str(proto_event.event_id),
            event_type=str(proto_event.event_type),
            timestamp=int(proto_event.time),
            platform=platform_id,  # 使用我们解析出来的 platform_id！
            bot_id=str(proto_event.bot_id),
            content=content_as_dicts,
            user_info=user_info_dict,
            conversation_info=conversation_info_dict,
            raw_data=raw_data_dict,
            protocol_version=__import__("aicarus_protocols").__version__ or "1.6.0",
            user_id_extracted=uid_ext,
            conversation_id_extracted=cid_ext,
            motivation=getattr(proto_event, "motivation", None),
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
