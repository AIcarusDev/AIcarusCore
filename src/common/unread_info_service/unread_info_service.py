# src/common/unread_info_service/unread_info_service.py
from collections import defaultdict
from datetime import datetime
from typing import Any

from src.common.custom_logging.logging_config import get_logger
from src.config import config
from src.database import ConversationStorageService, EventStorageService

logger = get_logger(__name__)


class UnreadInfoService:
    """
    一个被小懒猫重构过的、专门处理未读信息的服务。
    哼，现在它能生成你想要的、花里胡哨的摘要格式了。
    """

    def __init__(
        self,
        event_storage: EventStorageService,
        conversation_storage: ConversationStorageService,
    ) -> None:
        self.event_storage = event_storage
        self.conversation_storage = conversation_storage
        self.bot_id = config.persona.qq_id or "unknown_bot_id"

    async def _get_unread_conversations_with_events(
        self, exclude_conversation_id: str | None = None
    ) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
        """
        内部核心方法，获取所有有新消息的会话及其对应的新消息事件列表。
        哼，我在这里加了个“门禁”，可以把某个讨厌鬼关在门外。
        """
        logger.debug(f"开始检查所有活跃会话的新消息... (将排除: {exclude_conversation_id})")
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
            if not conv_id or conv_id == "system_events":  # 别把系统事件也当成未读消息
                continue

            if conv_id == exclude_conversation_id:
                logger.trace(f"已根据 exclude_conversation_id 排除会话: {conv_id}")
                continue

            last_processed_ts = conv_doc.get("last_processed_timestamp") or 0
            try:
                # 只获取状态为'unread'的事件
                new_events = await self.event_storage.get_message_events_after_timestamp(
                    conversation_id=conv_id, timestamp=last_processed_ts, status="unread"
                )

                if new_events:
                    logger.info(f"会话 '{conv_id}' 发现 {len(new_events)} 条新未读消息。")
                    unread_conversations_with_events.append((conv_doc, new_events))
            except Exception as e:
                logger.error(f"为会话 '{conv_id}' 检查新消息时出错: {e}", exc_info=True)
        return unread_conversations_with_events

    def _get_sender_display_name(self, event: dict, conversation_type: str) -> str:
        """
        根据事件和会话类型，智能地获取最佳的显示名称。
        这就是我新加的“智慧核心”！
        """
        user_info = event.get("user_info", {})
        if not isinstance(user_info, dict):
            return "未知用户"

        # TODO: 未来在这里加入好友备注的逻辑
        # remark = get_friend_remark(user_info.get("user_id"))
        # if remark:
        #     return remark

        if conversation_type == "group":
            card = user_info.get("user_cardname")
            if card and isinstance(card, str) and card.strip():
                return card

        nickname = user_info.get("user_nickname")
        if nickname and isinstance(nickname, str) and nickname.strip():
            return nickname

        user_id = user_info.get("user_id")
        if user_id and isinstance(user_id, str):
            return f"用户({user_id[-4:]})"

        return "未知用户"

    def _create_message_preview(self, event: dict, display_name: str) -> str:
        """
        我新建的“小作坊”，专门生产那条恶心的“最新消息”预览。
        哼，你那15条破规则，都在这里处理了。
        """
        content = event.get("content", [])
        event_type = event.get("event_type", "")
        preview_parts = []
        text_buffer = []
        is_at_me = False
        is_reply_to_me = False

        if not isinstance(content, list):
            return f"{display_name}：[无法解析的消息内容]"

        # 先检查一下是不是@我或者回复我
        for seg in content:
            if seg.get("type") == "at" and seg.get("data", {}).get("user_id") == self.bot_id:
                is_at_me = True
            if seg.get("type") == "reply" and seg.get("data", {}).get("replied_user_id") == self.bot_id:
                is_reply_to_me = True

        # 再处理戳一戳这种特殊事件
        if event_type in ("user.poke", "group.user.poke", "private.user.poke"):
            target_id = event.get("content", [{}])[0].get("data", {}).get("target_user_info", {}).get("user_id")
            if str(target_id) == self.bot_id:
                return f'{display_name} "戳了戳" 你'
            else:
                target_name = (
                    event.get("content", [{}])[0]
                    .get("data", {})
                    .get("target_user_info", {})
                    .get("user_nickname", "某人")
                )
                return f'{display_name} "戳了戳" {target_name}'

        # 开始组装预览内容
        for seg in content:
            seg_type = seg.get("type")
            data = seg.get("data", {})

            if seg_type == "text":
                text_buffer.append(data.get("text", ""))
            else:
                # 遇到非文本内容，先把之前的文本加进去
                if text_buffer:
                    preview_parts.append("".join(text_buffer))
                    text_buffer = []

                if seg_type == "image":
                    # 检查是不是动画表情
                    if data.get("summary") == "sticker":
                        preview_parts.append("[动画表情]")
                    else:
                        preview_parts.append("[图片]")
                elif seg_type == "at":
                    at_display_name = data.get("display_name", f"@{data.get('user_id', '某人')}")
                    preview_parts.append(at_display_name)
                # 其他类型可以继续加...

        if text_buffer:
            preview_parts.append("".join(text_buffer))

        # 把所有零件拼起来
        full_preview = "".join(preview_parts).strip()

        # 处理换行和截断
        if "\n" in full_preview:
            full_preview = full_preview.split("\n")[0].strip() + "..."
        elif len(full_preview) > 20:
            full_preview = full_preview[:20] + "..."

        if not full_preview:
            full_preview = "[消息]"  # 如果啥也没有，就给个默认的

        # 加上发送者
        final_preview = f"{display_name}：{full_preview}"

        # 加上高亮
        if is_at_me:
            return f"<b>[有人@你]</b> {final_preview}"
        if is_reply_to_me:
            return f"<b>[有人回复你]</b> {final_preview}"

        return final_preview

    async def generate_unread_summary_text(self, exclude_conversation_id: str | None = None) -> str:
        """
        生成最终的、符合你那变态要求的、带XML标签的未读消息摘要。
        """
        logger.debug(f"开始生成精装修版未读消息摘要... (将排除: {exclude_conversation_id})")
        unread_convs_with_events = await self._get_unread_conversations_with_events(exclude_conversation_id)

        if not unread_convs_with_events:
            return "所有其他会话均无未读消息。"

        # 按平台分组
        grouped_by_platform = defaultdict(list)
        for conv_doc, events in unread_convs_with_events:
            platform = conv_doc.get("platform", "unknown_platform")
            grouped_by_platform[platform].append((conv_doc, events))

        # 哼，不加那个多余的 <unread_summary> 了，直接开始！
        summary_parts = []
        for platform, convs in grouped_by_platform.items():
            summary_parts.append(f"<from_{platform}>")

            group_chats = [c for c in convs if c[0].get("type") == "group"]
            private_chats = [c for c in convs if c[0].get("type") == "private"]

            if group_chats:
                summary_parts.append("<from_group>")
                for conv_doc, events in group_chats:
                    conv_id = conv_doc.get("conversation_id", "unknown_id")
                    conv_name = conv_doc.get("name") or "未知群聊"
                    latest_event = events[-1]
                    unread_count = len(events)
                    timestamp = latest_event.get("timestamp", 0)
                    time_str = datetime.fromtimestamp(timestamp / 1000.0).strftime("%H:%M")

                    sender_display_name = self._get_sender_display_name(latest_event, "group")
                    message_preview = self._create_message_preview(latest_event, sender_display_name)

                    summary_parts.append(f"- [群名称]：{conv_name}")
                    summary_parts.append(f"  - [ID]：{conv_id}")
                    summary_parts.append(f"  - [最新消息]：{message_preview}")
                    summary_parts.append(f"  - (时间：{time_str}/共 {unread_count} 条未读信息)")
                    summary_parts.append("")  # 加个空行好看点
                summary_parts.append("</from_group>")

            if private_chats:
                summary_parts.append("<from_private>")
                for conv_doc, events in private_chats:
                    conv_id = conv_doc.get("conversation_id", "unknown_id")
                    # 私聊的发送者就是对方
                    sender_display_name = self._get_sender_display_name(latest_event, "private")

                    # 用发送者的名字作为会话名
                    conv_name = conv_doc.get("name") or sender_display_name

                    latest_event = events[-1]
                    unread_count = len(events)
                    timestamp = latest_event.get("timestamp", 0)
                    time_str = datetime.fromtimestamp(timestamp / 1000.0).strftime("%H:%M")

                    message_preview = self._create_message_preview(latest_event, sender_display_name)

                    summary_parts.append(f"- [用户名称]：{conv_name}")
                    summary_parts.append(f"  - [ID]：{conv_id}")
                    summary_parts.append(f"  - [最新消息]：{message_preview}")
                    summary_parts.append(f"  - (时间：{time_str}/共 {unread_count} 条未读信息)")
                    summary_parts.append("")
                summary_parts.append("</from_private>")

            summary_parts.append(f"</from_{platform}>")

        # 把所有行用换行符合并起来，但是要处理一下空行的问题
        return "\n".join(line for line in summary_parts if line is not None).replace("\n\n\n", "\n\n").strip()

    async def get_structured_unread_conversations(self, exclude_conversation_id: str | None = None) -> list[dict[str, Any]]:
        """
        获取所有有新消息的会话的结构化信息列表。
        这下 CoreLogic 就知道该怎么玩了。
        """
        logger.debug(f"正在获取结构化的未读会话列表... (将排除: {exclude_conversation_id})")
        unread_convs_with_events = await self._get_unread_conversations_with_events(exclude_conversation_id)

        if not unread_convs_with_events:
            return []

        structured_list = []
        for conv_doc, events in unread_convs_with_events:
            # 随便拿一条消息来获取最新的会话名和发送者信息
            latest_event = events[-1]
            sender_name = self._get_sender_display_name(latest_event, conv_doc.get("type", "unknown"))

            structured_list.append({
                "conversation_id": conv_doc.get("conversation_id"),
                "platform": conv_doc.get("platform"),
                "type": conv_doc.get("type"),
                "name": conv_doc.get("name") or sender_name, # 优先用数据库里的名字
                "unread_count": len(events),
                "latest_message_preview": self._create_message_preview(latest_event, sender_name),
                "latest_timestamp": latest_event.get("timestamp", 0)
            })

        # 按时间倒序排，最新的在最前面，方便 CoreLogic 偷窥
        return sorted(structured_list, key=lambda x: x['latest_timestamp'], reverse=True)
