import uuid
from collections import OrderedDict
from datetime import datetime
from typing import TYPE_CHECKING, Any

from aicarus_protocols.common import extract_text_from_content
from aicarus_protocols.conversation_info import ConversationInfo
from aicarus_protocols.event import Event
from aicarus_protocols.seg import Seg  # SegBuilder is not used here directly for now
from aicarus_protocols.user_info import UserInfo

from src.action.action_handler import ActionHandler
from src.common.custom_logging.logger_manager import get_logger
from src.config import config
from src.database.services.event_storage_service import EventStorageService
from src.tools.platform_actions import get_bot_profile

from . import prompt_templates

if TYPE_CHECKING:
    from .chat_session import ChatSession

logger = get_logger(__name__)


class ChatPromptBuilder:
    def __init__(
        self,
        session: "ChatSession",
        event_storage: EventStorageService,
        action_handler: ActionHandler,
        bot_id: str,
        platform: str,
        conversation_id: str,
        conversation_type: str,
    ) -> None:
        self.session = session
        self.event_storage: EventStorageService = event_storage
        self.action_handler: ActionHandler = action_handler
        self.bot_id: str = bot_id
        self.platform: str = platform
        self.conversation_id: str = conversation_id
        self.conversation_type: str = conversation_type
        logger.info(
            f"[ChatPromptBuilder][{self.conversation_id}] 实例已创建 for bot_id: {self.bot_id}, type: {self.conversation_type}."
        )

    async def build_prompts(
        self,
        session: "ChatSession",
        last_processed_timestamp: float,
        last_llm_decision: dict[str, Any] | None,
        sent_actions_context: OrderedDict[str, dict[str, Any]],
        is_first_turn: bool,  # 新增参数
        last_think_from_core: str | None = None,  # 新增参数
    ) -> tuple[str, str, dict[str, str], list[str]]:  # 修改返回类型，增加 List[str] for processed_event_ids
        # --- Step 1: Decide which templates to use ---
        user_nick = ""
        if self.conversation_type == "private":
            system_prompt_template = prompt_templates.PRIVATE_SYSTEM_PROMPT
            user_prompt_template = prompt_templates.PRIVATE_USER_PROMPT
        else:
            system_prompt_template = prompt_templates.GROUP_SYSTEM_PROMPT
            user_prompt_template = prompt_templates.GROUP_USER_PROMPT

        # --- Step 2: Prepare common data ---
        current_time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        persona_config = config.persona
        bot_name_str = persona_config.bot_name or "AI"
        bot_description_str = f"\n{persona_config.description}" if persona_config.description else ""
        bot_profile_str = f"\n{persona_config.profile}" if persona_config.profile else ""

        # 根据无动作计数器，准备引导提示
        no_action_guidance_str = ""
        if session.no_action_count >= 3:
            if self.conversation_type == "private":
                no_action_guidance_str = f"\n你已经决定连续不发言/没有互动 {session.no_action_count} 次了，观察一下目前与对方的话题是不是已经告一段落了，如果是，可以考虑暂时先不专注于与对方的聊天了。"
            else:
                no_action_guidance_str = f"\n你已经决定连续不发言/没有互动 {session.no_action_count} 次了，观察一下目前群内话题是不是已经告一段落了，如果是，可以考虑暂时先不专注于群聊的消息了。"
            logger.info(f"[{self.conversation_id}] 添加无互动提示到System Prompt, count: {session.no_action_count}")

        # --- Step 3: Fetch and process events ---
        event_dicts = await self.event_storage.get_recent_chat_message_documents(
            conversation_id=self.conversation_id,
            limit=50,  # Fetch a bit more to allow for deduplication if needed, or ensure DB query is distinct
            fetch_all_event_types=True,
        )

        raw_events: list[Event] = []
        if event_dicts:
            for event_dict in event_dicts:
                try:
                    content_segs_data = event_dict.get("content", [])
                    content_segs = [
                        Seg(type=s_data.get("type", "unknown"), data=s_data.get("data", {}))
                        for s_data in content_segs_data
                        if isinstance(s_data, dict)
                    ]
                    user_info_dict = event_dict.get("user_info")
                    protocol_user_info = (
                        UserInfo(**user_info_dict) if user_info_dict and isinstance(user_info_dict, dict) else None
                    )
                    conv_info_dict = event_dict.get("conversation_info")
                    protocol_conv_info = (
                        ConversationInfo(**conv_info_dict)
                        if conv_info_dict and isinstance(conv_info_dict, dict)
                        else None
                    )
                    motivation = event_dict.pop("motivation", None)  # 从字典中取出motivation，避免传入Event构造函数
                    event_obj = Event(
                        event_id=str(event_dict.get("event_id", event_dict.get("_key", str(uuid.uuid4())))),
                        event_type=str(event_dict.get("event_type", "unknown")),
                        time=float(event_dict.get("timestamp", event_dict.get("time", 0.0))),
                        platform=str(event_dict.get("platform", "unknown")),
                        bot_id=str(event_dict.get("bot_id", "unknown")),  # This is bot_id of the event sender
                        content=content_segs,
                        user_info=protocol_user_info,
                        conversation_info=protocol_conv_info,
                        raw_data=event_dict.get("raw_data") if isinstance(event_dict.get("raw_data"), dict) else None,
                    )
                    if motivation:
                        event_obj.motivation = motivation  # 将motivation作为属性添加到对象上
                    raw_events.append(event_obj)
                except Exception as e_conv:
                    # 使用bind来安全地记录结构化数据，避免f-string格式化问题
                    logger.bind(event_dict=event_dict).error(
                        f"将数据库事件字典转换为Event对象时出错: {e_conv}", exc_info=True
                    )

        # --- Deduplicate raw_events ---
        # Priority for deduplication:
        # 1. For message events (event_type starts with "message."), use platform_message_id.
        # 2. For other events, use event_id.
        # Keep the event with the latest timestamp if duplicates are found.
        if raw_events:
            logger.debug(f"[ChatPromptBuilder][{self.conversation_id}] Original raw_events count: {len(raw_events)}")

            unique_events_dict: dict[str, Event] = {}

            # Sort by time descending to process newer events first, so we keep the newest if IDs collide
            for event_obj in sorted(raw_events, key=lambda e: e.time, reverse=True):
                dedup_key: str | None = None
                is_message_event = event_obj.event_type.startswith("message.")

                if is_message_event:
                    platform_msg_id = event_obj.get_message_id()
                    if platform_msg_id:
                        dedup_key = f"msg_{platform_msg_id}"

                if not dedup_key:  # Fallback to event_id or if not a message event with platform_msg_id
                    dedup_key = f"core_{event_obj.event_id}"

                if dedup_key not in unique_events_dict:
                    unique_events_dict[dedup_key] = event_obj
                # else, we've already seen this key, and since we sorted by time desc, the one stored is newer or same.

            raw_events = sorted(unique_events_dict.values(), key=lambda e: e.time)  # Sort back by time ascending
            logger.debug(
                f"[ChatPromptBuilder][{self.conversation_id}] Deduplicated raw_events count: {len(raw_events)}"
            )
        # --- End Deduplication ---

        user_map: dict[str, dict[str, Any]] = {}
        platform_id_to_uid_str: dict[str, str] = {}  # Maps platform-specific ID (e.g., QQ number) to U_id
        uid_counter = 0
        conversation_name_str = "未知会话"
        conversation_type_str = "未知类型"

        persona_config = config.persona

        

        # --- 动态获取机器人信息 (小懒猫修正版：先问身份！) ---
        bot_profile = await session.get_bot_profile()

        # 准备最终用于 prompt 的变量，并设置后备值
        final_bot_id = self.bot_id  # 默认使用配置文件中的 ID
        final_bot_nickname = persona_config.bot_name or "机器人"
        final_bot_card = final_bot_nickname  # 默认群名片是昵称

        if bot_profile and bot_profile.get("user_id"):
            final_bot_id = str(bot_profile["user_id"])
            final_bot_nickname = bot_profile.get("nickname", final_bot_nickname)
            final_bot_card = bot_profile.get("card", final_bot_nickname)
            logger.info(f"动态获取机器人信息成功: ID={final_bot_id}, Nick={final_bot_nickname}, Card={final_bot_card}")
        else:
            logger.warning("动态获取机器人信息失败或信息不完整，将回退到使用配置文件中的信息。")
            # Fallback to static config if API call fails, final_bot_id, etc., will use their default values.

        # 【关键修正】在确定了最终的 final_bot_id 之后，再进行登记！
        platform_id_to_uid_str[final_bot_id] = "U0"
        user_map[final_bot_id] = {
            "uid_str": "U0",
            "nick": final_bot_nickname,
            "card": final_bot_card,
            "title": bot_profile.get("title", "") if bot_profile else getattr(persona_config, "title", None) or "",
            "perm": bot_profile.get("role", "成员") if bot_profile else "成员",
        }

        # Initialize a set to track platform message IDs already added to chat_log_lines
        # This is for display-level deduplication if upstream deduplication isn't perfect.
        added_platform_message_ids_for_log = set()

        if raw_events and raw_events[0].conversation_info:
            conv_info = raw_events[0].conversation_info
            conversation_type_str = conv_info.type
            if conv_info.name:
                conversation_name_str = conv_info.name

        self.session.conversation_name = conversation_name_str

        for event_data in raw_events:
            if event_data.user_info and event_data.user_info.user_id:
                p_user_id = event_data.user_info.user_id  # Platform specific user_id
                if p_user_id not in platform_id_to_uid_str:
                    uid_counter += 1
                    uid_str = f"U{uid_counter}"
                    platform_id_to_uid_str[p_user_id] = uid_str
                    user_map[p_user_id] = {
                        "uid_str": uid_str,
                        "nick": event_data.user_info.user_nickname or f"用户{p_user_id}",
                        "card": event_data.user_info.user_cardname
                        or (event_data.user_info.user_nickname or f"用户{p_user_id}"),
                        "title": event_data.user_info.user_titlename or "",
                        "perm": event_data.user_info.permission_level or "成员",
                    }

        if self.conversation_type == "private":
            for _, user_data in user_map.items():
                if user_data.get("uid_str") == "U1":
                    user_nick = user_data.get("nick", "对方")
                    break
            if not user_nick:
                user_nick = "对方"

        conversation_info_block_str = (
            f'- conversation_name: "{conversation_name_str}"\n- conversation_type: "{conversation_type_str}"'
        )

        user_list_lines = []
        # Sort users by U_id for consistent ordering
        sorted_user_platform_ids = sorted(user_map.keys(), key=lambda pid: int(user_map[pid]["uid_str"][1:]))

        for p_id in sorted_user_platform_ids:
            user_data_item = user_map[p_id]
            user_identity_suffix = "（你）" if user_data_item["uid_str"] == "U0" else ""
            if self.conversation_type == "private":
                user_line = f"{user_data_item['uid_str']}: {p_id}{user_identity_suffix} [nick:{user_data_item['nick']}, card:{user_data_item['card']}]"
            else:
                user_line = f"{user_data_item['uid_str']}: {p_id}{user_identity_suffix} [nick:{user_data_item['nick']}, card:{user_data_item['card']}, title:{user_data_item['title']}, perm:{user_data_item['perm']}]"
            user_list_lines.append(user_line)
        user_list_block_str = "\n".join(user_list_lines)

        chat_log_lines: list[str] = []
        unread_section_started = False
        current_last_processed_timestamp = last_processed_timestamp  # Use passed param

        sorted_events = sorted(raw_events, key=lambda e: e.time)

        for event_data_log in sorted_events:
            # 根据 is_first_turn 控制是否显示已读/未读分割线
            if (
                not is_first_turn
                and event_data_log.time > current_last_processed_timestamp
                and not unread_section_started
            ):
                if chat_log_lines:
                    read_marker_time_obj = datetime.fromtimestamp(
                        current_last_processed_timestamp / 1000.0
                    )  # Use correct timestamp
                    read_marker_time_str = read_marker_time_obj.strftime("%H:%M:%S")
                    chat_log_lines.append(
                        f"--- 以上消息是你已经思考过的内容，已读 (标记时间: {read_marker_time_str}) ---"
                    )
                chat_log_lines.append("--- 请关注以下未读的新消息---")
                unread_section_started = True
            # current_last_processed_timestamp should not be updated inside the loop for marking unread correctly for all events after it.
            # It's the reference point.

            dt_obj = datetime.fromtimestamp(event_data_log.time / 1000.0)
            time_str = dt_obj.strftime("%H:%M:%S")

            log_user_id_str = "SYS"  # Default for system messages
            event_sender_platform_id = None
            if event_data_log.user_info and event_data_log.user_info.user_id:
                event_sender_platform_id = event_data_log.user_info.user_id
                log_user_id_str = platform_id_to_uid_str.get(
                    event_sender_platform_id, f"UnknownUser({event_sender_platform_id[:4]})"
                )

            log_line = ""
            msg_id_for_display = event_data_log.get_message_id() or event_data_log.event_id
            # event_internal_id = event_data_log.event_id # 不再需要通过 sent_actions_context 查找

            # is_bot_sent_msg_with_context = False # 不再需要这个标记
            # if log_user_id_str == "U0" and event_internal_id in sent_actions_context: # Use passed param
            #     is_bot_sent_msg_with_context = True

            is_robot_message_to_display_as_msg = (
                log_user_id_str == "U0" and event_data_log.event_type.startswith("message.")
            ) or (
                log_user_id_str == "U0" and event_data_log.event_type == "action.message.send"
            )  # 修复：根据bug报告，应为 send

            # Display-level deduplication for messages based on platform message ID
            current_platform_msg_id = event_data_log.get_message_id()
            if event_data_log.event_type.startswith("message.") and current_platform_msg_id:
                if current_platform_msg_id in added_platform_message_ids_for_log:
                    logger.debug(f"Skipping duplicate message for display, platform_msg_id: {current_platform_msg_id}")
                    continue  # Skip this event as it's a display duplicate
                added_platform_message_ids_for_log.add(current_platform_msg_id)

            if event_data_log.event_type.startswith("message.") or is_robot_message_to_display_as_msg:
                # --- New Logic for Handling Messages with Quotes ---
                quote_display_str = ""
                main_content_parts = []
                main_content_type = "MSG"  # Default to MSG

                for seg in event_data_log.content:
                    if seg.type == "quote":
                        quoted_message_id = seg.data.get("message_id", "unknown_id")
                        quoted_user_id = seg.data.get("user_id")
                        if quoted_user_id:
                            quoted_user_uid = platform_id_to_uid_str.get(
                                quoted_user_id, f"未知用户({quoted_user_id[:4]})"
                            )
                            quote_display_str = f"引用/回复 {quoted_user_uid}(id:{quoted_message_id})"
                        else:
                            quote_display_str = f"引用/回复 (id:{quoted_message_id})"
                    elif seg.type == "text":
                        main_content_parts.append(seg.data.get("text", ""))
                    elif seg.type == "image":
                        main_content_type = "IMG"
                        img_src = seg.data.get("file_id") or seg.data.get("url", "unknown_image")
                        main_content_parts.append(f"[IMG:{img_src.split('/')[-1][:15]}]")
                    elif seg.type == "at":
                        at_user_id = seg.data.get("user_id")
                        at_display_name = seg.data.get("display_name")
                        if at_user_id in platform_id_to_uid_str:
                            at_display_name = platform_id_to_uid_str[at_user_id]
                        elif not at_display_name:
                            at_display_name = f"@{at_user_id}"
                        main_content_parts.append(f"@{at_display_name} ")
                    elif seg.type == "face":
                        face_id = seg.data.get("id", "未知表情")
                        main_content_parts.append(f"[表情:{face_id}]")
                    elif seg.type == "file":
                        main_content_type = "FILE"
                        file_name = seg.data.get("name", "未知文件")
                        file_size = seg.data.get("size", 0)
                        main_content_parts.append(f"[FILE:{file_name} ({file_size} bytes)]")

                main_content_str = "".join(main_content_parts).strip()

                # Determine final display tag
                display_tag = main_content_type
                if quote_display_str:
                    display_tag = f"{main_content_type}, {quote_display_str}"

                log_line = (
                    f"[{time_str}] {log_user_id_str} [{display_tag}]: {main_content_str} (id:{msg_id_for_display})"
                )

                event_motivation = getattr(event_data_log, "motivation", None)
                if log_user_id_str == "U0" and event_motivation and event_motivation.strip():
                    log_line += f"\n    - [MOTIVE]: {event_motivation}"

                chat_log_lines.append(log_line)
                log_line = ""  # Clear log_line after use
                # --- End of New Logic ---

            # Simplified event type handling for brevity, assuming QQ-like structures for now
            elif event_data_log.event_type == "notice.group.increase":
                op_id = event_data_log.content[0].data.get("operator_id") if event_data_log.content else None
                tar_id = event_data_log.content[0].data.get("target_id") if event_data_log.content else None
                op_uid = platform_id_to_uid_str.get(op_id, op_id or "UnknownOperator")
                tar_uid = platform_id_to_uid_str.get(tar_id, tar_id or "UnknownTarget")
                log_line = f"[{time_str}] [SYS]: {op_uid}邀请{tar_uid}加入了群聊。"
            # ... other notice types would be similarly formatted ...
            elif event_data_log.event_type == "internal.sub_consciousness.thought_log":
                motivation_text = extract_text_from_content(event_data_log.content)
                log_line = f"[{time_str}] {log_user_id_str} [MOTIVE]: {motivation_text}"
            else:
                log_line = f"[{time_str}] {log_user_id_str} [{event_data_log.event_type.split('.')[-1].upper()}]: {extract_text_from_content(event_data_log.content)[:30]}... (id:{event_data_log.event_id})"

            if log_line:  # 对于非MSG事件，或者没有motivation的MSG事件，这里仍然需要处理
                chat_log_lines.append(log_line)

        # 根据 is_first_turn 控制是否显示“以上消息是你已经思考过的内容”的最终标记
        if (
            not is_first_turn and not unread_section_started and chat_log_lines
        ):  # If all messages were already processed
            marker_ts_for_all_read = sorted_events[-1].time if sorted_events else current_last_processed_timestamp
            read_marker_time_obj = datetime.fromtimestamp(marker_ts_for_all_read / 1000.0)
            read_marker_time_str = read_marker_time_obj.strftime("%H:%M:%S")
            chat_log_lines.append(f"--- 以上消息是你已经思考过的内容，已读 (标记时间: {read_marker_time_str}) ---")

        chat_history_log_block_str = "\n".join(chat_log_lines)
        if not chat_history_log_block_str:
            chat_history_log_block_str = "当前没有聊天记录。"

        previous_thoughts_block_str = ""
        if is_first_turn:
            mood_part = ""
            if session.initial_core_mood:
                mood_part = f'你刚才的心情是"{session.initial_core_mood}"。\n'

            think_part = ""
            if last_think_from_core:
                think_part = f"你刚才的想法是：{last_think_from_core}\n\n现在你刚刚把注意力放到这个群聊中；\n\n原因是：你对当前聊天内容有点兴趣\n"
            else:
                think_part = "你已进入专注模式，开始处理此会话。\n"

            previous_thoughts_block_str = (
                f"<previous_thoughts_and_actions>\n{mood_part}{think_part}</previous_thoughts_and_actions>"
            )
        elif last_llm_decision:  # 不是第一次，且有上一轮子意识的思考 (last_llm_decision 来自 ChatSession)
            think_content = last_llm_decision.get("think", "")
            mood_content = last_llm_decision.get("mood", "平静")
            reply_text = last_llm_decision.get("reply_text")
            motivation = last_llm_decision.get("motivation", "")
            reply_willing = last_llm_decision.get("reply_willing", False)
            poke_target_id = last_llm_decision.get("poke")

            action_desc = ""
            if reply_willing and reply_text:
                action_desc = f"发言（发言内容为：{reply_text}）"
            elif reply_willing and not reply_text:
                action_desc = "决定发言但未提供内容"
            else:
                if poke_target_id:
                    poked_user_display = platform_id_to_uid_str.get(poke_target_id, poke_target_id)
                    action_desc = f"戳一戳 {poked_user_display}"
                else:
                    action_desc = "暂时不发言"

            prev_parts = [
                f'<previous_thoughts_and_actions>\n刚刚你的心情是："{mood_content}"\n刚刚你的内心想法是："{think_content}"'
            ]
            if action_desc:
                prev_parts.append(f"出于这个想法，你刚才做了：{action_desc}")

            if motivation and (
                action_desc == "暂时不发言"
                or action_desc.startswith("戳一戳")
                or not motivation.startswith(action_desc)
            ):
                if action_desc != motivation:  # 避免重复显示动机
                    prev_parts.append(f"因为：{motivation}")
            elif not reply_willing and not poke_target_id and motivation:  # 如果没回复也没戳人，但有不发言的动机
                prev_parts.append(f"因为：{motivation}")

            prev_parts.append("</previous_thoughts_and_actions>")
            previous_thoughts_block_str = "\n".join(prev_parts)
        # 如果不是 is_first_turn 且没有 last_llm_decision，则 previous_thoughts_block_str 保持为空字符串
        # 修正：确保即使 last_llm_decision 为 None，也有一个默认的 previous_thoughts_block
        elif not last_llm_decision:  # is_first_turn is False, but no last_llm_decision
            previous_thoughts_block_str = "<previous_thoughts_and_actions>\n我正在处理当前会话，但上一轮的思考信息似乎丢失了。\n</previous_thoughts_and_actions>"

        # --- Step 6: Assemble final prompts ---
        # 根据会话类型选择不同的格式化参数
        if self.conversation_type == "group":
            system_prompt = system_prompt_template.format(
                current_time=current_time_str,
                bot_name=bot_name_str,
                optional_description=bot_description_str,
                optional_profile=bot_profile_str,
                bot_id=final_bot_id,
                bot_nickname=final_bot_nickname,
                conversation_name=conversation_name_str,
                bot_card=final_bot_card,
                no_action_guidance=no_action_guidance_str,
            )
        else:  # private
            system_prompt = system_prompt_template.format(
                current_time=current_time_str,
                bot_name=bot_name_str,
                bot_id=final_bot_id,
                optional_description=bot_description_str,
                optional_profile=bot_profile_str,
                no_action_guidance=no_action_guidance_str,
                user_nick=user_nick,
            )

        user_prompt = user_prompt_template.format(
            conversation_info_block=conversation_info_block_str,
            user_list_block=user_list_block_str,
            chat_history_log_block=chat_history_log_block_str,
            previous_thoughts_block=previous_thoughts_block_str,
        )

        # --- Construct uid_str_to_platform_id_map ---
        uid_str_to_platform_id_map: dict[str, str] = {uid_str: p_id for p_id, uid_str in platform_id_to_uid_str.items()}
        logger.debug(
            f"[ChatPromptBuilder][{self.conversation_id}] Constructed uid_str_to_platform_id_map: {uid_str_to_platform_id_map}"
        )

        # --- Collect processed_event_ids ---
        # 简单版本：收集所有在 last_processed_timestamp 之后，且类型为 message.* 的事件ID
        # 注意：这可能需要更精确的逻辑，确保只包含真正被用于生成 prompt 的用户消息
        processed_event_ids: list[str] = []
        if sorted_events:  # sorted_events 是从数据库获取并按时间排序的事件
            for event_obj in sorted_events:
                if event_obj.event_type.startswith("message.") and event_obj.time > last_processed_timestamp:
                    processed_event_ids.append(event_obj.event_id)

        logger.debug(
            f"[ChatPromptBuilder][{self.conversation_id}] Collected processed_event_ids: {processed_event_ids}"
        )

        return system_prompt, user_prompt, uid_str_to_platform_id_map, processed_event_ids
