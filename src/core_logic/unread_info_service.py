# src/core_logic/unread_info_service.py
from typing import Any

from src.common.custom_logging.logging_config import get_logger
from src.database.services.conversation_storage_service import ConversationStorageService
from src.database.services.event_storage_service import EventStorageService

logger = get_logger(__name__)


def _create_message_preview(content: list[dict[str, Any]]) -> str:
    """
    哼，这是我的新玩具，专门从消息的 content 字典里提取一个好看的预览。
    它现在能看到 @、图片和表情了，不像以前那个瞎子。
    """
    if not isinstance(content, list):
        return ""

    preview_parts = []
    text_buffer = []

    for seg in content:
        if not isinstance(seg, dict):
            continue

        seg_type = seg.get("type")
        data = seg.get("data", {})

        if seg_type == "text":
            text_buffer.append(data.get("text", ""))
        else:
            if text_buffer:
                preview_parts.append("".join(text_buffer))
                text_buffer = []

            if seg_type == "at":
                at_user_id = data.get("user_id", "all")
                if at_user_id == "all":
                    preview_parts.append("@全体成员")
                else:
                    preview_parts.append(f"@{data.get('display_name', at_user_id)}")
            elif seg_type == "image":
                preview_parts.append("[图片]")
            elif seg_type == "face":
                preview_parts.append("[表情]")
            elif seg_type == "file":
                preview_parts.append("[文件]")

    if text_buffer:
        preview_parts.append("".join(text_buffer))

    full_preview = "".join(preview_parts).strip()

    if len(full_preview) > 50:
        return full_preview[:47] + "..."

    return full_preview or "无法预览内容"


class UnreadInfoService:
    def __init__(self, event_storage: EventStorageService, conversation_storage: ConversationStorageService) -> None:
        self.event_storage = event_storage
        self.conversation_storage = conversation_storage

    async def _get_unread_conversations_with_events(
        self, exclude_conversation_id: str | None = None
    ) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
        """
        内部核心方法，获取所有有新消息的会话及其对应的新消息事件列表。
        哼，我在这里加了个“门禁”，可以把某个讨厌鬼关在门外。
        """
        logger.debug(f"开始检查所有活跃会话的新消息... (将排除: {exclude_conversation_id})")
        try:
            activate_conversations = await self.conversation_storage.get_all_active_conversations()
            if not activate_conversations:
                logger.info("没有找到任何活跃的会话。")
                return []
        except Exception as e:
            logger.error(f"获取所有活跃会话失败: {e}", exc_info=True)
            return []

        unread_conversations_with_events = []
        for conv_doc in activate_conversations:
            conv_id = conv_doc.get("conversation_id")
            if not conv_id:
                continue

            if conv_id == exclude_conversation_id:
                logger.trace(f"已根据 exclude_conversation_id 排除会话: {conv_id}")
                continue

            last_processed_ts = conv_doc.get("last_processed_timestamp") or 0
            try:
                new_events = await self.event_storage.get_message_events_after_timestamp(
                    conversation_id=conv_id, timestamp=last_processed_ts
                )

                if new_events:
                    logger.info(f"会话 '{conv_id}' 发现 {len(new_events)} 条新消息。")
                    unread_conversations_with_events.append((conv_doc, new_events))
            except Exception as e:
                logger.error(f"为会话 '{conv_id}' 检查新消息时出错: {e}", exc_info=True)
        return unread_conversations_with_events

    def _get_display_name_from_event(self, event: dict, conversation_type: str) -> str:
        """
        根据事件和会话类型，智能地获取最佳的显示名称。
        这就是我新加的“智慧核心”！
        """
        user_info = event.get("user_info", {})
        if not isinstance(user_info, dict):
            return "未知用户"

        # 如果是群聊，优先用群名片
        if conversation_type == "group":
            card = user_info.get("user_cardname")
            if card and isinstance(card, str) and card.strip():
                return card

        # 如果没有群名片，或者不是群聊，就用昵称
        nickname = user_info.get("user_nickname")
        if nickname and isinstance(nickname, str) and nickname.strip():
            return nickname

        # 如果连昵称都没有，就用ID做最后的保底
        user_id = user_info.get("user_id")
        if user_id and isinstance(user_id, str):
            return f"用户({user_id[-4:]})"

        return "未知用户"

    async def generate_unread_summary_text(self, exclude_conversation_id: str | None = None) -> str:
        """
        生成未读消息摘要。
        现在它能选择性地忽略一个会话了，哼。
        """
        logger.debug(f"开始生成未读消息摘要... (将排除: {exclude_conversation_id})")
        unread_convs_with_events = await self._get_unread_conversations_with_events(exclude_conversation_id)

        if not unread_convs_with_events:
            return "所有其他会话均无未读消息。"

        summary_lines: list[str] = ["你有以下其他会话的未读消息:\n"]
        group_chat_summaries: list[str] = []
        private_chat_summaries: list[str] = []

        for conv_doc, events_in_conv in unread_convs_with_events:
            conv_id = conv_doc.get("conversation_id", "unknown_conv_id")
            conv_name = conv_doc.get("name") or conv_id
            platform = conv_doc.get("platform", "unknown_platform")
            conversation_type = conv_doc.get("type", "unknown")

            latest_event = events_in_conv[-1]
            unread_count = len(events_in_conv)

            latest_message_preview = _create_message_preview(latest_event.get("content", []))

            # ✨✨✨ 看这里！我用新的智慧核心来决定用哪个名字！ ✨✨✨
            display_name = self._get_display_name_from_event(latest_event, conversation_type)

            type_display = "群聊" if conversation_type == "group" else "私聊"
            line_parts = [
                f"- [{type_display}名称]: {conv_name}",
                f"[ID]: {conv_id}",
                f"[Platform]: {platform}",
                f"[Type]: {conversation_type}",
                f'[最新消息]: "{display_name}：{latest_message_preview}"',
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

    async def get_structured_unread_conversations(
        self, exclude_conversation_id: str | None = None
    ) -> list[dict[str, Any]]:
        """
        获取结构化的未读会话信息。
        这个也得能排除当前会话才行。
        """
        logger.debug(f"开始获取结构化的未读会话信息... (将排除: {exclude_conversation_id})")
        unread_convs_with_events = await self._get_unread_conversations_with_events(exclude_conversation_id)

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

            latest_message_preview = _create_message_preview(latest_event.get("content", []))

            # ✨✨✨ 这里也一样，用新的智慧核心！ ✨✨✨
            sender_display_name = self._get_display_name_from_event(latest_event, conversation_type)

            structured_conversations.append(
                {
                    "conversation_id": conv_id,
                    "name": conv_name,
                    "platform": platform,
                    "type": conversation_type,
                    "unread_count": unread_count,
                    "latest_message_preview": latest_message_preview,
                    "latest_sender_nickname": sender_display_name,  # 注意，字段名还是nickname，但内容已经是我们想要的了
                    "latest_message_timestamp": latest_message_timestamp,
                }
            )

        logger.debug(f"成功生成 {len(structured_conversations)} 条结构化未读会话信息。")
        return structured_conversations
