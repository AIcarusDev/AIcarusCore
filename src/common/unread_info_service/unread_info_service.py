# src/common/unread_info_service/unread_info_service.py
from collections import defaultdict
from datetime import datetime
from typing import Any

from src.common.custom_logging.logging_config import get_logger
from src.database import ConversationStorageService, EventStorageService

logger = get_logger(__name__)


class UnreadInfoService:
    """未读消息信息服务，用于处理和生成未读消息摘要.

    该服务提供了获取未读消息摘要和结构化未读会话列表的功能，支持排除特定会话ID.

    Attributes:
        event_storage (EventStorageService): 事件存储服务，用于访问消息事件数据.
        conversation_storage (ConversationStorageService): 会话存储服务，用于访问会话数据.
        bot_id (str): 祂的唯一标识符，默认为配置中的QQ ID，如果未设置则为 "unknown_bot_id".
    """

    def __init__(
        self,
        event_storage: EventStorageService,
        conversation_storage: ConversationStorageService,
    ) -> None:
        self.event_storage = event_storage
        self.conversation_storage = conversation_storage
        self.self_bot_ids: dict[str, str] = {}

    def update_self_bot_ids(self, new_bot_ids: dict[str, str]) -> None:
        """从外部更新服务所知的、所有平台上的祂自身的ID.

        这个方法应该在安检流程后被调用.
        """
        self.self_bot_ids.update(new_bot_ids)
        logger.info(f"UnreadInfoService 已更新自身ID列表: {self.self_bot_ids}")

    async def _get_unread_conversations_with_events(
        self, exclude_conversation_id: str | None = None
    ) -> list[tuple[dict[str, Any], list[dict[str, Any]], bool]]:
        """获取所有活跃会话中有新消息的会话列表，排除指定的会话ID.

        Args:
            exclude_conversation_id (str | None): 要排除的会话ID，默认为 None.

        Returns:
            list[tuple[dict[str, Any], list[dict[str, Any]]]]: 有新消息的会话及其
                对应的新消息事件列表.
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
                    # --- 新增的高优事件检测逻辑 ---
                    has_high_priority = False
                    platform = conv_doc.get("platform")
                    bot_id_on_this_platform = self.self_bot_ids.get(platform)

                    if bot_id_on_this_platform:
                        for event in new_events:
                            for seg in event.get("content", []):
                                if (
                                    seg.get("type") == "at"
                                    and str(seg.get("data", {}).get("user_id"))
                                    == bot_id_on_this_platform
                                ) or (
                                    seg.get("type") == "quote"
                                    and str(seg.get("data", {}).get("user_id"))
                                    == bot_id_on_this_platform
                                ):
                                    has_high_priority = True
                                    break
                            if has_high_priority:
                                break
                    # --- 检测逻辑结束 ---

                    logger.info(
                        f"会话 '{conv_id}' 发现 {len(new_events)} 条新未读消息 "
                        f"(高优: {has_high_priority})。"
                    )
                    unread_conversations_with_events.append(
                        (conv_doc, new_events, has_high_priority)
                    )  # <--- 返回三元组

            except Exception as e:
                logger.error(f"为会话 '{conv_id}' 检查新消息时出错: {e}", exc_info=True)
        return unread_conversations_with_events

    def _get_sender_display_name(self, event: dict, conversation_type: str) -> str:
        """获取发送者的显示名称，优先使用群名片或昵称.

        Args:
            event (dict): 消息事件的字典，包含发送者信息.
            conversation_type (str): 会话类型，可能是 "group" 或 "private".

        Returns:
            str: 发送者的显示名称，如果无法获取则返回 "未知用户".
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
        """生成消息预览内容，包含发送者名称和消息摘要.

        Args:
            event (dict): 消息事件的字典，包含消息内容和类型等信息.
            display_name (str): 发送者的显示名称.

        Returns:
            str: 格式化的消息预览字符串，包含发送者名称和消息内容摘要.
        """
        content = event.get("content", [])
        event_type = event.get("event_type", "")
        preview_parts = []
        text_buffer = []
        is_at_me = False
        is_reply_to_me = False

        # 1. 获取当前事件的平台ID
        platform = event.get("platform")

        # 2. 根据平台ID，从我们的“马甲字典”中找到AI在这个平台上的ID
        bot_id_on_this_platform = self.self_bot_ids.get(platform) if platform else None

        # 3. 只有当我们知道AI在这个平台上的ID时，才进行高亮判断
        if bot_id_on_this_platform:
            for seg in content:
                if (
                    seg.get("type") == "at"
                    and str(seg.get("data", {}).get("user_id")) == bot_id_on_this_platform
                ):
                    is_at_me = True
                if (
                    seg.get("type") == "quote"
                    and str(seg.get("data", {}).get("user_id")) == bot_id_on_this_platform
                ):
                    is_reply_to_me = True

        if event_type.endswith("user.poke"):
            target_id = (
                event.get("content", [{}])[0]
                .get("data", {})
                .get("target_user_info", {})
                .get("user_id")
            )
            # 判断戳的是不是我
            if bot_id_on_this_platform and str(target_id) == bot_id_on_this_platform:
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

    async def get_conversation_list_summary(
        self, platform_id: str, exclude_conversation_id: str | None = None
    ) -> str:
        """生成中层所需的、特定平台的会话列表摘要."""
        logger.debug(
            f"开始为平台 '{platform_id}' 生成会话列表摘要... (将排除: {exclude_conversation_id})"
        )
        unread_convs_with_events = await self._get_unread_conversations_with_events(
            exclude_conversation_id
        )

        if not unread_convs_with_events:
            return (
                f"<conversation_list>\n"
                f"  <!-- 在平台 '{platform_id}' 下，没有发现任何其他会话有未读消息。 -->\n"
                f"</conversation_list>"
            )

        # 只处理指定平台的会话
        platform_convs = []
        for conv_doc, events, has_high_priority in unread_convs_with_events: # 使用三元组
            if conv_doc.get("platform") == platform_id:
                platform_convs.append((conv_doc, events, has_high_priority)) # 保存高优标志

        if not platform_convs:
            return (
                f"<conversation_list>\n"
                f"  <!-- 在平台 '{platform_id}' 下，没有发现任何其他会话有未读消息。 -->\n"
                f"</conversation_list>"
            )

        summary_parts = ["<conversation_list>"]
        # 这部分逻辑和 generate_unread_summary_text 很像，但是不包含 <from_platform> 标签
        platform_convs.sort(key=lambda item: item[2], reverse=True)
        group_chats = [c for c in platform_convs if c[0].get("type") == "group"]
        private_chats = [c for c in platform_convs if c[0].get("type") == "private"]

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
                # 在预览字符串中直接体现高优状态
                summary_parts.append(f"- [群名称]：{conv_name}")
                summary_parts.append(f"  - [ID]：{conv_id}")
                summary_parts.append(f"  - [最新消息]：{message_preview}")
                summary_parts.append(f"  - (时间：{time_str}/共 {unread_count} 条未读信息)")
                summary_parts.append("")
            summary_parts.append("</from_group>")

        if private_chats:
            summary_parts.append("<from_private>")
            for conv_doc, events, _ in private_chats:
                conv_id = conv_doc.get("conversation_id", "unknown_id")
                latest_event = events[-1]
                unread_count = len(events)
                timestamp = latest_event.get("timestamp", 0)
                time_str = datetime.fromtimestamp(timestamp / 1000.0).strftime("%H:%M")
                sender_display_name = self._get_sender_display_name(latest_event, "private")
                conv_name = conv_doc.get("name") or sender_display_name
                message_preview = self._create_message_preview(latest_event, sender_display_name)
                summary_parts.append(f"- [用户名称]：{conv_name}")
                summary_parts.append(f"  - [ID]：{conv_id}")
                summary_parts.append(f"  - [最新消息]：{message_preview}")
                summary_parts.append(f"  - (时间：{time_str}/共 {unread_count} 条未读信息)")
                summary_parts.append("")
            summary_parts.append("</from_private>")

        summary_parts.append("</conversation_list>")

        return (
            "\n".join(line for line in summary_parts if line is not None)
            .replace("\n\n\n", "\n\n")
            .strip()
        )

    async def generate_unread_summary_text(self, exclude_conversation_id: str | None = None) -> str:
        """生成最终的、符合你那变态要求的、带XML标签的未读消息摘要."""
        logger.debug(f"开始生成精装修版未读消息摘要... (将排除: {exclude_conversation_id})")
        unread_convs_with_events = await self._get_unread_conversations_with_events(
            exclude_conversation_id
        )

        if not unread_convs_with_events:
            return "所有其他会话均无未读消息。"

        # 按平台分组
        grouped_by_platform = defaultdict(list)
        for conv_doc, events, _ in unread_convs_with_events:
            platform = conv_doc.get("platform", "unknown_platform")
            grouped_by_platform[platform].append((conv_doc, events))

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
                    message_preview = self._create_message_preview(
                        latest_event, sender_display_name
                    )

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
                    latest_event = events[-1]
                    unread_count = len(events)
                    timestamp = latest_event.get("timestamp", 0)
                    time_str = datetime.fromtimestamp(timestamp / 1000.0).strftime("%H:%M")
                    sender_display_name = self._get_sender_display_name(latest_event, "private")
                    conv_name = conv_doc.get("name") or sender_display_name
                    message_preview = self._create_message_preview(
                        latest_event, sender_display_name
                    )
                    summary_parts.append(f"- [用户名称]：{conv_name}")
                    summary_parts.append(f"  - [ID]：{conv_id}")
                    summary_parts.append(f"  - [最新消息]：{message_preview}")
                    summary_parts.append(f"  - (时间：{time_str}/共 {unread_count} 条未读信息)")
                    summary_parts.append("")
                summary_parts.append("</from_private>")

            summary_parts.append(f"</from_{platform}>")

        return (
            "\n".join(line for line in summary_parts if line is not None)
            .replace("\n\n\n", "\n\n")
            .strip()
        )

    async def get_structured_unread_conversations(
        self, exclude_conversation_id: str | None = None
    ) -> list[dict[str, Any]]:
        """获取结构化的未读会话列表，排除指定的会话ID.

        Args:
            exclude_conversation_id (str | None): 要排除的会话ID，默认为 None.

        Returns:
            list[dict[str, Any]]: 结构化的未读会话列表.
        """
        logger.debug(f"正在获取结构化的未读会话列表... (将排除: {exclude_conversation_id})")
        unread_convs_with_events = await self._get_unread_conversations_with_events(
            exclude_conversation_id
        )

        if not unread_convs_with_events:
            return []

        structured_list = []
        for conv_doc, events, _ in unread_convs_with_events:
            latest_event = events[-1]
            sender_name = self._get_sender_display_name(
                latest_event, conv_doc.get("type", "unknown")
            )

            structured_list.append(
                {
                    "conversation_id": conv_doc.get("conversation_id"),
                    "platform": conv_doc.get("platform"),
                    "type": conv_doc.get("type"),
                    "name": conv_doc.get("name") or sender_name,  # 优先用数据库里的名字
                    "unread_count": len(events),
                    "latest_message_preview": self._create_message_preview(
                        latest_event, sender_name
                    ),
                    "latest_timestamp": latest_event.get("timestamp", 0),
                }
            )

        # 按时间倒序排，最新的在最前面，方便 CoreLogic 偷窥
        return sorted(structured_list, key=lambda x: x["latest_timestamp"], reverse=True)

    async def get_platform_summary(self) -> str:
        """生成顶层所需的平台级摘要，能感知高优事件."""
        logger.debug("开始生成平台级摘要...")
        unread_convs_with_events = await self._get_unread_conversations_with_events()

        if not unread_convs_with_events:
            return "所有平台均无新消息。"

        # --- 核心修复点 ---
        platforms_with_news = defaultdict(
            lambda: {"has_high_priority": False, "has_any_news": False}
        )
        for conv_doc, _, has_high_priority in unread_convs_with_events:
            platform = conv_doc.get("platform")
            if platform:
                # 只要有任何新闻，就标记
                platforms_with_news[platform]["has_any_news"] = True
                if has_high_priority:
                    # 如果是高优新闻，再额外标记
                    platforms_with_news[platform]["has_high_priority"] = True
        # --- 修复结束 ---

        if not platforms_with_news:
            return "所有平台均无新消息。"

        summary_lines = []
        for platform, info in sorted(platforms_with_news.items()):
            if info["has_high_priority"]:
                summary_lines.append(f"你的 '{platform}' 上似乎有人找你。")
            elif info["has_any_news"]:  # 现在这个判断才会生效
                summary_lines.append(f"你的 '{platform}' 上似乎有新消息。")

        if not summary_lines:
            return "所有平台均无新消息。"

        return "\n".join(summary_lines)
