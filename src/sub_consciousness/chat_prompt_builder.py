# -*- coding: utf-8 -*-
import uuid
from datetime import datetime
from typing import Optional, Dict, Any, List, Tuple
from collections import OrderedDict

from aicarus_protocols.event import Event
from aicarus_protocols.seg import Seg # SegBuilder is not used here directly for now
from aicarus_protocols.common import extract_text_from_content
from aicarus_protocols.user_info import UserInfo
from aicarus_protocols.conversation_info import ConversationInfo

from src.database.services.event_storage_service import EventStorageService
from src.common.custom_logging.logger_manager import get_logger
from src.config import config

logger = get_logger(__name__)

# --- Prompt Templates ---
# Note: bot_qq_id in SYSTEM_PROMPT_TEMPLATE is changed to bot_id for generality
SYSTEM_PROMPT_TEMPLATE = """
当前时间：{current_time}
你是{bot_name}；
你的qq号是{bot_id}；
{optional_description}
{optional_profile}
你当前正在参与qq群聊
""" 

USER_PROMPT_TEMPLATE = """
<当前聊天信息>
# CONTEXT
## Conversation Info
{conversation_info_block}

## Users
# 格式: ID: qq号 [nick:昵称, card:群名片/备注, title:头衔, perm:权限]
{user_list_block}

## Event Types
[MSG]: 普通消息，在消息后的（id:xxx）为消息的id
[SYS]: 系统通知
[MOTIVE]: 对应你的"motivation"，帮助你更好的了解自己的心路历程，它有两种出现形式：
      1. 独立出现时 (无缩进): 代表你经过思考后，决定“保持沉默/不发言”的原因。
      2. 附属出现时 (在[MSG]下缩进): 代表你发出该条消息的“背后动机”或“原因”，是消息的附注说明。
[IMG]: 图片消息
[FILE]: 文件分享

# CHAT HISTORY LOG
{chat_history_log_block}
</当前聊天信息>

{previous_thoughts_block}

<thinking_guidance>
请仔细阅读当前聊天内容，分析讨论话题和成员关系，分析你刚刚发言和别人对你的发言的反应，思考你要不要回复或发言。
注意耐心：
  -请特别关注对话的自然流转和对方的输入状态。如果感觉对方可能正在打字或思考，或者其发言明显未结束（比如话说到一半），请耐心等待，避免过早打断或急于追问。
  -如果你发送消息后对方没有立即回应，请优先考虑对方是否正在忙碌或话题已自然结束，内心想法应倾向于“耐心等待”或“思考对方是否在忙”，而非立即追问，除非追问非常必要且不会打扰。
当你觉得不想聊了，或对话已经告一段落时，请在"end_focused_chat"字段中填写true。
思考并输出你真实的内心想法。
</thinking_guidance>

<output_requirements_for_inner_thought>
1. 根据聊天内容生成你的内心想法，但是注意话题的推进，不要在一个话题上停留太久或揪着一个话题不放，除非你觉得真的有必要
   - 如果你决定回复或发言，请在"reply_text"中填写你准备发送的消息的具体内容，应该非常简短自然，省略主语
2. 不要分点、不要使用表情符号
3. 避免多余符号(冒号、引号、括号等)
4. 语言简洁自然，不要浮夸
5. 不要把注意力放在别人发的表情包上，它们只是一种辅助表达方式
6. 注意分辨群里谁在跟谁说话，你不一定是当前聊天的主角，消息中的“你”不一定指的是你，也可能是别人
7. 默认使用中文
</output_requirements_for_inner_thought>

现在请你请输出你现在的心情，内心想法，是否要发言，发言的动机，和要发言的内容等等。
请严格使用以下json格式输出内容，不需要输出markdown语句等多余内容，仅输出纯json内容：
```json
{{
    "mood":"此处填写你现在的心情，与造成这个心情的原因",
    "think":"此处填写你此时的内心想法，衔接你刚才的想法继续思考，应该自然流畅",
    "reply_willing":"此处决定是否发言，布尔值，true为发言，false为先不发言",
    "motivation":"此处填写发言/不发言的动机，会保留在聊天记录中，帮助你更好的了解自己的心路历程",
    "at_someone":"【可选】仅在reply_willing为True时有效，通常可能不需要，当目前群聊比较混乱，需要明确对某人说话的时使用，填写你想@的人的平台ID，如果需要@多个人，请用逗号隔开，如果不需要则不输出此字段",
    "quote_reply":"【可选】仅在reply_willing为True时有效，通常可能不需要，当需要明确回复某条消息时使用，填写你想具体回复的消息的message_id，只能回复一条，如果不需要则不输出此字段",
    "reply_text":"此处填写你完整的发言内容，应该尽可能简短，自然，口语化，多简短都可以。若已经@某人或引用回复某条消息，则建议省略主语。若reply_willing为False，则不输出此字段",
    "poke":"【可选】平台特有的戳一戳功能，填写目标平台ID，如果不需要则不输出此字段",
    "action_to_take":"【可选】描述你当前最想做的、需要与外界交互的具体动作，例如上网查询某信息，如果无，则不包含此字段", 
    "action_motivation":"【可选】如果你有想做的动作，请说明其动机。如果action_to_take不输出，此字段也应不输出",
    "end_focused_chat":"【可选】布尔值。当你认为本次对话可以告一段落时，请将此字段设为true。其它情况下，保持其为false"
}}
```"""

class ChatPromptBuilder:
    def __init__(
        self,
        event_storage: EventStorageService,
        bot_id: str, # Changed from bot_qq_id
        # platform: str, # May not be needed directly if conv_info has it
        # conversation_type: str, # May not be needed directly if conv_info has it
        conversation_id: str
    ):
        self.event_storage: EventStorageService = event_storage
        self.bot_id: str = bot_id
        # self.platform: str = platform
        # self.conversation_type: str = conversation_type
        self.conversation_id: str = conversation_id
        logger.info(f"[ChatPromptBuilder][{self.conversation_id}] 实例已创建 for bot_id: {self.bot_id}.")

    async def build_prompts(
        self,
        last_processed_timestamp: float,
        last_llm_decision: Optional[Dict[str, Any]],
        sent_actions_context: OrderedDict[str, Dict[str, Any]],
        is_first_turn: bool, # 新增参数
        last_think_from_core: Optional[str] = None # 新增参数
    ) -> Tuple[str, str, Dict[str, str], List[str]]: # 修改返回类型，增加 List[str] for processed_event_ids
        # --- Prepare data for System Prompt ---
        current_time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        persona_config = config.persona
        bot_name_str = persona_config.bot_name or "AI"
        bot_description_str = f"\n{persona_config.description}" if persona_config.description else ""
        bot_profile_str = f"\n{persona_config.profile}" if persona_config.profile else ""

        system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
            current_time=current_time_str,
            bot_name=bot_name_str,
            bot_id=self.bot_id, # Use generalized bot_id
            optional_description=bot_description_str,
            optional_profile=bot_profile_str
        )

        # --- Prepare data for User Prompt ---
        event_dicts = await self.event_storage.get_recent_chat_message_documents(
            conversation_id=self.conversation_id,
            limit=50, # Fetch a bit more to allow for deduplication if needed, or ensure DB query is distinct
            fetch_all_event_types=True
        )
        
        raw_events: List[Event] = []
        if event_dicts:
            for event_dict in event_dicts:
                try:
                    content_segs_data = event_dict.get('content', [])
                    content_segs = [Seg(type=s_data.get('type','unknown'), data=s_data.get('data',{})) for s_data in content_segs_data if isinstance(s_data, dict)]
                    user_info_dict = event_dict.get('user_info')
                    protocol_user_info = UserInfo(**user_info_dict) if user_info_dict and isinstance(user_info_dict, dict) else None
                    conv_info_dict = event_dict.get('conversation_info')
                    protocol_conv_info = ConversationInfo(**conv_info_dict) if conv_info_dict and isinstance(conv_info_dict, dict) else None
                    motivation = event_dict.pop('motivation', None) # 从字典中取出motivation，避免传入Event构造函数
                    event_obj = Event(
                        event_id=str(event_dict.get('event_id', event_dict.get('_key', str(uuid.uuid4())))),
                        event_type=str(event_dict.get('event_type', 'unknown')),
                        time=float(event_dict.get('timestamp', event_dict.get('time', 0.0))),
                        platform=str(event_dict.get('platform', 'unknown')),
                        bot_id=str(event_dict.get('bot_id', 'unknown')), # This is bot_id of the event sender
                        content=content_segs,
                        user_info=protocol_user_info,
                        conversation_info=protocol_conv_info,
                        raw_data=event_dict.get('raw_data') if isinstance(event_dict.get('raw_data'), dict) else None
                    )
                    if motivation:
                        setattr(event_obj, 'motivation', motivation) # 将motivation作为属性添加到对象上
                    raw_events.append(event_obj)
                except Exception as e_conv:
                    # 使用bind来安全地记录结构化数据，避免f-string格式化问题
                    logger.bind(event_dict=event_dict).error(f"将数据库事件字典转换为Event对象时出错: {e_conv}", exc_info=True)
        
        # --- Deduplicate raw_events ---
        # Priority for deduplication:
        # 1. For message events (event_type starts with "message."), use platform_message_id.
        # 2. For other events, use event_id.
        # Keep the event with the latest timestamp if duplicates are found.
        if raw_events:
            logger.debug(f"[ChatPromptBuilder][{self.conversation_id}] Original raw_events count: {len(raw_events)}")
            
            unique_events_dict: Dict[str, Event] = {}
            
            # Sort by time descending to process newer events first, so we keep the newest if IDs collide
            for event_obj in sorted(raw_events, key=lambda e: e.time, reverse=True):
                dedup_key: Optional[str] = None
                is_message_event = event_obj.event_type.startswith("message.")
                
                if is_message_event:
                    platform_msg_id = event_obj.get_message_id()
                    if platform_msg_id:
                        dedup_key = f"msg_{platform_msg_id}"
                
                if not dedup_key: # Fallback to event_id or if not a message event with platform_msg_id
                    dedup_key = f"core_{event_obj.event_id}"
                    
                if dedup_key not in unique_events_dict:
                    unique_events_dict[dedup_key] = event_obj
                # else, we've already seen this key, and since we sorted by time desc, the one stored is newer or same.
            
            raw_events = sorted(list(unique_events_dict.values()), key=lambda e: e.time) # Sort back by time ascending
            logger.debug(f"[ChatPromptBuilder][{self.conversation_id}] Deduplicated raw_events count: {len(raw_events)}")
        # --- End Deduplication ---

        user_map: Dict[str, Dict[str, Any]] = {}
        platform_id_to_uid_str: Dict[str, str] = {} # Maps platform-specific ID (e.g., QQ number) to U_id
        uid_counter = 0
        conversation_name_str = "未知会话"
        conversation_type_str = "未知类型"

        platform_id_to_uid_str[self.bot_id] = "U0" # Bot's own platform ID
        user_map[self.bot_id] = {
            "uid_str": "U0",
            "nick": persona_config.bot_name or "机器人",
            "card": persona_config.bot_name or "机器人",
            "title": getattr(persona_config, 'title', None) or "",
            "perm": "成员"
        }
        
        # Initialize a set to track platform message IDs already added to chat_log_lines
        # This is for display-level deduplication if upstream deduplication isn't perfect.
        added_platform_message_ids_for_log = set()

        if raw_events and raw_events[0].conversation_info:
            conv_info = raw_events[0].conversation_info
            conversation_type_str = conv_info.type
            if conv_info.name:
                conversation_name_str = conv_info.name
        
        for event_data in raw_events:
            if event_data.user_info and event_data.user_info.user_id:
                p_user_id = event_data.user_info.user_id # Platform specific user_id
                if p_user_id not in platform_id_to_uid_str:
                    uid_counter += 1
                    uid_str = f"U{uid_counter}"
                    platform_id_to_uid_str[p_user_id] = uid_str
                    user_map[p_user_id] = {
                        "uid_str": uid_str,
                        "nick": event_data.user_info.user_nickname or f"用户{p_user_id}",
                        "card": event_data.user_info.user_cardname or (event_data.user_info.user_nickname or f"用户{p_user_id}"),
                        "title": event_data.user_info.user_titlename or "",
                        "perm": event_data.user_info.permission_level or "成员"
                    }
        
        conversation_info_block_str = f"- conversation_name: \"{conversation_name_str}\"\n- conversation_type: \"{conversation_type_str}\""
        
        user_list_lines = []
        # Sort users by U_id for consistent ordering
        sorted_user_platform_ids = sorted(user_map.keys(), key=lambda pid: int(user_map[pid]["uid_str"][1:]))

        for p_id in sorted_user_platform_ids:
            user_data_item = user_map[p_id]
            user_identity_suffix = "（你）" if user_data_item["uid_str"] == "U0" else ""
            # Displaying platform ID (p_id) along with U_id
            user_line = f"{user_data_item['uid_str']}: {p_id}{user_identity_suffix} [nick:{user_data_item['nick']}, card:{user_data_item['card']}, title:{user_data_item['title']}, perm:{user_data_item['perm']}]"
            user_list_lines.append(user_line)
        user_list_block_str = "\n".join(user_list_lines)

        chat_log_lines: List[str] = []
        unread_section_started = False
        current_last_processed_timestamp = last_processed_timestamp # Use passed param
        
        sorted_events = sorted(raw_events, key=lambda e: e.time)

        for event_data_log in sorted_events:
            # 根据 is_first_turn 控制是否显示已读/未读分割线
            if not is_first_turn:
                if event_data_log.time > current_last_processed_timestamp and not unread_section_started:
                    if chat_log_lines:
                        read_marker_time_obj = datetime.fromtimestamp(current_last_processed_timestamp / 1000.0) # Use correct timestamp
                        read_marker_time_str = read_marker_time_obj.strftime("%H:%M:%S")
                        chat_log_lines.append(f"--- 以上消息是你已经思考过的内容，已读 (标记时间: {read_marker_time_str}) ---")
                    chat_log_lines.append("--- 请关注以下未读的新消息---")
                    unread_section_started = True
            # current_last_processed_timestamp should not be updated inside the loop for marking unread correctly for all events after it.
            # It's the reference point.

            dt_obj = datetime.fromtimestamp(event_data_log.time / 1000.0)
            time_str = dt_obj.strftime("%H:%M:%S")
            
            log_user_id_str = "SYS" # Default for system messages
            event_sender_platform_id = None
            if event_data_log.user_info and event_data_log.user_info.user_id:
                event_sender_platform_id = event_data_log.user_info.user_id
                log_user_id_str = platform_id_to_uid_str.get(event_sender_platform_id, f"UnknownUser({event_sender_platform_id[:4]})")

            log_line = ""
            msg_id_for_display = event_data_log.get_message_id() or event_data_log.event_id
            # event_internal_id = event_data_log.event_id # 不再需要通过 sent_actions_context 查找
            
            # is_bot_sent_msg_with_context = False # 不再需要这个标记
            # if log_user_id_str == "U0" and event_internal_id in sent_actions_context: # Use passed param
            #     is_bot_sent_msg_with_context = True
            
            is_robot_message_to_display_as_msg = \
                (log_user_id_str == "U0" and event_data_log.event_type.startswith("message.")) or \
                (log_user_id_str == "U0" and event_data_log.event_type == "action.message.send") # 修复：根据bug报告，应为 send

            # Display-level deduplication for messages based on platform message ID
            current_platform_msg_id = event_data_log.get_message_id()
            if event_data_log.event_type.startswith("message.") and current_platform_msg_id:
                if current_platform_msg_id in added_platform_message_ids_for_log:
                    logger.debug(f"Skipping duplicate message for display, platform_msg_id: {current_platform_msg_id}")
                    continue # Skip this event as it's a display duplicate
                added_platform_message_ids_for_log.add(current_platform_msg_id)

            if event_data_log.event_type.startswith("message.") or is_robot_message_to_display_as_msg:
                text_content = ""
                for seg in event_data_log.content:
                    if seg.type == "text": text_content += seg.data.get("text", "")
                    elif seg.type == "image": 
                        img_src = seg.data.get('file_id') or seg.data.get('url', 'unknown_image')
                        text_content += f"[IMG:{img_src.split('/')[-1][:15]}]"
                    elif seg.type == "at":
                        at_user_id = seg.data.get("user_id")
                        at_display_name = seg.data.get("display_name")
                        if at_user_id in platform_id_to_uid_str:
                            at_display_name = platform_id_to_uid_str[at_user_id]
                        elif not at_display_name:
                            at_display_name = f"@{at_user_id}"
                        text_content += f"@{at_display_name} "
                    elif seg.type == "face":
                        face_id = seg.data.get("id", "未知表情")
                        text_content += f"[表情:{face_id}]"
                    elif seg.type == "file":
                        file_name = seg.data.get("name", "未知文件")
                        file_size = seg.data.get("size", 0)
                        text_content += f"[FILE:{file_name} ({file_size} bytes)]"
                
                log_line = f"[{time_str}] {log_user_id_str} [MSG]: {text_content.strip()} (id:{msg_id_for_display})"
                
                event_motivation = getattr(event_data_log, 'motivation', None)
                if log_user_id_str == "U0" and event_motivation and event_motivation.strip():
                    log_line += f"\n    - [MOTIVE]: {event_motivation}"
                
                chat_log_lines.append(log_line)
                log_line = "" # 清空log_line
            
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
            
            if log_line: # 对于非MSG事件，或者没有motivation的MSG事件，这里仍然需要处理
                chat_log_lines.append(log_line)
        
        # 根据 is_first_turn 控制是否显示“以上消息是你已经思考过的内容”的最终标记
        if not is_first_turn:
            if not unread_section_started and chat_log_lines: # If all messages were already processed
                marker_ts_for_all_read = sorted_events[-1].time if sorted_events else current_last_processed_timestamp
                read_marker_time_obj = datetime.fromtimestamp(marker_ts_for_all_read / 1000.0)
                read_marker_time_str = read_marker_time_obj.strftime("%H:%M:%S")
                chat_log_lines.append(f"--- 以上消息是你已经思考过的内容，已读 (标记时间: {read_marker_time_str}) ---")

        chat_history_log_block_str = "\n".join(chat_log_lines)
        if not chat_history_log_block_str: chat_history_log_block_str = "当前没有聊天记录。"
        
        previous_thoughts_block_str = ""
        if is_first_turn:
            if last_think_from_core:
                previous_thoughts_block_str = f"<previous_thoughts_and_actions>\n你刚才的想法是：{last_think_from_core}\n\n现在你刚刚把注意力放到这个群聊中；\n\n原因是：你对当前聊天内容有点兴趣\n</previous_thoughts_and_actions>"
            else:
                previous_thoughts_block_str = "<previous_thoughts_and_actions>\n你已进入专注模式，开始处理此会话。\n</previous_thoughts_and_actions>"
        elif last_llm_decision: # 不是第一次，且有上一轮子意识的思考 (last_llm_decision 来自 ChatSession)
            think_content = last_llm_decision.get("think", "") 
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
            
            prev_parts = [f"<previous_thoughts_and_actions>\n刚刚你的内心想法是：\"{think_content}\""]
            if action_desc:
                prev_parts.append(f"出于这个想法，你刚才做了：{action_desc}")
            
            if motivation and (action_desc == "暂时不发言" or action_desc.startswith("戳一戳") or not motivation.startswith(action_desc)):
                 if action_desc != motivation: # 避免重复显示动机
                    prev_parts.append(f"因为：{motivation}")
            elif not reply_willing and not poke_target_id and motivation: # 如果没回复也没戳人，但有不发言的动机
                 prev_parts.append(f"因为：{motivation}")

            prev_parts.append("</previous_thoughts_and_actions>")
            previous_thoughts_block_str = "\n".join(prev_parts)
        # 如果不是 is_first_turn 且没有 last_llm_decision，则 previous_thoughts_block_str 保持为空字符串
        # 修正：确保即使 last_llm_decision 为 None，也有一个默认的 previous_thoughts_block
        elif not last_llm_decision: # is_first_turn is False, but no last_llm_decision
             previous_thoughts_block_str = "<previous_thoughts_and_actions>\n我正在处理当前会话，但上一轮的思考信息似乎丢失了。\n</previous_thoughts_and_actions>"


        user_prompt = USER_PROMPT_TEMPLATE.format(
            conversation_info_block=conversation_info_block_str,
            user_list_block=user_list_block_str,
            chat_history_log_block=chat_history_log_block_str,
            previous_thoughts_block=previous_thoughts_block_str # 确保这个占位符在模板中
        )
        
        # --- Construct uid_str_to_platform_id_map ---
        uid_str_to_platform_id_map: Dict[str, str] = {
            uid_str: p_id for p_id, uid_str in platform_id_to_uid_str.items()
        }
        logger.debug(f"[ChatPromptBuilder][{self.conversation_id}] Constructed uid_str_to_platform_id_map: {uid_str_to_platform_id_map}")

        # --- Collect processed_event_ids ---
        # 简单版本：收集所有在 last_processed_timestamp 之后，且类型为 message.* 的事件ID
        # 注意：这可能需要更精确的逻辑，确保只包含真正被用于生成 prompt 的用户消息
        processed_event_ids: List[str] = []
        if sorted_events: # sorted_events 是从数据库获取并按时间排序的事件
            for event_obj in sorted_events:
                if event_obj.event_type.startswith("message.") and event_obj.time > last_processed_timestamp:
                    processed_event_ids.append(event_obj.event_id)
        
        logger.debug(f"[ChatPromptBuilder][{self.conversation_id}] Collected processed_event_ids: {processed_event_ids}")

        return system_prompt, user_prompt, uid_str_to_platform_id_map, processed_event_ids
