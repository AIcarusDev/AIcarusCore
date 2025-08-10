# src/common/focus_chat_history_builder/chat_history_formatter.py
from datetime import datetime
from typing import TYPE_CHECKING, Any

from aicarus_protocols import Event as ProtocolEvent
from src.common.custom_logging.logging_config import get_logger
from src.config import config

# ======================== [ 新增导入 ] ========================
from src.domain.models import Stimulus

# =============================================================
from src.focus_chat_mode.components import PromptComponents

if TYPE_CHECKING:
    from src.database.services.event_storage_service import EventStorageService

logger = get_logger(__name__)

VISUAL_VIEWPORT_SIZE = 20


class _ChatHistoryFormatter:
    """内部辅助类，封装了单次聊天记录格式化的所有状态和逻辑.

    它现在直接处理 Stimulus 领域模型对象.
    """

    def __init__(
        self,
        session_stimuli: list[Stimulus],
        bot_profile: dict,
        conversation_type: str,
        last_processed_timestamp: float,
        is_first_turn: bool,
    ) -> None:
        self.stimuli = session_stimuli
        self.bot_profile = bot_profile
        self.conversation_type = conversation_type
        self.last_processed_timestamp = last_processed_timestamp
        self.is_first_turn = is_first_turn

        self.user_map: dict[str, dict[str, Any]] = {}
        self.platform_id_to_uid_str: dict[str, str] = {}
        self.image_references_for_llm: list[str] = []
        self.last_valid_text_message: str | None = None
        self.image_ref_counter = 0

        self._build_user_maps()

    def _build_user_maps(self) -> None:
        """构建用户ID到UID的映射表."""
        uid_counter = 0
        final_bot_id = str(self.bot_profile.get("user_id"))
        self.platform_id_to_uid_str[final_bot_id] = "U0"
        self.user_map[final_bot_id] = {
            "uid_str": "U0",
            "nick": self.bot_profile.get("nickname", config.persona.bot_name or "bot"),
            "card": self.bot_profile.get("card", self.bot_profile.get("nickname")),
            "title": self.bot_profile.get("title", ""),
            "perm": self.bot_profile.get("role", "成员"),
        }

        for stimulus in self.stimuli:
            if stimulus.sender_id and stimulus.sender_id not in self.platform_id_to_uid_str:
                uid_counter += 1
                uid_str = f"U{uid_counter}"
                self.platform_id_to_uid_str[stimulus.sender_id] = uid_str
                self.user_map[stimulus.sender_id] = {
                    "uid_str": uid_str,
                    "nick": stimulus.sender_nickname or f"用户{stimulus.sender_id[:4]}",
                    "card": stimulus.sender_cardname or stimulus.sender_nickname,
                    "title": "",  # Stimulus 目前不包含 title 信息，可后续添加
                    "perm": "成员",  # Stimulus 目前不包含权限信息
                }

    def format_user_list_block(self) -> str:
        """格式化用户列表文本块."""
        lines = []
        sorted_ids = sorted(
            self.user_map.keys(), key=lambda pid: int(self.user_map[pid]["uid_str"][1:])
        )
        for p_id in sorted_ids:
            user_data = self.user_map[p_id]
            suffix = "（你）" if user_data["uid_str"] == "U0" else ""
            if self.conversation_type == "private":
                line = (
                    f"{user_data['uid_str']}: {p_id}{suffix} [nick:{user_data['nick']}, "
                    f"card:{user_data['card']}]"
                )
            else:
                title_part = f"title:{user_data['title']}, " if user_data["title"] else ""
                line = (
                    f"{user_data['uid_str']}: {p_id}{suffix} [nick:{user_data['nick']}, "
                    f"card:{user_data['card']}, {title_part}perm:{user_data['perm']}]"
                )
            lines.append(line)
        return "\n".join(lines)

    def format_chat_log_block(self) -> str:
        """核心逻辑：格式化完整的聊天记录文本块."""
        log_lines: list[str] = []
        unread_section_started = False
        total_stimuli = len(self.stimuli)

        for i, stimulus in enumerate(self.stimuli):
            is_in_viewport = (total_stimuli - 1 - i) < VISUAL_VIEWPORT_SIZE

            if (
                not self.is_first_turn
                and stimulus.timestamp > self.last_processed_timestamp
                and not unread_section_started
            ):
                if log_lines:
                    read_marker_time = datetime.fromtimestamp(
                        self.last_processed_timestamp / 1000.0
                    )
                    log_lines.append(
                        f"--- 以上消息是你已经思考过的内容，已读 "
                        f"(标记时间: {read_marker_time.strftime('%H:%M:%S')}) ---"
                    )
                log_lines.append("--- 请关注以下未读的新消息---")
                unread_section_started = True

            log_line = self._format_single_log_entry(stimulus, is_in_viewport)
            if log_line:
                log_lines.append(log_line)

        if not self.is_first_turn and not unread_section_started and log_lines:
            last_event_time = (
                self.stimuli[-1].timestamp if self.stimuli else self.last_processed_timestamp
            )
            read_marker_time = datetime.fromtimestamp(last_event_time / 1000.0)
            log_lines.append(
                f"--- 以上消息是你已经思考过的内容，已读 "
                f"(标记时间: {read_marker_time.strftime('%H:%M:%S')}) ---"
            )

        return "\n".join(log_lines) or "当前没有聊天记录。"

    def _format_single_log_entry(self, stimulus: Stimulus, is_in_viewport: bool) -> str | None:
        """格式化单条 Stimulus 为一行日志字符串."""
        event_type = stimulus.raw_event_type or "unknown"
        time_str = datetime.fromtimestamp(stimulus.timestamp / 1000.0).strftime("%H:%M:%S")
        sender_uid = "SYS"
        if stimulus.sender_id:
            sender_uid = self.platform_id_to_uid_str.get(
                stimulus.sender_id, f"Unknown({stimulus.sender_id[:4]})"
            )

        if event_type.startswith("message."):
            return self._format_message_entry(stimulus, sender_uid, time_str, is_in_viewport)

        # 简化处理，未来可以扩展
        event_type_display = event_type.split(".")[-1].upper()
        return (
            f"[{time_str}] {sender_uid} [{event_type_display}]: "
            f"{stimulus.text_content[:30]}... (id:{stimulus.event_id})"
        )

    def _format_message_entry(
        self, stimulus: Stimulus, sender_uid: str, time_str: str, is_in_viewport: bool
    ) -> str | None:
        """格式化消息类型的 Stimulus."""
        # 注意：这里的实现简化了，直接使用 stimulus.text_content。
        # 一个更完整的实现会重新解析 ProtocolEvent 的 content 列表来处理图片、at等。
        # 但为了演示核心思想，我们先用纯文本。
        content_type = "MSG"
        main_content = stimulus.text_content
        if stimulus.image_urls:
            image_placeholders = []
            for url in stimulus.image_urls:
                self.image_ref_counter += 1
                image_placeholders.append(f"[图片_{self.image_ref_counter}]")
                if is_in_viewport:
                    self.image_references_for_llm.append(url)
            main_content += " " + " ".join(image_placeholders)

        if main_content:
            self.last_valid_text_message = main_content

        log_line = (
            f"[{time_str}] {sender_uid} [{content_type}]: "
            f"{main_content.strip()} (id:{stimulus.event_id})"
        )
        return log_line


# ======================== [ 核心改造点 ] ========================
async def _fetch_and_prepare_stimuli(
    event_storage: "EventStorageService",
    conversation_id: str,
) -> list[Stimulus]:
    """获取、去重、排序事件字典，并最终转换为 Stimulus 领域模型列表."""
    event_dicts = await event_storage.get_recent_chat_message_documents(
        conversation_id=conversation_id, limit=50, fetch_all_event_types=True
    )
    if not event_dicts:
        return []

    unique_events_desc: dict[str, dict] = {}
    for event in event_dicts:
        content = event.get("content", [])
        msg_id = None
        if content and isinstance(content, list):
            for seg in content:
                if seg.get("type") == "message_metadata":
                    msg_id = seg.get("data", {}).get("message_id")
                    break
        key = f"msg_{msg_id}" if msg_id else f"core_{event.get('event_id')}"
        if key not in unique_events_desc:
            unique_events_desc[key] = event

    final_events_desc = list(unique_events_desc.values())
    final_events_asc = final_events_desc[::-1]

    # 在这里完成从数据库字典 -> ProtocolEvent -> Stimulus 的转换
    stimuli = [
        Stimulus.from_protocol_event(ProtocolEvent.from_dict(doc)) for doc in final_events_asc
    ]
    return stimuli


async def format_chat_history_for_llm(
    event_storage: "EventStorageService",
    conversation_id: str,
    bot_profile: dict,
    conversation_type: str,
    conversation_name: str | None,
    last_processed_timestamp: float,
    is_first_turn: bool,
) -> tuple[PromptComponents, list[Stimulus]]:
    """通用的聊天记录格式化工具，现在返回 Stimulus 列表."""
    prepared_stimuli = await _fetch_and_prepare_stimuli(event_storage, conversation_id)

    formatter = _ChatHistoryFormatter(
        session_stimuli=prepared_stimuli,
        bot_profile=bot_profile,
        conversation_type=conversation_type,
        last_processed_timestamp=last_processed_timestamp,
        is_first_turn=is_first_turn,
    )

    final_conversation_name = conversation_name or "未知会话"
    if prepared_stimuli:
        for stimulus in reversed(prepared_stimuli):
            if stimulus.conversation_name:
                final_conversation_name = stimulus.conversation_name
                break

    conversation_info_block = (
        f'- conversation_name: "{final_conversation_name}"\n'
        f'- conversation_type: "{conversation_type}"'
    )
    user_list_block = formatter.format_user_list_block()
    chat_history_log_block = formatter.format_chat_log_block()

    processed_stimuli = [
        s
        for s in prepared_stimuli
        if s.raw_event_type
        and s.raw_event_type.startswith("message.")
        and s.timestamp > last_processed_timestamp
    ]

    components = PromptComponents(
        chat_history_log_block=chat_history_log_block,
        user_list_block=user_list_block,
        conversation_info_block=conversation_info_block,
        user_map=formatter.user_map,
        uid_str_to_platform_id_map={
            uid: pid for pid, uid in formatter.platform_id_to_uid_str.items()
        },
        processed_event_ids=[s.event_id for s in processed_stimuli],
        image_references=formatter.image_references_for_llm,
        conversation_name=final_conversation_name,
        last_valid_text_message=formatter.last_valid_text_message,
    )
    return components, processed_stimuli


# =============================================================
