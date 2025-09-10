# src/domain/models.py
from dataclasses import dataclass, field
from typing import Any

from aicarus_protocols import Event as ProtocolEvent


@dataclass(frozen=True)
class Stimulus:
    """刺激物 - 领域模型.

    代表一个进入核心并可能触发思考的外部或内部事件的纯净表示.
    它从原始的 ProtocolEvent 中提取了核心业务所需的最本质信息，并与协议完全解耦.
    这是内部逻辑处理信息的主要输入形式.
    """

    event_id: str
    timestamp: int
    platform: str
    bot_id: str
    text_content: str
    image_urls: list[str] = field(default_factory=list)

    # 上下文信息
    conversation_id: str | None = None
    conversation_type: str | None = None
    conversation_name: str | None = None

    sender_id: str | None = None
    sender_nickname: str | None = None
    sender_cardname: str | None = None

    # 供格式化器等特殊模块使用的附加信息
    raw_event_type: str | None = None
    raw_content: list[dict[str, Any]] = field(default_factory=list)
    image_analysis: list[dict[str, Any]] | None = None
    motivation: str | None = None  # 用于记录 AI 自身发言的动机
    embedding: list[float] | None = field(default=None, repr=False)
    narrative_sentence: str | None = None

    @classmethod
    def from_protocol_event(cls, event: ProtocolEvent) -> "Stimulus":
        """从通信协议的 Event 对象转换为内部领域的 Stimulus 对象.

        这是“神圣边界”上的翻译官.
        """
        image_urls = [
            seg.data.get("url", "")
            for seg in event.content
            if seg.type == "image" and seg.data.get("url")
        ]

        return cls(
            event_id=event.event_id,
            timestamp=event.time,
            platform=event.get_platform() or "unknown",
            bot_id=event.bot_id,
            text_content=event.get_text_content(),
            image_urls=image_urls,
            conversation_id=event.conversation_info.conversation_id
            if event.conversation_info
            else None,
            conversation_type=event.conversation_info.type if event.conversation_info else None,
            conversation_name=event.conversation_info.name if event.conversation_info else None,
            sender_id=event.user_info.user_id if event.user_info else None,
            sender_nickname=event.user_info.user_nickname if event.user_info else None,
            sender_cardname=event.user_info.user_cardname if event.user_info else None,
            raw_event_type=event.event_type,
            raw_content=[seg.to_dict() for seg in event.content],
        )

    @classmethod
    def from_db_document(cls, doc: dict) -> "Stimulus":
        """从【历史】的数据库事件文档创建 Stimulus，包含更丰富的渲染信息."""
        # 创建一个可变副本以避免副作用
        doc_for_protocol = doc.copy()

        # --- 兼容性修复区域开始 ---

        # 1. 兼容旧的 'timestamp' 字段
        if "time" not in doc_for_protocol and "timestamp" in doc_for_protocol:
            doc_for_protocol["time"] = doc_for_protocol["timestamp"]

        # 2. 为缺失的 'bot_id' 提供一个明确的默认值
        if "bot_id" not in doc_for_protocol:
            # 从其他字段推断，或使用一个安全的未知值
            # 假设旧事件的 bot_id 可以从 platform 字段推断或默认为 'unknown_bot'
            platform = doc_for_protocol.get("platform", "unknown")
            doc_for_protocol["bot_id"] = f"unknown_bot_on_{platform}"

        # 3. 为缺失的 'content' 提供一个空的列表作为默认值
        if "content" not in doc_for_protocol:
            doc_for_protocol["content"] = []

        # --- 兼容性修复区域结束 ---

        proto_event = ProtocolEvent.from_dict(doc_for_protocol)
        stimulus = cls.from_protocol_event(proto_event)

        # 使用 object.__setattr__ 来修改 frozen dataclass 的字段
        object.__setattr__(stimulus, "image_analysis", doc.get("image_analysis"))
        object.__setattr__(stimulus, "motivation", doc.get("motivation"))

        return stimulus


@dataclass(frozen=True)
class ActionMetadata:
    """动作元数据 - 领域模型.

    用于封装一个动作背后的、纯粹属于 Core 内部业务逻辑的上下文信息.
    例如“为什么”要执行这个动作。这些信息绝不应该污染通信协议.
    """

    motivation: str
    source_event_id: str | None = None
    source_thought_id: str | None = None


@dataclass(frozen=True)
class InternalActionRequest:
    """内部动作请求 - 领域模型.

    这是 Core 内部业务逻辑在决定执行一个动作后，生成的最终请求对象.
    它清晰地分离了需要对外发送的“信件”(action_event)和仅供内部记录和理解的“思考过程”(metadata).
    这个对象将被传递给 ActionHandler 或类似的分发器进行处理.
    """

    action_event: ProtocolEvent
    metadata: ActionMetadata


@dataclass(frozen=True)
class ActionResult:
    """动作结果 - 领域模型.

    代表一个外部动作执行完成后的纯净结果.
    它从 action_response event 中提取了核心信息，供内部逻辑使用,
    而无需关心 action_response event 本身的复杂结构.
    """

    action_id: str
    is_success: bool
    payload: Any | None = None
    error_message: str | None = None
