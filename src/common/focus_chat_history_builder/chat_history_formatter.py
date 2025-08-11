# src/common/focus_chat_history_builder/chat_history_formatter.py
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from aicarus_protocols import Seg, extract_text_from_content
from src.common.custom_logging.logging_config import get_logger
from src.config import config
from src.domain.models import Stimulus
from src.focus_chat_mode.components import PromptComponents

if TYPE_CHECKING:
    from src.database.services.event_storage_service import EventStorageService

logger = get_logger(__name__)

VISUAL_VIEWPORT_SIZE = 20


@dataclass
class ParsedContent:
    """一个用于存放消息段解析结果的数据容器."""

    content_parts: list[str] = field(default_factory=list)
    content_type: str = "MSG"
    quote_str: str = ""


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
                    "title": "",
                    "perm": "成员",
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

    def _is_in_viewport(self, index: int, total_stimuli: int) -> bool:
        """检查给定的索引是否在“可视窗口”内."""
        return (total_stimuli - 1 - index) < VISUAL_VIEWPORT_SIZE

    def _is_unread(self, stimulus: Stimulus) -> bool:
        """检查一个刺激物是否应被视为“未读”."""
        return not self.is_first_turn and stimulus.timestamp > self.last_processed_timestamp

    def format_chat_log_block(self) -> str:
        """核心逻辑：格式化完整的聊天记录文本块."""
        log_lines: list[str] = []
        unread_section_started = False
        added_platform_message_ids: set[str] = set()
        total_stimuli = len(self.stimuli)

        # 引入状态变量来追踪上一个显示的机器人动机
        last_displayed_bot_motive: str | None = None

        for i, stimulus in enumerate(self.stimuli):
            is_in_viewport = self._is_in_viewport(i, total_stimuli)

            if self._is_unread(stimulus) and not unread_section_started:
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

            # 将动机的格式化逻辑移出 _format_single_log_entry，在此处集中处理
            if log_line := self._format_single_log_entry(
                stimulus, is_in_viewport, added_platform_message_ids
            ):
                log_lines.append(log_line)

                sender_uid = self.platform_id_to_uid_str.get(stimulus.sender_id, "SYS")

                # 如果是机器人发言 (U0) 且有动机
                if sender_uid == "U0" and stimulus.motivation:
                    # 只有当动机与上一个不同时才显示
                    if stimulus.motivation != last_displayed_bot_motive:
                        log_lines.append(f"    - [MOTIVE]: {stimulus.motivation}")
                    # 无论是否显示，都更新“上一个动机”的状态
                    last_displayed_bot_motive = stimulus.motivation
                else:
                    # 如果发言者不是机器人，则重置追踪器，确保下次机器人的动机一定会被显示
                    last_displayed_bot_motive = None

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

    def _format_single_log_entry(
        self, stimulus: Stimulus, is_in_viewport: bool, added_ids: set[str]
    ) -> str | None:
        """[协调者] 格式化单条日志条目，并将任务分发给具体的处理器."""
        event_type = stimulus.raw_event_type or "unknown"
        time_str = datetime.fromtimestamp(stimulus.timestamp / 1000.0).strftime("%H:%M:%S")
        sender_uid = "SYS"
        if stimulus.sender_id:
            sender_uid = self.platform_id_to_uid_str.get(
                stimulus.sender_id, f"Unknown({stimulus.sender_id[:4]})"
            )

        if event_type.startswith("message."):
            return self._format_message_entry(
                stimulus, sender_uid, time_str, is_in_viewport, added_ids
            )
        if event_type.startswith("notice."):
            return self._format_notice_entry(stimulus, time_str)
        if event_type == "internal.focus_chat_mode.thought_log":
            return f"[{time_str}] {sender_uid} [MOTIVE]: {stimulus.text_content}"

        event_type_display = event_type.split(".")[-1].upper()
        return (
            f"[{time_str}] {sender_uid} [{event_type_display}]: {stimulus.text_content[:30]}... "
            f"(id:{stimulus.event_id})"
        )

    def _get_message_id_and_check_duplicate(
        self, content_segs: list[Seg], added_ids: set[str]
    ) -> tuple[str | None, bool]:
        """[辅助函数] 从消息段中提取 message_id 并检查是否重复."""
        msg_id = next(
            (seg.data.get("message_id") for seg in content_segs if seg.type == "message_metadata"),
            None,
        )
        if msg_id:
            if msg_id in added_ids:
                return msg_id, True  # 是重复的
            added_ids.add(msg_id)
        return msg_id, False  # 不是重复的

    def _parse_message_segments(
        self, content_segs: list[Seg], stimulus: Stimulus, is_in_viewport: bool
    ) -> ParsedContent:
        """[辅助函数] 遍历所有消息段并将其解析为结构化内容."""
        parsed = ParsedContent()
        image_analysis_index = 0

        for seg in content_segs:
            seg_type, seg_data = seg.type, seg.data
            if seg_type == "text":
                parsed.content_parts.append(seg_data.get("text", ""))
            elif seg_type == "image":
                parsed.content_parts.append(
                    self._format_image_segment(seg, is_in_viewport, stimulus, image_analysis_index)
                )
                image_analysis_index += 1
            elif seg_type == "video":
                parsed.content_parts.append(self._format_video_segment(seg, is_in_viewport))
            elif seg_type == "quote":
                parsed.quote_str = self._format_quote_segment(seg)
            elif seg_type == "at":
                parsed.content_parts.append(self._format_at_segment(seg))
            elif seg_type == "face":
                parsed.content_parts.append(f"[表情:{seg_data.get('id', '未知')}]")
            elif seg_type == "file":
                parsed.content_type = "FILE"
                parsed.content_parts.append(
                    f"[FILE:{seg_data.get('name', '未知')} ({seg_data.get('size', 0)} bytes)]"
                )
        return parsed

    def _assemble_log_line(
        self,
        parsed_content: ParsedContent,
        stimulus: Stimulus,
        sender_uid: str,
        time_str: str,
        msg_id: str | None,
    ) -> str:
        """[辅助函数] 将解析后的内容组装成最终的日志行字符串."""
        main_content = "".join(parsed_content.content_parts).strip()
        display_tag = (
            f"{parsed_content.content_type}"
            f"{', ' + parsed_content.quote_str if parsed_content.quote_str else ''}"
        )
        event_identifier = msg_id or stimulus.event_id
        log_line = (
            f"[{time_str}] {sender_uid} [{display_tag}]: {main_content} (id:{event_identifier})"
        )
        return log_line

    def _format_message_entry(
        self,
        stimulus: Stimulus,
        sender_uid: str,
        time_str: str,
        is_in_viewport: bool,
        added_ids: set[str],
    ) -> str | None:
        """[协调者] 格式化消息条目，调用辅助函数完成工作."""
        content_segs = [Seg.from_dict(c) for c in stimulus.raw_content]

        # 守卫子句: 处理消息ID并检查重复
        msg_id, is_duplicate = self._get_message_id_and_check_duplicate(content_segs, added_ids)
        if is_duplicate:
            return None

        # 步骤 1: 将所有消息段解析为结构化对象
        parsed = self._parse_message_segments(content_segs, stimulus, is_in_viewport)

        # 步骤 2: 更新最后一条有效文本消息 (副作用)
        if text_only := extract_text_from_content(content_segs):
            self.last_valid_text_message = text_only

        # 步骤 3: 组装最终的日志行
        return self._assemble_log_line(parsed, stimulus, sender_uid, time_str, msg_id)

    def _format_image_segment(
        self, seg: Seg, is_in_viewport: bool, stimulus: Stimulus, analysis_index: int
    ) -> str:
        """格式化图片消息段.

        - 无论何时，都准备好多模态数据。
        - 如果在可视窗口内，返回一个特殊的、带编号的占位符，并附上哈希值文本。
        - 如果在可视窗口外，返回已分析的文本描述，否则回退到带哈希的占位符。
        """
        # 步骤 1: 准备多模态数据和哈希文本
        base64_data = seg.data.get("base64")
        if isinstance(base64_data, str) and base64_data.strip():
            mime_type = seg.data.get("mime_type", "image/jpeg")
            self.image_references_for_llm.append(f"data:{mime_type};base64,{base64_data}")
        elif url := seg.data.get("url"):
            self.image_references_for_llm.append(url)

        self.image_ref_counter += 1
        is_sticker = seg.data.get("summary") == "sticker"
        image_hash = seg.data.get("hash")
        hash_str = f" (hash: {image_hash})" if image_hash else ""

        # 这个占位符文本是给 llmrequest 模块的 interleaver 识别并替换用的
        # 它本身不包含哈希，以确保正则表达式能精确匹配
        placeholder_for_injection = (
            f"[{'动画表情' if is_sticker else '图片'}_{self.image_ref_counter}]"
            )

        # 步骤 2: 根据是否在可视窗口内决定最终返回的字符串
        if is_in_viewport:
            # 在可视窗口内：返回“注入占位符” + “哈希文本”
            # LLM会看到图片，同时也能读到旁边的哈希值
            return f"{placeholder_for_injection}{hash_str}"
        else:
            # 在可视窗口外：优先使用已分析的文本描述
            if (
                stimulus.image_analysis
                and analysis_index < len(stimulus.image_analysis)
            ):
                analysis_item = stimulus.image_analysis[analysis_index]
                desc_text = analysis_item.get("details", {}).get("description", "图片")
                prefix = "表情包" if analysis_item.get("type") == "sticker" else "图片"
                # 注意：对于旧图片，我们只返回描述，不附加哈希，因为AI无法“看到”它来决定收藏
                return f"[{prefix}: {desc_text}]"
            else:
                # 如果没有分析结果，作为回退，仍然显示带哈希的占位符
                return f"{placeholder_for_injection}{hash_str}"

    def _format_video_segment(self, seg: Seg, is_in_viewport: bool) -> str:
        """格式化视频(动图)消息段."""
        # 步骤 1: 准备多模态数据
        if base64_data := seg.data.get("base64"):
            mime_type = seg.data.get("mime_type", "video/mp4")
            self.image_references_for_llm.append(f"data:{mime_type};base64,{base64_data}")

        # 步骤 2: 根据是否在可视窗口内决定返回的文本
        if is_in_viewport:
            # 在可视窗口内：返回一个特殊的占位符，让llmrequest注入视频
            # 注意：视频/动图没有哈希的概念，所以直接返回占位符即可
            self.image_ref_counter += 1
            return f"[GIF_{self.image_ref_counter}]"
        else:
            # 在可视窗口外：返回通用文本占位符
            return "[GIF]"

    def _format_quote_segment(self, seg: Seg) -> str:
        msg_id = seg.data.get("message_id", "unknown")
        user_id = seg.data.get("user_id")
        user_uid = (
            self.platform_id_to_uid_str.get(str(user_id), f"未知({str(user_id)[:4]})")
            if user_id
            else "未知用户"
        )
        return f"引用/回复 {user_uid}(id:{msg_id})"

    def _format_at_segment(self, seg: Seg) -> str:
        at_user_id = seg.data.get("user_id")
        at_display_name = seg.data.get("display_name")
        if at_user_id and at_user_id in self.platform_id_to_uid_str:
            return f"@{self.platform_id_to_uid_str[at_user_id]} "
        return f"@{at_display_name or at_user_id or '未知'} "

    def _format_notice_entry(self, stimulus: Stimulus, time_str: str) -> str:
        content_segs = [Seg.from_dict(c) for c in stimulus.raw_content]
        notice_data = content_segs[0].data if content_segs else {}
        notice_subtype = (stimulus.raw_event_type or "").split(".")[-1]

        operator_info = notice_data.get("operator_user_info") or {}
        operator_id = operator_info.get("user_id")
        operator_uid = (
            self.platform_id_to_uid_str.get(str(operator_id), "系统") if operator_id else "系统"
        )

        target_id = stimulus.sender_id
        target_uid = (
            self.platform_id_to_uid_str.get(str(target_id), "某人") if target_id else "某人"
        )

        content = f"收到一条 {notice_subtype} 类型的平台通知。"
        if notice_subtype == "member_increase":
            content = (
                f"{operator_uid} 邀请 {target_uid} 加入了群聊。"
                if notice_data.get("join_type") != "approve"
                else f"{target_uid} 加入了群聊。"
            )
        elif notice_subtype == "member_decrease":
            content = (
                f"{operator_uid} 将 {target_uid} 移出了群聊。"
                if notice_data.get("leave_type") == "kick"
                else f"{target_uid} 退出了群聊。"
            )
        elif notice_subtype == "recalled":
            content = f"{operator_uid} 撤回了一条消息。"

        return f"[{time_str}] [NOTICE]: {content}"


async def _fetch_and_prepare_stimuli(
    event_storage: "EventStorageService",
    conversation_id: str,
) -> list[Stimulus]:
    event_dicts = await event_storage.get_recent_chat_message_documents(
        conversation_id=conversation_id, limit=50, fetch_all_event_types=True
    )
    if not event_dicts:
        return []

    unique_events_desc: dict[str, dict] = {}
    for event in event_dicts:
        content = event.get("content", [])
        msg_id = None
        if isinstance(content, list):
            for seg in content:
                if seg.get("type") == "message_metadata":
                    msg_id = seg.get("data", {}).get("message_id")
                    break
        key = f"msg_{msg_id}" if msg_id else f"core_{event.get('event_id')}"
        if key not in unique_events_desc:
            unique_events_desc[key] = event

    final_events_asc = list(unique_events_desc.values())[::-1]

    return [Stimulus.from_db_document(doc) for doc in final_events_asc]


async def format_chat_history_for_llm(
    event_storage: "EventStorageService",
    conversation_id: str,
    bot_profile: dict,
    conversation_type: str,
    conversation_name: str | None,
    last_processed_timestamp: float,
    is_first_turn: bool,
) -> tuple[PromptComponents, list[Stimulus]]:
    """格式化聊天记录以供 LLM 使用."""
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
