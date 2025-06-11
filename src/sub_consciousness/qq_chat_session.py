# -*- coding: utf-8 -*-
import asyncio
import time
import json
import re 
import uuid 
from datetime import datetime
from typing import Optional, Dict, Any, List ,Tuple
from collections import OrderedDict

from aicarus_protocols.event import Event
from aicarus_protocols.seg import Seg, SegBuilder
from aicarus_protocols.common import extract_text_from_content
from aicarus_protocols.user_info import UserInfo
from aicarus_protocols.conversation_info import ConversationInfo

from src.llmrequest.llm_processor import Client as LLMProcessorClient
from src.database.services.event_storage_service import EventStorageService
from src.action.action_handler import ActionHandler
from src.common.custom_logging.logger_manager import get_logger
from src.config import config

logger = get_logger(__name__)

# --- Prompt Templates ---
SYSTEM_PROMPT_TEMPLATE = """
当前时间：{current_time}
你是{bot_name}；
你的qq号是{bot_qq_id}；
{optional_description}
{optional_profile}
你当前正在参与qq群聊
"""

USER_PROMPT_TEMPLATE = """
<当前群聊信息>
# CONTEXT
## Group Info
{group_info_block}

## Users
# 格式: ID: QQ [nick:昵称, card:群名片, title:头衔, perm:权限]
{user_list_block}

## Event Types
[MSG]: 普通消息，在消息后的（id:xxx）为消息的id
[SYS]: 系统通知
[ACT]: 对应你的"motivation"，帮助你更好的了解自己的心路历程，它有两种出现形式：
      1. 独立出现时 (无缩进): 代表你经过思考后，决定“保持沉默/观察”的完整行为。这是你在该时间点的主要动作。
      2. 附属出现时 (在[MSG]下缩进): 代表你发出该条消息的“背后动机”或“原因”，是消息的附注说明。
[IMG]: 图片消息
[FILE]: 文件分享

# CHAT HISTORY LOG
{chat_history_log_block}
</当前群聊信息>

{previous_thoughts_block}

<thinking_guidance>
请仔细阅读当前聊天内容，分析讨论话题和群成员关系，分析你刚刚发言和别人对你的发言的反应，思考你要不要回复或发言。
注意耐心：
  -请特别关注对话的自然流转和对方的输入状态。如果感觉对方可能正在打字或思考，或者其发言明显未结束（比如话说到一半），请耐心等待，避免过早打断或急于追问。
  -如果你发送消息后对方没有立即回应，请优先考虑对方是否正在忙碌或话题已自然结束，内心想法应倾向于“耐心等待”或“思考对方是否在忙”，而非立即追问，除非追问非常必要且不会打扰。
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
    "reasoning":"此处填写你此时的内心想法，衔接你刚才的想法继续思考，应该自然流畅",
    "reply_willing":"此处决定是否发言，布尔值，true为发言，false为先不发言",
    "motivation":"此处填写发言/不发言的动机，会保留在聊天记录中，帮助你更好的了解自己的心路历程",
    "at_someone":"【可选】仅在reply_willing为True时有效，通常可能不需要，当目前群聊比较混乱，需要明确对某人说话的时使用，填写你想@的人的qq号，如果需要@多个人，请用逗号隔开，如果不需要则不输出此字段",
    "quote_reply":"【可选】仅在reply_willing为True时有效，通常可能不需要，当需要明确回复某条消息时使用，填写你想具体回复的消息的message_id，只能回复一条，如果不需要则不输出此字段",
    "reply_text":"此处填写你完整的发言内容，应该尽可能简短，自然，口语化，多简短都可以。若已经@某人或引用回复某条消息，则建议省略主语。若reply_willing为False，则不输出此字段",
    "poke":"【可选】qq戳一戳功能，无太大实际意义，多半是娱乐作用，或是试图引起某人注意，填写目标qq号，如果不需要则不输出此字段",
    "action_to_take": "【可选】描述你当前最想做的、需要与外界交互的具体动作，例如上网查询某信息，如果无，则不包含此字段", 
    "action_motivation": "【可选】如果你有想做的动作，请说明其动机。如果action_to_take不输出，此字段也应不输出"
}}
```"""

class QQChatSession:
    def __init__(
        self,
        conversation_id: str,
        llm_client: LLMProcessorClient,
        event_storage: EventStorageService,
        action_handler: ActionHandler,
        bot_qq_id: str,
        platform: str, 
        conversation_type: str 
    ):
        self.conversation_id: str = conversation_id
        self.llm_client: LLMProcessorClient = llm_client
        self.event_storage: EventStorageService = event_storage
        self.action_handler: ActionHandler = action_handler
        self.bot_qq_id: str = bot_qq_id
        self.platform: str = platform
        self.conversation_type: str = conversation_type
        self.is_active: bool = False
        self.last_active_time: float = 0.0
        self.last_processed_timestamp: float = 0.0 
        self.last_llm_decision: Optional[Dict[str, Any]] = None 
        self.sent_actions_context: OrderedDict[str, Dict[str, Any]] = OrderedDict()
        self.processing_lock = asyncio.Lock()
        logger.info(f"[ChatSession][{self.conversation_id}] 实例已创建。")

    def activate(self):
        if not self.is_active:
            self.is_active = True
            self.last_active_time = time.time()
            logger.info(f"[ChatSession][{self.conversation_id}] 已激活。")

    def deactivate(self):
        if self.is_active:
            self.is_active = False
            self.last_llm_decision = None 
            self.last_processed_timestamp = 0.0
            logger.info(f"[ChatSession][{self.conversation_id}] 已因不活跃而停用。")

    async def _build_prompt(self) -> Tuple[str, str]:
        # --- Prepare data for System Prompt ---
        current_time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S") 
        persona_config = config.persona
        bot_name_str = persona_config.bot_name or "AI"
        bot_description_str = f"\n{persona_config.description}" if persona_config.description else ""
        bot_profile_str = f"\n{persona_config.profile}" if persona_config.profile else ""

        system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
            current_time=current_time_str,
            bot_name=bot_name_str,
            bot_qq_id=self.bot_qq_id,
            optional_description=bot_description_str,
            optional_profile=bot_profile_str
        )

        # --- Prepare data for User Prompt ---
        event_dicts = await self.event_storage.get_recent_chat_message_documents(
            conversation_id=self.conversation_id, 
            limit=50, 
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
                    event_obj = Event(
                        event_id=str(event_dict.get('event_id', event_dict.get('_key', str(uuid.uuid4())))),
                        event_type=str(event_dict.get('event_type', 'unknown')),
                        time=float(event_dict.get('timestamp', event_dict.get('time', 0.0))),
                        platform=str(event_dict.get('platform', 'unknown')),
                        bot_id=str(event_dict.get('bot_id', 'unknown')),
                        content=content_segs,
                        user_info=protocol_user_info,
                        conversation_info=protocol_conv_info,
                        raw_data=event_dict.get('raw_data') if isinstance(event_dict.get('raw_data'), dict) else None
                    )
                    raw_events.append(event_obj)
                except Exception as e_conv:
                    logger.error(f"将数据库事件字典转换为Event对象时出错: {e_conv}, dict: {event_dict}", exc_info=True)
        
        user_map: Dict[str, Dict[str, Any]] = {} 
        qq_to_uid_str: Dict[str, str] = {}
        uid_counter = 0
        group_name_str = "未知群聊" 

        qq_to_uid_str[self.bot_qq_id] = "U0"
        user_map[self.bot_qq_id] = {
            "uid_str": "U0",
            "nick": persona_config.bot_name or "机器人",
            "card": persona_config.bot_name or "机器人", 
            "title": getattr(persona_config, 'title', None) or "数字生命体", 
            "perm": "成员" 
        }
        
        if raw_events and raw_events[0].conversation_info:
            conv_info = raw_events[0].conversation_info
            if conv_info.type == "group" and conv_info.name:
                group_name_str = conv_info.name
        
        for event_data in raw_events: 
            if event_data.user_info and event_data.user_info.user_id:
                user_id = event_data.user_info.user_id
                if user_id not in qq_to_uid_str:
                    uid_counter += 1
                    uid_str = f"U{uid_counter}"
                    qq_to_uid_str[user_id] = uid_str
                    user_map[user_id] = {
                        "uid_str": uid_str,
                        "nick": event_data.user_info.user_nickname or f"用户{user_id}",
                        "card": event_data.user_info.user_cardname or (event_data.user_info.user_nickname or f"用户{user_id}"),
                        "title": event_data.user_info.user_titlename or "",
                        "perm": event_data.user_info.permission_level or "成员"
                    }
        
        group_info_block_str = f"- group_name: \"{group_name_str}\""
        
        user_list_lines = []
        sorted_users = sorted(user_map.values(), key=lambda u: int(u["uid_str"][1:]))
        for user_data_item in sorted_users:
            user_qq = ""
            for qq, uid_s in qq_to_uid_str.items(): 
                if uid_s == user_data_item["uid_str"]:
                    user_qq = qq
                    break
            user_identity_suffix = "（你）" if user_data_item["uid_str"] == "U0" else ""
            user_line = f"{user_data_item['uid_str']}: {user_qq}{user_identity_suffix} [nick:{user_data_item['nick']}, card:{user_data_item['card']}, title:{user_data_item['title']}, perm:{user_data_item['perm']}]"
            user_list_lines.append(user_line)
        user_list_block_str = "\n".join(user_list_lines)

        chat_log_lines: List[str] = []
        unread_section_started = False
        last_event_timestamp_for_read_marker = self.last_processed_timestamp
        sorted_events = sorted(raw_events, key=lambda e: e.time)

        for event_data_log in sorted_events: 
            if event_data_log.time > self.last_processed_timestamp and not unread_section_started:
                if chat_log_lines: 
                    read_marker_time_obj = datetime.fromtimestamp(last_event_timestamp_for_read_marker / 1000.0)
                    read_marker_time_str = read_marker_time_obj.strftime("%H:%M:%S")
                    chat_log_lines.append(f"--- 以上消息是你已经思考过的内容，已读 (标记时间: {read_marker_time_str}) ---")
                chat_log_lines.append("--- 请关注以下未读的新消息---")
                unread_section_started = True
            last_event_timestamp_for_read_marker = event_data_log.time

            dt_obj = datetime.fromtimestamp(event_data_log.time / 1000.0)
            time_str = dt_obj.strftime("%H:%M:%S")
            
            user_id_str_log = "SYS" 
            if event_data_log.user_info and event_data_log.user_info.user_id:
                user_id_str_log = qq_to_uid_str.get(event_data_log.user_info.user_id, f"UnknownUser({event_data_log.user_info.user_id[:4]})")

            log_line = ""
            msg_id = event_data_log.get_message_id() or event_data_log.event_id
            is_bot_sent_msg_with_context = False
            # 尝试用当前事件的 msg_id 在 sent_actions_context 中查找
            if user_id_str_log == "U0" and msg_id in self.sent_actions_context:
                is_bot_sent_msg_with_context = True
            
            # 修正标签问题：如果事件是机器人发送的 action.message.sent，也视为普通消息，并打[MSG]标签
            # 但由于ID不匹配，is_bot_sent_msg_with_context 可能仍为False，导致无法附加ACT
            is_bot_originated_message_event = event_data_log.event_type.startswith("message.") or \
                                             (user_id_str_log == "U0" and event_data_log.event_type == "action.message.sent")

            if is_bot_originated_message_event:
                text_content = ""
                current_event_content = event_data_log.content
                # 下面的 if 条件是针对 sent_actions_context 的，如果ID不匹配，is_bot_sent_msg_with_context 会是 False
                # if is_bot_sent_msg_with_context and self.sent_actions_context[msg_id].get("reply_text_segs"):
                #     pass # 暂时不处理用segs恢复文本的逻辑

                for seg in current_event_content:
                    if seg.type == "text": text_content += seg.data.get("text", "")
                    elif seg.type == "image": 
                        img_src = seg.data.get('file_id') or seg.data.get('url', 'unknown_image')
                        text_content += f"[IMG:{img_src.split('/')[-1][:15]}]"
                    elif seg.type == "at":
                        at_user_id = seg.data.get("user_id")
                        at_display_name = seg.data.get("display_name") 
                        if at_user_id in qq_to_uid_str: 
                            at_display_name = qq_to_uid_str[at_user_id]
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
                
                log_line = f"[{time_str}] {user_id_str_log} [MSG]: {text_content.strip()} (id:{msg_id})" # 强制使用 [MSG]
                
                # 只有当ID能匹配上 sent_actions_context 时，才能附加ACT
                if is_bot_sent_msg_with_context: 
                    action_context = self.sent_actions_context[msg_id]
                    motivation = action_context.get("motivation")
                    if motivation:
                        log_line += f"\n    - [ACT]: {motivation}"
            elif event_data_log.event_type == "notice.group.increase":
                operator_id_log, target_id_log = "UnknownOperator", "UnknownTarget"
                if event_data_log.content and len(event_data_log.content) > 0 and event_data_log.content[0].data:
                    op_qq = event_data_log.content[0].data.get("operator_id") 
                    tar_qq = event_data_log.content[0].data.get("target_id")   
                    if op_qq: operator_id_log = qq_to_uid_str.get(op_qq, op_qq)
                    if tar_qq: target_id_log = qq_to_uid_str.get(tar_qq, tar_qq)
                log_line = f"[{time_str}] [SYS]: {operator_id_log}邀请{target_id_log}加入了群聊。"
            elif event_data_log.event_type.startswith("notice.group.decrease"):
                operator_id_log, target_id_log = "UnknownOperator", "UnknownTarget"
                decrease_type = "离开" 
                if event_data_log.content and len(event_data_log.content) > 0 and event_data_log.content[0].data:
                    op_qq = event_data_log.content[0].data.get("operator_id")
                    tar_qq = event_data_log.content[0].data.get("target_id")
                    sub_type = event_data_log.content[0].data.get("sub_type", "leave") 
                    if op_qq: operator_id_log = qq_to_uid_str.get(op_qq, op_qq)
                    if tar_qq: target_id_log = qq_to_uid_str.get(tar_qq, tar_qq)
                    if sub_type == "kick":
                        decrease_type = f"将{target_id_log}移出"
                        log_line = f"[{time_str}] [SYS]: {operator_id_log}{decrease_type}了群聊。"
                    else: 
                        log_line = f"[{time_str}] [SYS]: {target_id_log}{decrease_type}了群聊。"
                else: 
                     log_line = f"[{time_str}] [SYS]: 有成员离开了群聊。"
            elif event_data_log.event_type.startswith("notice.group.admin"): 
                admin_qq = event_data_log.content[0].data.get("user_id") if event_data_log.content and event_data_log.content[0].data else None
                admin_id_log = qq_to_uid_str.get(admin_qq, admin_qq) if admin_qq else "某人"
                set_type = "设置" if event_data_log.content[0].data.get("set") else "取消" 
                log_line = f"[{time_str}] [SYS]: {admin_id_log} 被{set_type}为管理员。"
            elif event_data_log.event_type == "notice.group.name_update":
                new_name = event_data_log.content[0].data.get("new_name", "未知新名称") if event_data_log.content and event_data_log.content[0].data else "未知新名称"
                operator_qq = event_data_log.content[0].data.get("operator_id") if event_data_log.content and event_data_log.content[0].data else None
                operator_id_log = qq_to_uid_str.get(operator_qq, operator_qq) if operator_qq else "有人"
                log_line = f"[{time_str}] [SYS]: {operator_id_log}将群聊名称修改为 \"{new_name}\"。"
            elif event_data_log.event_type == "notice.group.card_update":
                user_qq = event_data_log.content[0].data.get("user_id") if event_data_log.content and event_data_log.content[0].data else None
                user_id_log_card = qq_to_uid_str.get(user_qq, user_qq) if user_qq else "某人"
                new_card = event_data_log.content[0].data.get("new_card", "") if event_data_log.content and event_data_log.content[0].data else ""
                old_card = event_data_log.content[0].data.get("old_card", "") if event_data_log.content and event_data_log.content[0].data else ""
                if new_card:
                    log_line = f"[{time_str}] [SYS]: {user_id_log_card} 的群名片从 \"{old_card}\" 修改为 \"{new_card}\"。"
                else: 
                    log_line = f"[{time_str}] [SYS]: {user_id_log_card} 清空了群名片 (原名片: \"{old_card}\")。"
            elif event_data_log.event_type == "notice.group.ban": 
                operator_qq = event_data_log.content[0].data.get("operator_id") if event_data_log.content and event_data_log.content[0].data else None
                target_qq = event_data_log.content[0].data.get("target_id") if event_data_log.content and event_data_log.content[0].data else None
                duration = event_data_log.content[0].data.get("duration", 0) if event_data_log.content and event_data_log.content[0].data else 0
                operator_id_log = qq_to_uid_str.get(operator_qq, operator_qq) if operator_qq else "管理员"
                target_id_log = qq_to_uid_str.get(target_qq, target_qq) if target_qq else "某人"
                if duration > 0:
                    log_line = f"[{time_str}] [SYS]: {target_id_log} 被 {operator_id_log} 禁言 {duration} 秒。"
                else: 
                    log_line = f"[{time_str}] [SYS]: {target_id_log} 被 {operator_id_log} 解除禁言。"
            elif event_data_log.event_type.startswith("notice.group.recall") or event_data_log.event_type.startswith("notice.friend.recall"):
                operator_qq = event_data_log.content[0].data.get("operator_id") if event_data_log.content and event_data_log.content[0].data else None
                author_qq = event_data_log.content[0].data.get("author_id") if event_data_log.content and event_data_log.content[0].data else None
                operator_id_log = qq_to_uid_str.get(operator_qq, operator_qq) if operator_qq else "管理员"
                author_id_log = qq_to_uid_str.get(author_qq, author_qq) if author_qq else "某人"
                if operator_qq and author_qq and operator_qq == author_qq: 
                    log_line = f"[{time_str}] [SYS]: {author_id_log} 撤回了一条消息。"
                elif operator_qq and author_qq : 
                    log_line = f"[{time_str}] [SYS]: {operator_id_log} 撤回了 {author_id_log} 的一条消息。"
                else: 
                    log_line = f"[{time_str}] [SYS]: 一条消息被撤回。"
            elif event_data_log.event_type == "internal.sub_consciousness.thought_log": 
                motivation_text = extract_text_from_content(event_data_log.content)
                log_line = f"[{time_str}] {user_id_str_log} [ACT]: {motivation_text}"
            else: 
                log_line = f"[{time_str}] {user_id_str_log} [{event_data_log.event_type.split('.')[-1].upper()}]: {extract_text_from_content(event_data_log.content)[:30]}... (id:{event_data_log.event_id})"
            
            if log_line: # This if should be at the same indentation level as the preceding elif/else
                chat_log_lines.append(log_line)
        # This if block should be at the same indentation level as the for loop
        if not unread_section_started and chat_log_lines:
            read_marker_time_obj = datetime.fromtimestamp(last_event_timestamp_for_read_marker / 1000.0)
            read_marker_time_str = read_marker_time_obj.strftime("%H:%M:%S")
            chat_log_lines.append(f"--- 以上消息是你已经思考过的内容，已读 (标记时间: {read_marker_time_str}) ---")
        
        chat_history_log_block_str = "\n".join(chat_log_lines)
        if not chat_history_log_block_str: chat_history_log_block_str = "当前没有聊天记录。"
        
        previous_thoughts_block_str = ""
        if self.last_llm_decision:
            reasoning = self.last_llm_decision.get("reasoning", "")
            reply_text = self.last_llm_decision.get("reply_text")
            motivation = self.last_llm_decision.get("motivation", "") # LLM 返回的动机
            reply_willing = self.last_llm_decision.get("reply_willing", False)
            poke_target = self.last_llm_decision.get("poke")

            action_desc = ""
            if reply_willing and reply_text:
                action_desc = f"发言（发言内容为：{reply_text}）"
            elif reply_willing and not reply_text: # 决定发言但没给内容，不太可能，但做个保护
                action_desc = "决定发言但未提供内容"
            else: # reply_willing is False
                if poke_target:
                    # 尝试从 qq_to_uid_str 和 user_map 获取 poke_target 的显示名
                    poked_user_display = poke_target # 默认为QQ号
                    if poke_target in qq_to_uid_str:
                        poked_user_uid = qq_to_uid_str[poke_target]
                        poked_user_display = f"{poked_user_uid} ({poke_target})"
                    elif poke_target in user_map: # 如果 poke_target 直接是 uid_str (不太可能，LLM应返回QQ)
                         poked_user_display = f"{user_map[poke_target].get('nick', poke_target)} ({poke_target})"

                    action_desc = f"戳一戳 {poked_user_display}"
                # elif self.last_llm_decision.get("action_to_take"): # 以后可以扩展
                #     action_desc = f"执行内部动作：{self.last_llm_decision.get('action_to_take')}"
                else:
                    action_desc = "暂时不发言" # 默认不发言时的动作描述
            
            prev_parts = [f"<previous_thoughts_and_actions>\n刚刚你的内心想法是：\"{reasoning}\""]
            if action_desc: 
                prev_parts.append(f"出于这个想法，你刚才做了：{action_desc}")
            
            # 只有当 motivation 存在且与 action_desc 不同（或者 action_desc 是通用描述如“暂时不发言”）时，才显示“因为”
            if motivation and (action_desc == "暂时不发言" or action_desc.startswith("戳一戳") or not motivation.startswith(action_desc)):
                 if action_desc != motivation: # 避免重复，例如 action_desc 是 "决定观察"，motivation 也是 "决定观察"
                    prev_parts.append(f"因为：{motivation}")
            elif not reply_willing and not poke_target and motivation: # 如果是不发言，没有poke，但有motivation，则显示motivation作为原因
                 prev_parts.append(f"因为：{motivation}")


            prev_parts.append("</previous_thoughts_and_actions>")
            previous_thoughts_block_str = "\n".join(prev_parts)

        user_prompt = USER_PROMPT_TEMPLATE.format(
            group_info_block=group_info_block_str,
            user_list_block=user_list_block_str,
            chat_history_log_block=chat_history_log_block_str,
            previous_thoughts_block=previous_thoughts_block_str
        )
        
        return system_prompt, user_prompt

    async def process_event(self, event: Event):
        if not self.is_active:
            return

        async with self.processing_lock:
            self.last_active_time = time.time()
            
            system_prompt, user_prompt = await self._build_prompt() 
            logger.debug(f"构建的System Prompt:\n{system_prompt}")
            logger.debug(f"构建的User Prompt:\n{user_prompt}")
            
            llm_api_response = await self.llm_client.make_llm_request(
                prompt=user_prompt, 
                system_prompt=system_prompt, 
                is_stream=False
            )
            response_text = llm_api_response.get("text") if llm_api_response else None
            
            if not response_text or (llm_api_response and llm_api_response.get("error")):
                error_msg = llm_api_response.get('message') if llm_api_response else '无响应'
                logger.error(f"[ChatSession][{self.conversation_id}] LLM调用失败或返回空: {error_msg}")
                self.last_llm_decision = {"reasoning": f"LLM调用失败: {error_msg}", "reply_willing": False, "motivation": "系统错误导致无法思考"}
                return
            
            try:
                parsed_response_data = None
                if response_text:
                    match = re.search(r"```json\s*(\{[\s\S]*?\})\s*```", response_text, re.DOTALL)
                    if match:
                        json_str = match.group(1)
                        try:
                            parsed_response_data = json.loads(json_str)
                        except json.JSONDecodeError as e_json_block:
                            logger.error(f"[ChatSession][{self.conversation_id}] 解析被```json包裹的响应时JSONDecodeError: {e_json_block}. JSON string: {json_str[:200]}...")
                            # parsed_response_data remains None
                    else: 
                        try:
                            parsed_response_data = json.loads(response_text)
                        except json.JSONDecodeError as e_json_direct:
                             logger.warning(f"[ChatSession][{self.conversation_id}] LLM响应不是有效的JSON，且未被```json包裹: {response_text[:200]}. Error: {e_json_direct}")
                             # parsed_response_data remains None
                
                if not parsed_response_data:
                    logger.error(f"[ChatSession][{self.conversation_id}] LLM响应最终解析失败或为空。")
                    self.last_llm_decision = {"reasoning": "LLM响应解析失败或为空", "reply_willing": False, "motivation": "系统错误导致无法解析LLM的胡言乱语"}
                    return

                self.last_llm_decision = parsed_response_data 

                if parsed_response_data.get("reply_willing") and parsed_response_data.get("reply_text"):
                    reply_text_content = parsed_response_data["reply_text"]
                    at_target_qq = parsed_response_data.get("at_someone")
                    quote_msg_id = parsed_response_data.get("quote_reply")

                    content_segs_payload: List[Dict[str, Any]] = [] 
                    
                    if quote_msg_id:
                        content_segs_payload.append(SegBuilder.reply(message_id=quote_msg_id).to_dict())
                    
                    at_added_flag = False
                    if at_target_qq:
                        targets_to_at = []
                        if isinstance(at_target_qq, str):
                            targets_to_at = [target.strip() for target in at_target_qq.split(',') if target.strip()]
                        elif isinstance(at_target_qq, list):
                            targets_to_at = [str(target).strip() for target in at_target_qq if str(target).strip()]
                        elif at_target_qq: 
                            targets_to_at = [str(at_target_qq).strip()]

                        for target_qq_id in targets_to_at:
                            if target_qq_id: 
                                content_segs_payload.append(SegBuilder.at(user_id=target_qq_id, display_name="").to_dict())
                                at_added_flag = True
                    
                    if at_added_flag and reply_text_content: 
                        content_segs_payload.append(SegBuilder.text(" ").to_dict())
                    
                    if reply_text_content: 
                        content_segs_payload.append(SegBuilder.text(reply_text_content).to_dict())
                    elif at_added_flag and not reply_text_content: 
                        if not content_segs_payload or \
                           not (content_segs_payload[-1].get("type") == "text" and content_segs_payload[-1].get("data", {}).get("text") == " "):
                            content_segs_payload.append(SegBuilder.text(" ").to_dict())

                    platform_for_action = event.platform 
                    conv_type_for_action = event.conversation_info.type if event.conversation_info else "unknown"

                    action_event_dict = {
                        "event_id": f"sub_chat_reply_{uuid.uuid4()}",
                        "event_type": "action.message.send", 
                        "platform": platform_for_action,
                        "bot_id": self.bot_qq_id,
                        "conversation_info": {"conversation_id": self.conversation_id, "type": conv_type_for_action},
                        "content": content_segs_payload 
                    }
                    
                    logger.info(f"[ChatSession][{self.conversation_id}] Decided to reply: {reply_text_content}")
                    
                    success, msg = await self.action_handler.submit_constructed_action(
                        action_event_dict, 
                        "发送子意识聊天回复"
                    )
                    if success:
                        logger.info(f"[ChatSession][{self.conversation_id}] Action to send reply submitted successfully: {msg}")
                        if parsed_response_data.get("motivation"):
                            action_event_id = action_event_dict['event_id']
                            self.sent_actions_context[action_event_id] = {
                                "motivation": parsed_response_data.get("motivation"),
                                "reply_text": reply_text_content 
                            }
                            if len(self.sent_actions_context) > 10:
                                self.sent_actions_context.popitem(last=False) 
                    else:
                        logger.error(f"[ChatSession][{self.conversation_id}] Failed to submit action to send reply: {msg}")
                else:
                    motivation = parsed_response_data.get("motivation")
                    if motivation:
                        logger.info(f"[ChatSession][{self.conversation_id}] Decided not to reply. Motivation: {motivation}")
                        try:
                            internal_act_event_dict = {
                                "event_id": f"internal_act_{uuid.uuid4()}",
                                "event_type": "internal.sub_consciousness.thought_log",
                                "time": time.time() * 1000, 
                                "platform": self.platform,
                                "bot_id": self.bot_qq_id,
                                "user_info": UserInfo(user_id=self.bot_qq_id, user_nickname=config.persona.bot_name).to_dict(), 
                                "conversation_info": ConversationInfo(conversation_id=self.conversation_id, type=self.conversation_type, platform=self.platform).to_dict(),
                                "content": [SegBuilder.text(motivation).to_dict()]
                            }
                            await self.event_storage.save_event_document(internal_act_event_dict)
                            logger.debug(f"[ChatSession][{self.conversation_id}] Saved internal ACT event for not replying.")
                        except Exception as e_save_act:
                            logger.error(f"[ChatSession][{self.conversation_id}] Failed to save internal ACT event: {e_save_act}", exc_info=True)
                            
                self.last_processed_timestamp = event.time 
            
            except json.JSONDecodeError as e_json:
                logger.error(f"[ChatSession][{self.conversation_id}] Error decoding LLM response JSON: {e_json}. Response text (first 200 chars): {response_text[:200]}...", exc_info=True)
                self.last_llm_decision = {"reasoning": f"Error decoding LLM JSON: {e_json}", "reply_willing": False, "motivation": "System error processing LLM response"}
            except KeyError as e_key:
                logger.error(f"[ChatSession][{self.conversation_id}] Missing key in LLM response: {e_key}. Parsed data: {parsed_response_data if 'parsed_response_data' in locals() else 'N/A'}", exc_info=True)
                self.last_llm_decision = {"reasoning": f"Missing key in LLM response: {e_key}", "reply_willing": False, "motivation": "System error processing LLM response"}
            except AttributeError as e_attr:
                logger.error(f"[ChatSession][{self.conversation_id}] Attribute error while processing LLM response: {e_attr}. Parsed data: {parsed_response_data if 'parsed_response_data' in locals() else 'N/A'}", exc_info=True)
                self.last_llm_decision = {"reasoning": f"Attribute error processing LLM response: {e_attr}", "reply_willing": False, "motivation": "System error processing LLM response"}
            except Exception as e_general: 
                logger.error(f"[ChatSession][{self.conversation_id}] Unexpected error processing LLM response: {e_general}", exc_info=True)
                self.last_llm_decision = {"reasoning": f"Unexpected error: {e_general}", "reply_willing": False, "motivation": "System error processing LLM response"}
