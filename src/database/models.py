# src/database/models.py
import time
import uuid
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Optional, Dict

from aicarus_protocols import ConversationInfo as ProtocolConversationInfo
from aicarus_protocols import ConversationType as ProtocolConversationType  # 会话类型的枚举

# 从 aicarus_protocols 导入基础的协议对象定义
# 这些是AI核心与适配器之间通信时使用的数据结构
from aicarus_protocols import Event as ProtocolEvent

from src.common.custom_logging.logger_manager import get_logger  # 日志记录器

logger = get_logger("AIcarusCore.DB.Models")  # 获取日志实例


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
    运行时使用的会话信息对象。
    它整合了来自通信协议的 `ConversationInfo` 核心数据，
    并加入了AI的 `AttentionProfile` 以及一些管理字段（如创建和更新时间戳）。
    这个对象的数据将被转换为字典后存入 'conversations' 集合。
    """

    # 直接来自或对应 aicarus_protocols.ConversationInfo 的字段
    conversation_id: str  # 会话的唯一ID，将用作数据库文档的 _key
    platform: str  # 会话所属平台 (通常从其关联的Event的顶层platform字段获取)
    bot_id: str  # 处理此会话的机器人ID (通常从其关联的Event的顶层bot_id字段获取)
    type: str | None = None  # 会话类型 (使用协议定义的枚举)
    name: str | None = None  # 会话名称 (例如群名、私聊对方昵称)
    parent_id: str | None = None  # 父会话ID (用于支持如频道-子频道-话题之类的嵌套结构)
    avatar: str | None = None  # 会话头像的URL链接

    # 由系统管理的额外字段
    created_at: int = field(default_factory=lambda: int(time.time() * 1000))  # 会话记录首次创建时间戳 (毫秒, UTC)
    updated_at: int = field(default_factory=lambda: int(time.time() * 1000))  # 会话记录最后更新时间戳 (毫秒, UTC)
    last_processed_timestamp: int | None = None  # AI核心处理此会话消息的最新时间戳 (毫秒, UTC)

    # 用于存储协议中可能存在的其他未明确定义的扩展数据
    extra: dict[str, Any] = field(default_factory=dict)

    # 核心增强：AI的注意力及偏好档案
    attention_profile: AttentionProfile = field(default_factory=AttentionProfile.get_default_profile)

        # 【小懒猫的新增字段】
    bot_profile_in_this_conversation: Optional[Dict[str, Any]] = None
    """存储机器人自身在此会话中的档案信息，例如群名片、权限等，作为缓存。"""

    @classmethod
    def from_protocol_and_event_context(
        cls,
        proto_conv_info: ProtocolConversationInfo | None,  # 来自协议的原始 ConversationInfo 对象
        event_platform: str,  # 关联Event的平台信息 (必需)
        event_bot_id: str,  # 关联Event的机器人ID (必需)
    ) -> "EnrichedConversationInfo":
        """
        从协议层 `ConversationInfo` 对象和其关联的 `Event` 上下文信息，
        创建一个 `EnrichedConversationInfo` 实例。
        主要用于当 `DefaultMessageProcessor` 收到新事件，需要创建或准备更新会话档案时。

        Args:
            proto_conv_info: 从事件中解析出的原始协议层 `ConversationInfo` 对象，可能为None。
            event_platform: 该事件发生的平台。
            event_bot_id: 处理该事件的机器人ID。

        Returns:
            一个 `EnrichedConversationInfo` 实例。
        """
        current_time_ms = int(time.time() * 1000)  # 获取当前时间戳

        if proto_conv_info and proto_conv_info.conversation_id:
            raw_type = proto_conv_info.type
            # conv_type = None # 默认是 None
            # if isinstance(raw_type, ProtocolConversationType):
            # conv_type = raw_type # 如果已经是正确的枚举类型，直接用
            # elif isinstance(raw_type, str) and raw_type.strip():
            # 如果是字符串，我们尝试把它转换成枚举
            # try:
            # conv_type = ProtocolConversationType(raw_type)
            # except ValueError:
            # logger.warning(f"无法将字符串 '{raw_type}' 转换为有效的 ProtocolConversationType。将使用 None。")
            # conv_type = None
            # 其他所有乱七八糟的情况（比如是 None 或者别的类型），conv_type 都会保持为 None

            return cls(
                conversation_id=str(proto_conv_info.conversation_id),  # 确保是字符串
                platform=event_platform,  # 使用Event中的platform和bot_id作为权威来源
                bot_id=event_bot_id,
                type=raw_type,
                name=proto_conv_info.name,  # name, parent_id, avatar, extra 都是 Optional
                parent_id=proto_conv_info.parent_id,
                # avatar=proto_conv_info.avatar, 这个暂时不用
                extra=proto_conv_info.extra if proto_conv_info.extra is not None else {},  # 确保extra字段是字典
                attention_profile=AttentionProfile.get_default_profile(),  # 新识别的会话赋予默认的注意力档案
                created_at=current_time_ms,  # 假定此时是首次创建或识别此会话
                updated_at=current_time_ms,
            )
        else:
            # 处理 proto_conv_info 为 None 或缺少 conversation_id 的情况。
            # 这在某些事件类型中是正常的 (例如非消息类事件可能没有完整的会话上下文)，
            # 或者表示适配器未能提供完整的会话信息。
            # 对于需要存档的会话，上层逻辑 (如 DefaultMessageProcessor) 应确保能提供一个稳定的 conversation_id。
            # 例如，对于私聊，如果协议中 conversation_info 为空，但 user_info 存在，
            # DefaultMessageProcessor 可能会用 user_id 作为 conversation_id 来调用此方法。
            # 如果这里仍然无法确定一个稳定的ID，记录错误并可能创建一个占位符ID（不推荐用于长期存储）。
            placeholder_conv_id = f"derived_missing_id_{event_platform}_{str(uuid.uuid4())[:8]}"
            logger.error(
                f"从协议传入的 ConversationInfo 对象缺失或无 conversation_id。 "
                f"平台: '{event_platform}', 机器人ID: '{event_bot_id}'. "
                f"将创建一个临时的 EnrichedConversationInfo ID: '{placeholder_conv_id}'。"
                f"强烈建议上层逻辑确保提供稳定的 conversation_id。"
            )
            return cls(
                conversation_id=placeholder_conv_id,  # 这是一个有问题的ID，仅作占位
                platform=event_platform,
                bot_id=event_bot_id,
                type=ProtocolConversationType.UNKNOWN,  # 类型未知
                attention_profile=AttentionProfile.get_default_profile(),
                created_at=current_time_ms,
                updated_at=current_time_ms,
            )

    def to_db_document(self) -> dict[str, Any]:
        """将此 EnrichedConversationInfo 实例转换为适合存入数据库的字典。"""
        doc = {
            "_key": str(self.conversation_id),  # ArangoDB 主键通常是 _key
            "conversation_id": self.conversation_id,  # 同时保留原始ID字段，便于理解
            "platform": self.platform,
            "bot_id": self.bot_id,  # 将 bot_id 也一并存入，便于区分同一平台下不同机器人的会话数据
            "type": self.type,  # 枚举转为存储值
            "name": self.name,  # Optional字段，如果为None则不包含或值为None（取决于数据库配置）
            "parent_id": self.parent_id,
            "avatar": self.avatar,
            "created_at": self.created_at,  # 已经是毫秒时间戳
            "updated_at": self.updated_at,  # 确保每次保存都更新此时间戳
            "last_processed_timestamp": self.last_processed_timestamp,  # 新增：更新处理时间戳
            "extra": self.extra,  # 已经是字典
            "attention_profile": self.attention_profile.to_dict(),  # 调用AttentionProfile的转换方法
            "bot_profile_in_this_conversation": self.bot_profile_in_this_conversation,
        }
        # 根据需要，可以移除值为None的顶级可选字段，以保持数据库文档的清洁
        # 例如: return {k: v for k, v in doc.items() if v is not None}
        # 但通常ArangoDB可以处理值为null的字段。对于dataclass，asdict会保留所有字段。
        # 为了明确，我们这里只移除值为None且不是核心结构（如extra, attention_profile）的字段。
        return {
            k: v
            for k, v in doc.items()
            if v is not None or k in ["name", "parent_id", "avatar", "extra", "attention_profile"]
        }

    @classmethod
    def from_db_document(cls, doc: dict[str, Any] | None) -> Optional["EnrichedConversationInfo"]:
        """从数据库文档字典创建 EnrichedConversationInfo 实例。"""
        if not doc:  # 如果输入文档为空，则返回None
            return None

        # 从文档中安全地获取各个字段的值，并进行必要的类型转换
        conv_type_str = doc.get("type")  # 会话类型在数据库中可能存为字符串
        conv_type_enum = ProtocolConversationType.UNKNOWN  # 默认类型
        if conv_type_str:  # 如果存在类型字符串
            try:
                conv_type_enum = ProtocolConversationType(conv_type_str)  # 尝试从字符串转换为枚举成员
            except ValueError:  # 如果转换失败（例如数据库中的类型字符串无效）
                logger.warning(
                    f"从数据库加载会话 '{doc.get('conversation_id')}' 时遇到未知的类型字符串 '{conv_type_str}'。"
                    f"将使用默认类型 UNKNOWN。"
                )

        return cls(
            conversation_id=doc["conversation_id"],  # 假定 conversation_id 字段必定存在
            platform=doc["platform"],  # 假定 platform 字段必定存在
            bot_id=doc.get("bot_id", "unknown_bot_in_db_doc"),  # 兼容旧数据可能没有bot_id的情况
            type=conv_type_enum,
            name=doc.get("name"),  # name 是 Optional[str]
            parent_id=doc.get("parent_id"),  # parent_id 是 Optional[str]
            avatar=doc.get("avatar"),  # avatar 是 Optional[str]
            created_at=doc.get("created_at", int(time.time() * 1000)),  # 兼容旧数据可能没有创建时间的情况
            updated_at=doc.get("updated_at", int(time.time() * 1000)),  # 兼容旧数据可能没有更新时间的情况
            last_processed_timestamp=doc.get("last_processed_timestamp"),  # 新增：读取处理时间戳
            extra=doc.get("extra", {}),  # extra 默认为空字典
            attention_profile=AttentionProfile.from_dict(doc.get("attention_profile")),  # 使用from_dict处理None情况
            bot_profile_in_this_conversation=doc.get("bot_profile_in_this_conversation"),
        )


@dataclass
class DBEventDocument:
    """
    代表存储在数据库中的 Event 文档结构。
    此类主要用于在数据库服务层与实际存储交互时，确保数据格式的统一和正确性，
    特别是包含从上游（如协议对象）到数据库格式的转换逻辑。
    """

    _key: str  # event_id 将作为数据库文档的 _key
    event_id: str  # 事件的唯一ID
    event_type: str  # 事件类型，例如 "message.group.normal"
    timestamp: int  # 事件发生的时间戳 (毫秒, UTC)
    platform: str  # 事件发生的平台，例如 "qq", "discord"
    bot_id: str  # 处理此事件的机器人ID
    content: list[dict[str, Any]]  # 事件内容，通常是Seg对象的字典列表

    # 以下字段是可选的，取决于事件类型和协议定义
    user_info: dict[str, Any] | None = None  # 发起事件的用户信息 (UserInfo对象的字典表示)
    conversation_info: dict[str, Any] | None = None  # 事件发生的会话信息 (ConversationInfo对象的字典表示)
    raw_data: dict[str, Any] | None = None  # 来自适配器的原始事件数据，用于调试或特殊处理
    protocol_version: str = "1.4.0"  # 事件数据所遵循的协议版本号

    # 为便于数据库查询而从 user_info 和 conversation_info 中提取的关键ID
    user_id_extracted: str | None = None  # 提取出的用户ID
    conversation_id_extracted: str | None = None  # 提取出的会话ID
    motivation: str | None = None  # 新增：用于存储事件的动机，特别是机器人发出的消息事件
    status: str = "unread"  # 新增：事件的读取状态，默认为"unread"

    @classmethod
    def from_protocol(cls, proto_event: ProtocolEvent) -> "DBEventDocument":
        """
        从 `aicarus_protocols.Event` 对象创建一个 `DBEventDocument` 实例，
        用于准备存入数据库的数据。
        """
        if not isinstance(proto_event, ProtocolEvent):  # 基本的类型检查
            logger.error(
                f"传递给 DBEventDocument.from_protocol 的对象类型错误，期望 ProtocolEvent，得到 {type(proto_event)}"
            )
            # 实际应用中应抛出更具体的异常或有更完善的错误处理
            raise TypeError("输入对象必须是 aicarus_protocols.Event 的实例。")

        # 提取用于索引和查询的 user_id 和 conversation_id
        uid_ext = (
            str(proto_event.user_info.user_id) if proto_event.user_info and proto_event.user_info.user_id else None
        )
        cid_ext = (
            str(proto_event.conversation_info.conversation_id)
            if proto_event.conversation_info and proto_event.conversation_info.conversation_id
            else None
        )

        # 将 content (Seg对象列表) 转换为字典列表
        content_as_dicts: list[dict[str, Any]] = []
        if proto_event.content:  # proto_event.content 应该是 List[ProtocolSeg]
            for seg_obj in proto_event.content:
                if hasattr(seg_obj, "to_dict") and callable(seg_obj.to_dict):  # 优先调用对象的to_dict方法
                    content_as_dicts.append(seg_obj.to_dict())
                elif isinstance(seg_obj, dict):  # 如果已经是字典 (例如来自旧数据或已转换)
                    content_as_dicts.append(seg_obj)
                else:  # 记录无法处理的内容项
                    logger.warning(
                        f"事件 '{proto_event.event_id}' 的内容列表中包含无法转换为字典的项目: {type(seg_obj)}。已跳过。"
                    )

        # 将 UserInfo, ConversationInfo, RawData (如果它们是对象) 转换为字典
        user_info_dict = None
        if proto_event.user_info:  # proto_event.user_info 是 ProtocolUserInfo 类型
            if hasattr(proto_event.user_info, "to_dict") and callable(proto_event.user_info.to_dict):
                user_info_dict = proto_event.user_info.to_dict()
            elif isinstance(proto_event.user_info, dict):  # 兼容已经是字典的情况
                user_info_dict = proto_event.user_info
            else:
                logger.warning(
                    f"事件 '{proto_event.event_id}' 的 user_info 无法转换为字典: {type(proto_event.user_info)}。"
                )

        conversation_info_dict = None
        if proto_event.conversation_info:  # proto_event.conversation_info 是 ProtocolConversationInfo 类型
            if hasattr(proto_event.conversation_info, "to_dict") and callable(proto_event.conversation_info.to_dict):
                conversation_info_dict = proto_event.conversation_info.to_dict()
            elif isinstance(proto_event.conversation_info, dict):
                conversation_info_dict = proto_event.conversation_info
            else:
                logger.warning(
                    f"事件 '{proto_event.event_id}' 的 conversation_info 无法转换为字典: {type(proto_event.conversation_info)}。"
                )

        raw_data_dict = None
        if proto_event.raw_data:  # proto_event.raw_data 类型未知，可能是dict或自定义对象
            if hasattr(proto_event.raw_data, "to_dict") and callable(proto_event.raw_data.to_dict):
                raw_data_dict = proto_event.raw_data.to_dict()
            elif isinstance(proto_event.raw_data, dict):
                raw_data_dict = proto_event.raw_data
            else:  # 如果是其他类型，尝试转为字符串存入一个标准键下，避免直接存储复杂对象
                raw_data_dict = {"_raw_content_as_string_": str(proto_event.raw_data)}

        return cls(
            _key=str(proto_event.event_id),  # 确保 _key 是字符串
            event_id=str(proto_event.event_id),
            event_type=str(proto_event.event_type),
            timestamp=int(proto_event.time),  # 协议中的 time 字段是毫秒时间戳 (int or float)
            platform=str(proto_event.platform),
            bot_id=str(proto_event.bot_id),
            content=content_as_dicts,  # 存储转换后的字典列表
            user_info=user_info_dict,
            conversation_info=conversation_info_dict,
            raw_data=raw_data_dict,
            protocol_version=getattr(proto_event, "protocol_version", "1.4.0"),  # 安全获取，万一协议对象没有此字段
            user_id_extracted=uid_ext,
            conversation_id_extracted=cid_ext,
            motivation=getattr(proto_event, "motivation", None),  # 新增：安全获取motivation
        )

    def to_dict(self) -> dict[str, Any]:
        """将 DBEventDocument 实例转换为适合存入数据库的字典。"""
        # dataclasses.asdict 对于包含Optional字段的dataclass是安全的
        # (None值会被包含在字典中，ArangoDB通常能正确处理null值)
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
