# src/common/focus_chat_history_builder/chat_history_formatter.py
from datetime import datetime
from typing import TYPE_CHECKING, Any

from aicarus_protocols import Seg, extract_text_from_content
from src.common.custom_logging.logging_config import get_logger
from src.config import config
from src.focus_chat_mode.components import PromptComponents

if TYPE_CHECKING:
    from src.database.services.event_storage_service import EventStorageService

logger = get_logger(__name__)

VISUAL_VIEWPORT_SIZE = 20


class _ChatHistoryFormatter:
    """一个内部辅助类，封装了单次聊天记录格式化的所有状态和逻辑。它直接处理从数据库获取的原生字典."""

    def __init__(
        self,
        session_events: list[dict],
        bot_profile: dict,
        conversation_type: str,
        last_processed_timestamp: float,
        is_first_turn: bool,
    ) -> None:
        self.events = session_events
        self.bot_profile = bot_profile
        self.conversation_type = conversation_type
        self.last_processed_timestamp = last_processed_timestamp
        self.is_first_turn = is_first_turn

        # 初始化将要构建的组件
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

        for event in self.events:
            user_info = event.get("user_info")
            if (
                isinstance(user_info, dict)
                and (p_user_id := user_info.get("user_id"))
                and p_user_id not in self.platform_id_to_uid_str
            ):
                uid_counter += 1
                uid_str = f"U{uid_counter}"
                self.platform_id_to_uid_str[p_user_id] = uid_str
                remark = user_info.get("extra", {}).get("friend_remark")
                self.user_map[p_user_id] = {
                    "uid_str": uid_str,
                    "nick": user_info.get("user_nickname") or f"用户{p_user_id[:4]}",
                    "card": (
                        remark or user_info.get("user_cardname") or user_info.get("user_nickname")
                    ),
                    "title": user_info.get("user_titlename") or "",
                    "perm": user_info.get("permission_level") or "成员",
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
        added_platform_message_ids: set[str] = set()
        total_events = len(self.events)

        for i, event in enumerate(self.events):
            is_in_viewport = (total_events - 1 - i) < VISUAL_VIEWPORT_SIZE

            if (
                not self.is_first_turn
                and event.get("timestamp", 0) > self.last_processed_timestamp
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

            log_line = self._format_single_log_entry(
                event, is_in_viewport, added_platform_message_ids
            )
            if log_line:
                log_lines.append(log_line)

        if not self.is_first_turn and not unread_section_started and log_lines:
            if self.events:
                last_event_time = self.events[-1].get("timestamp", 0)
            else:
                last_event_time = self.last_processed_timestamp
            read_marker_time = datetime.fromtimestamp(last_event_time / 1000.0)
            log_lines.append(
                f"--- 以上消息是你已经思考过的内容，已读 "
                f"(标记时间: {read_marker_time.strftime('%H:%M:%S')}) ---"
            )

        return "\n".join(log_lines) or "当前没有聊天记录。"

    def _format_single_log_entry(
        self, event: dict, is_in_viewport: bool, added_ids: set[str]
    ) -> str | None:
        """格式化单条事件字典为一行日志字符串."""
        event_type = event.get("event_type", "unknown")
        time_str = datetime.fromtimestamp(event.get("timestamp", 0) / 1000.0).strftime("%H:%M:%S")
        sender_uid = "SYS"
        user_info = event.get("user_info")
        if isinstance(user_info, dict) and (user_id := user_info.get("user_id")):
            sender_uid = self.platform_id_to_uid_str.get(
                str(user_id), f"Unknown({str(user_id)[:4]})"
            )

        if event_type.startswith("message."):
            return self._format_message_entry(
                event, sender_uid, time_str, is_in_viewport, added_ids
            )
        if event_type.startswith("notice."):
            return self._format_notice_entry(event, time_str)
        if event_type == "internal.focus_chat_mode.thought_log":
            content_segs = [
                Seg.from_dict(c) for c in (event.get("content") or []) if isinstance(c, dict)
            ]
            motivation = extract_text_from_content(content_segs)
            return f"[{time_str}] {sender_uid} [MOTIVE]: {motivation}"

        content_segs = [
            Seg.from_dict(c) for c in (event.get("content") or []) if isinstance(c, dict)
        ]
        content_preview = extract_text_from_content(content_segs)
        event_type_display = event_type.split(".")[-1].upper()
        return (
            f"[{time_str}] {sender_uid} [{event_type_display}]: "
            f"{content_preview[:30]}{'...' if len(content_preview) > 30 else ''} "
            f"(id:{event.get('event_id')})"
        )

    def _format_message_entry(
        self, event: dict, sender_uid: str, time_str: str, is_in_viewport: bool, added_ids: set[str]
    ) -> str | None:
        """格式化消息类型事件字典."""
        content = event.get("content", [])
        msg_id = None
        if content and isinstance(content, list):
            for seg in content:
                if seg.get("type") == "message_metadata":
                    msg_id = seg.get("data", {}).get("message_id")
                    break

        if msg_id:
            if msg_id in added_ids:
                return None
            added_ids.add(msg_id)

        content_segs = [Seg.from_dict(c) for c in content if isinstance(c, dict)]
        content_parts, content_type, quote_str = [], "MSG", ""
        image_analysis_index = 0

        for seg in content_segs:
            if seg.type == "text":
                content_parts.append(seg.data.get("text", ""))
            elif seg.type == "image":
                content_parts.append(
                    self._format_image_segment(seg, is_in_viewport, event, image_analysis_index)
                )
                image_analysis_index += 1
            elif seg.type == "video":
                content_parts.append(self._format_video_segment(seg, is_in_viewport))
            elif seg.type == "quote":
                quote_str = self._format_quote_segment(seg)
            elif seg.type == "at":
                content_parts.append(self._format_at_segment(seg))
            elif seg.type == "face":
                content_parts.append(f"[表情:{seg.data.get('id', '未知')}]")
            elif seg.type == "file":
                content_type = "FILE"
                content_parts.append(
                    f"[FILE:{seg.data.get('name', '未知')} ({seg.data.get('size', 0)} bytes)]"
                )

        main_content = "".join(content_parts).strip()
        if text_only := extract_text_from_content(content_segs):
            self.last_valid_text_message = text_only

        display_tag = f"{content_type}{', ' + quote_str if quote_str else ''}"
        event_identifier = msg_id or event.get("event_id")
        log_line = (
            f"[{time_str}] {sender_uid} [{display_tag}]: {main_content} (id:{event_identifier})"
        )

        if sender_uid == "U0" and (motivation := event.get("motivation")):
            log_line += f"\n    - [MOTIVE]: {motivation}"

        return log_line

    def _format_image_segment(
        self, seg: Seg, is_in_viewport: bool, event: dict, analysis_index: int
    ) -> str:
        """格式化图片消息段."""
        self.image_ref_counter += 1
        is_sticker = seg.data.get("summary") == "sticker"
        placeholder = f"[{'动画表情' if is_sticker else '图片'}_{self.image_ref_counter}]"

        # 只要是图片，就提取它的数据，确保占位符和数据列表一致
        if base64_data := seg.data.get("base64"):
            mime_type = seg.data.get("mime_type", "image/jpeg")
            self.image_references_for_llm.append(f"data:{mime_type};base64,{base64_data}")
        elif url := seg.data.get("url"):
            self.image_references_for_llm.append(url)

        # 对于不在可视范围内的旧图片，如果已经有分析结果，则使用分析结果作为文本提示
        if not is_in_viewport:
            analysis_list = event.get("image_analysis", [])
            if isinstance(analysis_list, list) and analysis_index < len(analysis_list):
                analysis_item = analysis_list[analysis_index]
                desc_text = analysis_item.get("details", {}).get("description", "图片")
                prefix = "表情包" if analysis_item.get("type") == "sticker" else "图片"
                # 返回分析文本，而不是占位符
                return f"[{prefix}: {desc_text}]"

        # 对于可视范围内的图片，或没有分析结果的旧图片，返回占位符
        return placeholder

    def _format_video_segment(self, seg: Seg, is_in_viewport: bool) -> str:
        """格式化视频/GIF消息段."""
        if is_in_viewport:
            self.image_ref_counter += 1
            if base64_data := seg.data.get("base64"):
                mime_type = seg.data.get("mime_type", "video/mp4")
                self.image_references_for_llm.append(f"data:{mime_type};base64,{base64_data}")
            return f"[GIF_{self.image_ref_counter}]"
        return "[GIF]"

    def _format_quote_segment(self, seg: Seg) -> str:
        """格式化引用消息段."""
        msg_id = seg.data.get("message_id", "unknown")
        user_id = seg.data.get("user_id")
        if user_id:
            user_uid = self.platform_id_to_uid_str.get(str(user_id), f"未知({str(user_id)[:4]})")
        else:
            user_uid = "未知用户"
        return f"引用/回复 {user_uid}(id:{msg_id})"

    def _format_at_segment(self, seg: Seg) -> str:
        """格式化@消息段."""
        at_user_id = seg.data.get("user_id")
        at_display_name = seg.data.get("display_name")
        if at_user_id and at_user_id in self.platform_id_to_uid_str:
            return f"@{self.platform_id_to_uid_str[at_user_id]} "
        return f"@{at_display_name or at_user_id or '未知'} "

    def _format_notice_entry(self, event: dict, time_str: str) -> str:
        """格式化通知类型事件."""
        content_segs = [
            Seg.from_dict(c) for c in (event.get("content") or []) if isinstance(c, dict)
        ]
        notice_data = content_segs[0].data if content_segs else {}
        notice_subtype = event.get("event_type", "").split(".")[-1]

        operator_id = notice_data.get("operator_user_info", {}).get("user_id")
        operator_uid = (
            self.platform_id_to_uid_str.get(str(operator_id), "系统") if operator_id else "系统"
        )

        user_info = event.get("user_info", {})
        target_id = user_info.get("user_id") if isinstance(user_info, dict) else None
        target_uid = (
            self.platform_id_to_uid_str.get(str(target_id), "某人") if target_id else "某人"
        )

        content = f"收到一条 {notice_subtype} 类型的平台通知。"
        if notice_subtype == "member_increase":
            if notice_data.get("join_type") != "approve":
                content = f"{operator_uid} 邀请 {target_uid} 加入了群聊。"
            else:
                content = f"{target_uid} 加入了群聊。"
        elif notice_subtype == "member_decrease":
            if notice_data.get("leave_type") == "kick":
                content = f"{operator_uid} 将 {target_uid} 移出了群聊。"
            else:
                content = f"{target_uid} 退出了群聊。"
        elif notice_subtype == "recalled":
            content = f"{operator_uid} 撤回了一条消息。"

        return f"[{time_str}] [NOTICE]: {content}"


async def _fetch_and_prepare_events(
    event_storage: "EventStorageService",
    conversation_id: str,
    raw_events_from_caller: list[dict] | None,
) -> list[dict]:
    """获取、去重并正确排序事件字典列表."""
    event_dicts = raw_events_from_caller
    if event_dicts is None:
        # 数据库返回的事件是按时间戳倒序（从新到旧）
        event_dicts = await event_storage.get_recent_chat_message_documents(
            conversation_id=conversation_id, limit=50, fetch_all_event_types=True
        )

    if not event_dicts:
        return []

    # 去重逻辑：因为列表已经是“从新到旧”，所以我们遇到的第一个就是最新的，直接保留。
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

    # 将去重后的事件（仍然是从新到旧）提取出来
    final_events_desc = list(unique_events_desc.values())

    # 直接反转列表，得到最终正确的“从旧到新”的顺序
    final_events_asc = final_events_desc[::-1]

    return final_events_asc


async def format_chat_history_for_llm(
    event_storage: "EventStorageService",
    conversation_id: str,
    bot_profile: dict,
    conversation_type: str,
    conversation_name: str | None,
    last_processed_timestamp: float,
    is_first_turn: bool,
    raw_events_from_caller: list[dict[str, Any]] | None = None,
) -> tuple[PromptComponents, list[dict]]:
    """通用的聊天记录格式化工具（Orchestrator）.

    它编排一系列辅助方法来获取、处理和格式化聊天记录及相关元数据。
    """
    # 1. 获取、去重并排序事件字典
    prepared_events = await _fetch_and_prepare_events(
        event_storage, conversation_id, raw_events_from_caller
    )

    # 2. 初始化辅助类，它将处理所有复杂的格式化逻辑
    formatter = _ChatHistoryFormatter(
        session_events=prepared_events,
        bot_profile=bot_profile,
        conversation_type=conversation_type,
        last_processed_timestamp=last_processed_timestamp,
        is_first_turn=is_first_turn,
    )

    # 3. 生成各个文本块
    final_conversation_name = conversation_name or "未知会话"
    if prepared_events:
        for event in reversed(prepared_events):
            conv_info = event.get("conversation_info")
            if isinstance(conv_info, dict) and (name := conv_info.get("name")):
                final_conversation_name = name
                break

    conversation_info_block = (
        f'- conversation_name: "{final_conversation_name}"\n'
        f'- conversation_type: "{conversation_type}"'
    )
    user_list_block = formatter.format_user_list_block()
    chat_history_log_block = formatter.format_chat_log_block()

    # 4. 收集需要标记为已读的事件ID
    processed_event_ids = [
        event.get("event_id")
        for event in prepared_events
        if event.get("event_type", "").startswith("message.")
        and event.get("timestamp", 0) > last_processed_timestamp
    ]

    # 5. 组装并返回最终结果
    components = PromptComponents(
        chat_history_log_block=chat_history_log_block,
        user_list_block=user_list_block,
        conversation_info_block=conversation_info_block,
        user_map=formatter.user_map,
        uid_str_to_platform_id_map={
            uid: pid for pid, uid in formatter.platform_id_to_uid_str.items()
        },
        processed_event_ids=processed_event_ids,
        image_references=formatter.image_references_for_llm,
        conversation_name=final_conversation_name,
        last_valid_text_message=formatter.last_valid_text_message,
    )

    return components, prepared_events
