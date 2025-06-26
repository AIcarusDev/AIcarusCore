# src/core_logic/unread_info_service.py
from typing import Any

from src.common.custom_logging.logger_manager import get_logger
from src.database.services.conversation_storage_service import ConversationStorageService
from src.database.services.event_storage_service import EventStorageService

logger = get_logger("AIcarusCore.CoreLogic.UnreadInfoService")


def _extract_text_from_dict_content(content: list[dict[str, Any]]) -> str:
    """
    从 content (Seg 字典列表) 中安全地提取所有文本内容。
    """
    text_parts = []
    if not isinstance(content, list):
        return ""
    for seg in content:
        if isinstance(seg, dict) and seg.get("type") == "text":
            data = seg.get("data", {})
            if isinstance(data, dict) and "text" in data:
                text_parts.append(str(data["text"]))
    return "".join(text_parts)


class UnreadInfoService:
    def __init__(self, event_storage: EventStorageService, conversation_storage: ConversationStorageService) -> None:
        self.event_storage = event_storage
        self.conversation_storage = conversation_storage

    async def _get_unread_conversations_with_events(self) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
        """
        内部核心方法，获取所有有新消息的会话及其对应的新消息事件列表。
        """
        logger.debug("开始检查所有活跃会话的新消息...")
        try:
            all_conversations = await self.conversation_storage.get_all_active_conversations()
            if not all_conversations:
                logger.info("没有找到任何活跃的会话。")
                return []
        except Exception as e:
            logger.error(f"获取所有活跃会话失败: {e}", exc_info=True)
            return []

        unread_conversations_with_events = []
        for conv_doc in all_conversations:
            conv_id = conv_doc.get("conversation_id")
            if not conv_id:
                continue

            last_processed_ts = conv_doc.get("last_processed_timestamp") or 0
            try:
                # 只获取状态为 "unread" 的新事件
                new_events = await self.event_storage.get_message_events_after_timestamp(
                    conversation_id=conv_id, timestamp=last_processed_ts, status="unread"
                )

                if new_events:
                    logger.info(f"会话 '{conv_id}' 发现 {len(new_events)} 条新消息 (已过滤)。")
                    unread_conversations_with_events.append((conv_doc, new_events))
            except Exception as e:
                logger.error(
                    f"为会话 '{conv_id}' 检查新消息时出错: {e}",
                    exc_info=True,
                )
        return unread_conversations_with_events

    async def generate_unread_summary_text(self) -> str:
        logger.debug("开始生成未读消息摘要...")
        unread_convs_with_events = await self._get_unread_conversations_with_events()

        if not unread_convs_with_events:
            return "所有消息均已读。"

        summary_lines: list[str] = ["你有以下未读的会话新消息:\n"]
        group_chat_summaries: list[str] = []
        private_chat_summaries: list[str] = []

        for conv_doc, events_in_conv in unread_convs_with_events:
            conv_id = conv_doc.get("conversation_id", "unknown_conv_id")
            conv_name = conv_doc.get("name") or conv_id
            platform = conv_doc.get("platform", "unknown_platform")
            conversation_type = conv_doc.get("type", "unknown")

            latest_event = events_in_conv[-1]  # 因为是升序，所以最新消息在最后
            unread_count = len(events_in_conv)

            raw_content_segs = latest_event.get("content", [])
            latest_message_preview = _extract_text_from_dict_content(raw_content_segs)
            if not latest_message_preview:
                if any(isinstance(seg, dict) and seg.get("type") == "image" for seg in raw_content_segs):
                    latest_message_preview = "[图片]"
                elif any(isinstance(seg, dict) and seg.get("type") == "face" for seg in raw_content_segs):
                    latest_message_preview = "[表情]"
                elif any(isinstance(seg, dict) and seg.get("type") == "file" for seg in raw_content_segs):
                    latest_message_preview = "[文件]"
                else:
                    latest_message_preview = "无法预览内容"
            if len(latest_message_preview) > 50:
                latest_message_preview = latest_message_preview[:47] + "..."

            user_info_dict = latest_event.get("user_info", {})
            sender_nickname_val = user_info_dict.get("user_nickname") if isinstance(user_info_dict, dict) else None
            sender_nickname = sender_nickname_val if isinstance(sender_nickname_val, str) else "未知用户"

            type_display = "群聊" if conversation_type == "group" else "私聊"
            line_parts = [
                f"- [{type_display}名称]: {conv_name}",
                f"[ID]: {conv_id}",
                f"[Platform]: {platform}",
                f"[Type]: {conversation_type}",
                f'[最新消息]: "{sender_nickname}：{latest_message_preview}"',
                f"(此会话共有 {unread_count} 条新消息)",
            ]
            line = " ".join(line_parts)
            if conversation_type == "group":
                group_chat_summaries.append(line)
            else:
                private_chat_summaries.append(line)

        if group_chat_summaries:
            summary_lines.extend(["[群聊消息]"] + group_chat_summaries + [""])
        if private_chat_summaries:
            summary_lines.extend(["[私聊消息]"] + private_chat_summaries + [""])

        return "\n".join(summary_lines).strip()

    async def get_structured_unread_conversations(self) -> list[dict[str, Any]]:
        logger.debug("开始获取结构化的未读会话信息...")
        unread_convs_with_events = await self._get_unread_conversations_with_events()

        if not unread_convs_with_events:
            return []

        structured_conversations: list[dict[str, Any]] = []
        for conv_doc, events_in_conv in unread_convs_with_events:
            conv_id = conv_doc.get("conversation_id", "unknown_conv_id")
            conv_name = conv_doc.get("name") or conv_id
            platform = conv_doc.get("platform", "unknown_platform")
            conversation_type = conv_doc.get("type", "unknown")

            latest_event = events_in_conv[-1]
            unread_count = len(events_in_conv)
            latest_message_timestamp = latest_event.get("timestamp", 0)

            raw_content_segs = latest_event.get("content", [])
            latest_message_preview = _extract_text_from_dict_content(raw_content_segs)
            if not latest_message_preview:
                if any(isinstance(seg, dict) and seg.get("type") == "image" for seg in raw_content_segs):
                    latest_message_preview = "[图片]"
                elif any(isinstance(seg, dict) and seg.get("type") == "face" for seg in raw_content_segs):
                    latest_message_preview = "[表情]"
                elif any(isinstance(seg, dict) and seg.get("type") == "file" for seg in raw_content_segs):
                    latest_message_preview = "[文件]"
                else:
                    latest_message_preview = "无法预览内容"
            if len(latest_message_preview) > 50:
                latest_message_preview = latest_message_preview[:47] + "..."

            user_info_dict = latest_event.get("user_info", {})
            sender_nickname_val = user_info_dict.get("user_nickname") if isinstance(user_info_dict, dict) else None
            sender_nickname = sender_nickname_val if isinstance(sender_nickname_val, str) else "未知用户"

            structured_conversations.append(
                {
                    "conversation_id": conv_id,
                    "name": conv_name,
                    "platform": platform,
                    "type": conversation_type,
                    "unread_count": unread_count,
                    "latest_message_preview": latest_message_preview,
                    "latest_sender_nickname": sender_nickname,
                    "latest_message_timestamp": latest_message_timestamp,
                }
            )

        logger.debug(f"成功生成 {len(structured_conversations)} 条结构化未读会话信息。")
        return structured_conversations
