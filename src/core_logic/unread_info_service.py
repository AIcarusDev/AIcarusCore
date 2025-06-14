# src/core_logic/unread_info_service.py
import asyncio
import time 
from typing import List, Dict, Any, Optional, Tuple

from src.database.services.event_storage_service import EventStorageService
from src.database.services.conversation_storage_service import ConversationStorageService
from src.common.custom_logging.logger_manager import get_logger
from aicarus_protocols.common import extract_text_from_content

logger = get_logger("AIcarusCore.CoreLogic.UnreadInfoService")

class UnreadInfoService:
    def __init__(self, event_storage: EventStorageService, conversation_storage: ConversationStorageService):
        self.event_storage = event_storage
        self.conversation_storage = conversation_storage
        self.logger = logger

    async def generate_unread_summary_text(self) -> str:
        self.logger.debug("开始生成未读消息摘要...")
        try:
            unread_events_raw = await self.event_storage.get_unprocessed_message_events(limit=500) 
            self.logger.info(f"从 EventStorageService 获取到 {len(unread_events_raw)} 条未处理的消息事件。")
        except Exception as e:
            self.logger.error(f"调用 get_unprocessed_message_events 失败: {e}", exc_info=True)
            return "获取未读消息失败。"

        if not unread_events_raw:
            return "所有消息均已处理。"

        grouped_events: Dict[str, List[Dict[str, Any]]] = {}
        for event_doc in unread_events_raw:
            conv_id = event_doc.get("conversation_id_extracted")
            if isinstance(conv_id, str) and conv_id: # Ensure conv_id is a non-empty string
                grouped_events.setdefault(conv_id, []).append(event_doc)
            else:
                self.logger.warning(f"事件 {event_doc.get('event_id')} 缺少有效的 conversation_id_extracted。")


        if not grouped_events:
            return "所有消息均已处理。"

        summary_lines: List[str] = ["你有以下未处理的会话新消息:\n"]
        group_chat_summaries: List[str] = []
        private_chat_summaries: List[str] = []

        for conv_id, events_in_conv in grouped_events.items():
            if not events_in_conv: continue

            events_in_conv.sort(key=lambda e: e.get("timestamp", 0), reverse=True)
            latest_event = events_in_conv[0]
            event_id_for_log = latest_event.get('event_id', 'unknown_event')
            
            unread_count = len(events_in_conv)
            
            raw_content_segs = latest_event.get("content", [])
            latest_message_preview = extract_text_from_content(raw_content_segs)
            if not latest_message_preview:
                if any(isinstance(seg, dict) and seg.get("type") == "image" for seg in raw_content_segs): latest_message_preview = "[图片]"
                elif any(isinstance(seg, dict) and seg.get("type") == "face" for seg in raw_content_segs): latest_message_preview = "[表情]"
                elif any(isinstance(seg, dict) and seg.get("type") == "file" for seg in raw_content_segs): latest_message_preview = "[文件]"
                else: latest_message_preview = "无法预览内容"
            if len(latest_message_preview) > 50: latest_message_preview = latest_message_preview[:47] + "..."

            platform_val = latest_event.get("platform")
            platform = platform_val if isinstance(platform_val, str) else "unknown_platform"
            if not isinstance(platform_val, str): self.logger.warning(f"Event {event_id_for_log} platform 不是字符串: {platform_val}")

            event_type_str = latest_event.get("event_type")
            if not isinstance(event_type_str, str):
                self.logger.error(f"Event {event_id_for_log} event_type 不是字符串: {event_type_str}")
                conversation_type_from_event = "unknown_type_error"
            else:
                event_type_parts = event_type_str.split('.')
                conversation_type_from_event = event_type_parts[1] if len(event_type_parts) > 1 else "unknown"

            conv_name = conv_id 
            conversation_document = await self.conversation_storage.get_conversation_document_by_id(conv_id)
            if conversation_document:
                name_from_db = conversation_document.get("name")
                conv_name = name_from_db if isinstance(name_from_db, str) and name_from_db else conv_id
                if not (isinstance(name_from_db, str) and name_from_db): self.logger.warning(f"Conv doc {conv_id} name 不是字符串: {name_from_db}")

                platform_from_db = conversation_document.get("platform")
                if isinstance(platform_from_db, str) and platform_from_db: platform = platform_from_db
                elif platform_from_db is not None: self.logger.warning(f"Conv doc {conv_id} platform 不是字符串: {platform_from_db}")
                
                type_from_db = conversation_document.get("type")
                if isinstance(type_from_db, str) and type_from_db: conversation_type_from_event = type_from_db
                elif type_from_db is not None: self.logger.warning(f"Conv doc {conv_id} type 不是字符串: {type_from_db}")
            else: 
                conv_info = latest_event.get("conversation_info")
                if isinstance(conv_info, dict):
                    name_from_ci = conv_info.get("name")
                    conv_name = name_from_ci if isinstance(name_from_ci, str) and name_from_ci else conv_id
                    if not (isinstance(name_from_ci, str) and name_from_ci): self.logger.warning(f"Event {event_id_for_log} conv_info name 不是字符串: {name_from_ci}")
                elif conv_info is not None:  self.logger.warning(f"Event {event_id_for_log} conversation_info 不是字典: {conv_info}")
            
            user_info_dict = latest_event.get("user_info", {})
            sender_nickname_val = user_info_dict.get("user_nickname") if isinstance(user_info_dict, dict) else None
            sender_nickname = sender_nickname_val if isinstance(sender_nickname_val, str) else "未知用户"
            if not isinstance(sender_nickname_val, str): self.logger.warning(f"Event {event_id_for_log} sender_nickname 不是字符串: {sender_nickname_val}")

            if not isinstance(conversation_type_from_event, str): conversation_type_from_event = "unknown_type_error"
            
            type_display = "群聊" if conversation_type_from_event == "group" else "私聊"
            line_parts = [
                f"- [{type_display}名称]: {conv_name}", f"[ID]: {conv_id}", f"[Platform]: {platform}",
                f"[Type]: {conversation_type_from_event}", f"[最新消息]: \"{sender_nickname}：{latest_message_preview}\"",
                f"(此会话共有 {unread_count} 条未读消息)"
            ]
            line = " ".join(line_parts)
            if conversation_type_from_event == "group": group_chat_summaries.append(line)
            else: private_chat_summaries.append(line)

        if group_chat_summaries: summary_lines.extend(["[群聊消息]"] + group_chat_summaries + [""])
        if private_chat_summaries: summary_lines.extend(["[私聊消息]"] + private_chat_summaries + [""])
        if not group_chat_summaries and not private_chat_summaries: return "所有消息均已处理。"
        return "\n".join(summary_lines).strip()

    async def get_structured_unread_conversations(self) -> List[Dict[str, Any]]:
        self.logger.debug("开始获取结构化的未读会话信息...")
        try:
            unread_events_raw = await self.event_storage.get_unprocessed_message_events(limit=500)
        except Exception as e:
            self.logger.error(f"调用 get_unprocessed_message_events 失败: {e}", exc_info=True)
            return []

        if not unread_events_raw: return []
        grouped_events: Dict[str, List[Dict[str, Any]]] = {}
        for event_doc in unread_events_raw:
            conv_id = event_doc.get("conversation_id_extracted")
            if isinstance(conv_id, str) and conv_id:
                grouped_events.setdefault(conv_id, []).append(event_doc)
            else:
                self.logger.warning(f"事件 {event_doc.get('event_id')} 缺少有效的 conversation_id_extracted。")

        if not grouped_events: return []
        structured_conversations: List[Dict[str, Any]] = []
        for conv_id, events_in_conv in grouped_events.items():
            if not events_in_conv: continue
            events_in_conv.sort(key=lambda e: e.get("timestamp", 0), reverse=True)
            latest_event = events_in_conv[0]
            event_id_for_log = latest_event.get('event_id', 'unknown_event')
            
            unread_count = len(events_in_conv)
            raw_content_segs = latest_event.get("content", [])
            latest_message_preview = extract_text_from_content(raw_content_segs)
            if not latest_message_preview:
                if any(isinstance(seg, dict) and seg.get("type") == "image" for seg in raw_content_segs): latest_message_preview = "[图片]"
                elif any(isinstance(seg, dict) and seg.get("type") == "face" for seg in raw_content_segs): latest_message_preview = "[表情]"
                elif any(isinstance(seg, dict) and seg.get("type") == "file" for seg in raw_content_segs): latest_message_preview = "[文件]"
                else: latest_message_preview = "无法预览内容"
            if len(latest_message_preview) > 50: latest_message_preview = latest_message_preview[:47] + "..."

            platform_val = latest_event.get("platform")
            platform = platform_val if isinstance(platform_val, str) else "unknown_platform"
            if not isinstance(platform_val, str): self.logger.warning(f"Event {event_id_for_log} platform 不是字符串: {platform_val}")

            event_type_str = latest_event.get("event_type")
            if not isinstance(event_type_str, str):
                self.logger.error(f"Event {event_id_for_log} event_type 不是字符串: {event_type_str}, 将使用默认 conversation_type。")
                conversation_type = "unknown_event_type_error"
            else:
                event_type_parts = event_type_str.split('.')
                conversation_type = event_type_parts[1] if len(event_type_parts) > 1 else "unknown"

            conv_name = conv_id
            conversation_document = await self.conversation_storage.get_conversation_document_by_id(conv_id)
            if conversation_document:
                name_from_db = conversation_document.get("name")
                conv_name = name_from_db if isinstance(name_from_db, str) and name_from_db else conv_id
                if not (isinstance(name_from_db, str) and name_from_db): self.logger.warning(f"Conv doc {conv_id} name 不是字符串: {name_from_db}")
                
                platform_from_db = conversation_document.get("platform")
                if isinstance(platform_from_db, str) and platform_from_db: platform = platform_from_db
                elif platform_from_db is not None: self.logger.warning(f"Conv doc {conv_id} platform 不是字符串: {platform_from_db}")
                
                type_from_db = conversation_document.get("type")
                if isinstance(type_from_db, str) and type_from_db: conversation_type = type_from_db
                elif type_from_db is not None: self.logger.warning(f"Conv doc {conv_id} type 不是字符串: {type_from_db}")
            else:
                conv_info = latest_event.get("conversation_info")
                if isinstance(conv_info, dict):
                    name_from_ci = conv_info.get("name")
                    conv_name = name_from_ci if isinstance(name_from_ci, str) and name_from_ci else conv_id
                    if not (isinstance(name_from_ci, str) and name_from_ci): self.logger.warning(f"Event {event_id_for_log} conv_info name 不是字符串: {name_from_ci}")
                    
                    platform_from_ci = conv_info.get("platform")
                    if isinstance(platform_from_ci, str) and platform_from_ci: platform = platform_from_ci
                    elif platform_from_ci is not None: self.logger.warning(f"Event {event_id_for_log} conv_info platform 不是字符串: {platform_from_ci}")
                    
                    type_from_ci = conv_info.get("type")
                    if isinstance(type_from_ci, str) and type_from_ci:
                        conversation_type = type_from_ci
                    elif type_from_ci is not None:
                        self.logger.warning(f"Event {event_id_for_log} conv_info type 不是字符串: {type_from_ci}")
                elif conv_info is not None:
                    self.logger.warning(f"Event {event_id_for_log} conversation_info 不是字典: {conv_info}")
                
                if not conversation_type or conversation_type == "unknown":
                    event_type_str = latest_event.get("event_type")
                    if isinstance(event_type_str, str):
                        event_type_parts = event_type_str.split('.')
                        if len(event_type_parts) > 1:
                            conversation_type = event_type_parts[1]
            
            if not conversation_type or conversation_type == "unknown":
                conversation_type = "private" # 最终回退到 private
                self.logger.warning(f"conv_id '{conv_id}' 的 conversation_type 在所有检查后仍未知，最终回退到 'private'")

            user_info_dict = latest_event.get("user_info", {})
            sender_nickname_val = user_info_dict.get("user_nickname") if isinstance(user_info_dict, dict) else None
            sender_nickname = sender_nickname_val if isinstance(sender_nickname_val, str) else "未知用户"
            if not isinstance(sender_nickname_val, str): self.logger.warning(f"Event {event_id_for_log} sender_nickname 不是字符串: {sender_nickname_val}")

            if not isinstance(conversation_type, str):
                self.logger.error(f"Critical Error: conv_id '{conv_id}' 的 conversation_type 在所有检查后仍不是字符串: {conversation_type} (type: {type(conversation_type)}). Defaulting.")
                conversation_type = "unknown_type_critical_error"
            
            if not isinstance(platform, str): # 确保 platform 也是字符串
                self.logger.error(f"Critical Error: conv_id '{conv_id}' 的 platform 在所有检查后仍不是字符串: {platform} (type: {type(platform)}). Defaulting.")
                platform = "unknown_platform_critical_error"

            if not isinstance(conv_name, str): # 确保 conv_name 也是字符串
                self.logger.error(f"Critical Error: conv_id '{conv_id}' 的 conv_name 在所有检查后仍不是字符串: {conv_name} (type: {type(conv_name)}). Defaulting.")
                conv_name = conv_id


            structured_conversations.append({
                "conversation_id": conv_id, "name": conv_name, "platform": platform,
                "type": conversation_type, "unread_count": unread_count,
                "latest_message_preview": latest_message_preview, "latest_sender_nickname": sender_nickname
            })
            
        self.logger.debug(f"成功生成 {len(structured_conversations)} 条结构化未读会话信息。")
        return structured_conversations
