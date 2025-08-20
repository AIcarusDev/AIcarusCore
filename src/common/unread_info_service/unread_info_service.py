# src/common/unread_info_service/unread_info_service.py
import json
import re
from collections import defaultdict
from typing import Any

from src.common.custom_logging.logging_config import get_logger
from src.common.time_utils import format_relative_time
from src.common.utils import build_conversation_entity_uid
from src.database import EntityGraphService, EventStorageService
from src.database.models import ConversationDetails, EntityDocument

logger = get_logger(__name__)


class UnreadInfoService:
    """未读消息信息服务，用于处理和生成未读消息摘要.

    该服务现在依赖于新的 EntityGraphService 来获取所有实体（包括会话）的信息。
    """

    def __init__(
        self,
        event_storage: EventStorageService,
        entity_graph_service: EntityGraphService,
    ) -> None:
        """初始化未读信息服务."""
        self.event_storage = event_storage
        self.entity_graph_service = entity_graph_service
        self.self_bot_ids: dict[str, str] = {}

    def update_self_bot_ids(self, new_bot_ids: dict[str, str]) -> None:
        """从外部更新服务所知的、所有平台上的祂自身的ID."""
        self.self_bot_ids.update(new_bot_ids)
        logger.info(f"UnreadInfoService 已更新自身ID列表: {self.self_bot_ids}")

    async def _get_recently_active_conversations_with_details(
        self, exclude_conversation_id: str | None = None
    ) -> list[dict[str, Any]]:
        """获取所有最近活跃的会话实体及其详细信息."""
        return (
            await self.entity_graph_service.get_recently_active_conversation_entities_with_details(
                exclude_conversation_id, self.self_bot_ids
            )
        )

    async def _get_sender_display_name(self, event: dict, conv_doc: "EntityDocument") -> str:
        """获取会话列表中"发送者"的显示名称.

        规则：
        1) 如果该条最新消息是自己（机器人）发的：
            - 群聊：优先显示自己的 群名片（从数据库查询），其次昵称；都没有时回退为"我"。
            - 私聊：优先好友备注，其次昵称；都没有时回退"我"。
        2) 他人消息：
            - 先好友备注；群聊再看群名片；最后看昵称；都没有时回退"用户(后4位)"/"未知用户"。
        """
        event = event or {}
        user_info = (
            (event.get("user_info") or {}) if isinstance(event.get("user_info"), dict) else {}
        )

        # 从会话文档拿到类型与平台
        details = getattr(conv_doc, "details", None)
        conv_type = getattr(details, "type", None) or (event.get("conversation_info") or {}).get(
            "type"
        )
        platform = getattr(details, "platform", None)

        # 识别"是否自己发送"
        current_sender_id = user_info.get("user_id") or user_info.get("id") or ""
        is_self_sender = bool(
            platform
            and current_sender_id
            and self.self_bot_ids.get(platform) == str(current_sender_id)
        )

        # 常用字段
        friend_remark = (
            (user_info.get("extra") or {}).get("friend_remark")
            if isinstance(user_info.get("extra"), dict)
            else None
        )
        cardname = user_info.get("user_cardname")
        nickname = user_info.get("user_nickname")

        # 情况A：自己发的
        if is_self_sender:
            if conv_type == "group":
                # 群聊：优先从数据库查询群名片，其次使用事件中的昵称，最后回退
                if platform and conv_doc and conv_doc._key:
                    # 查询数据库获取机器人在该群的群名片
                    presence_info = await self.entity_graph_service.get_self_presence_in_conversation(
                        platform=platform,
                        conversation_entity_uid=conv_doc._key,
                    )
                    if presence_info and (group_cardname := presence_info.get("cardname")):
                        return group_cardname

                # 如果没有群名片，使用事件中的昵称
                if isinstance(nickname, str) and nickname.strip():
                    return nickname
                return "我"
            else:
                # 私聊：好友备注 > 昵称 > 我
                if isinstance(friend_remark, str) and friend_remark.strip():
                    return friend_remark
                if isinstance(nickname, str) and nickname.strip():
                    return nickname
                return "我"

        # 情况B：他人发的（原有顺序整理）
        if isinstance(friend_remark, str) and friend_remark.strip():
            return friend_remark
        if conv_type == "group" and isinstance(cardname, str) and cardname.strip():
            return cardname
        if isinstance(nickname, str) and nickname.strip():
            return nickname

        user_id = user_info.get("user_id")
        if isinstance(user_id, str) and user_id:
            return f"用户({user_id[-4:]})"

        return "未知用户"

    # --- Refactoring Helper 1: 优先级标签生成器 ---
    def _get_message_priority_tag(self, event: dict) -> str:
        """检查事件内容，如果包含@我或回复我，则返回一个高亮标签."""
        # 直接检查 target_user_id 是否在我们所有的机器人ID中
        all_my_bot_ids = set(self.self_bot_ids.values())

        for seg in event.get("content", []):
            if not isinstance(seg, dict):
                continue
            seg_type = seg.get("type")
            if seg_type in ("at", "quote"):
                target_user_id = str(seg.get("data", {}).get("user_id", ""))
                if target_user_id in all_my_bot_ids:
                    return "<b>[有人@你]</b>" if seg_type == "at" else "<b>[有人回复你]</b>"
        return ""

    # --- Refactoring Helper 2: 戳一戳事件预览生成器 ---
    def _create_poke_preview(self, event: dict, display_name: str) -> str:
        """专门为戳一戳事件生成预览文本."""
        target_info = event.get("content", [{}])[0].get("data", {}).get("target_user_info", {})
        all_my_bot_ids = set(self.self_bot_ids.values())
        if str(target_info.get("user_id")) in all_my_bot_ids:
            return f'{display_name} "戳了戳" 你'
        return f'{display_name} "戳了戳" {target_info.get("user_nickname", "某人")}'

    # Helper 3: 消息段到文本的转换器 ---
    async def _format_segment_to_text(self, seg: dict, conv_doc: EntityDocument) -> str:
        """将单个消息段(segment)转换为可读的文本预览."""
        seg_type = seg.get("type")
        data = seg.get("data", {})
        if seg_type == "text":
            return data.get("text", "")
        if seg_type == "image":
            return "[动画表情]" if data.get("summary") == "sticker" else "[图片]"
        if seg_type == "at":
            return await self._get_display_name_for_at_segment(data, conv_doc)
        return ""  # 其他未知类型暂时忽略

    async def _get_display_name_for_at_segment(self, data: dict, conv_doc: EntityDocument) -> str:
        """[新增辅助函数] 专门负责解析 @ 消息段，并从数据库获取准确的显示名称."""
        target_id = data.get("user_id")
        if not target_id:
            return "@未知用户"

        # Case 1: @全体成员
        if target_id.lower() == "all":
            return "@全体成员"

        # Case 2: @机器人自己 (使用已有的、更精确的逻辑)
        all_my_bot_ids = set(self.self_bot_ids.values())
        if str(target_id) in all_my_bot_ids:
            platform_id = conv_doc.details.platform
            # 优先尝试获取群名片
            presence_info = await self.entity_graph_service.get_self_presence_in_conversation(
                platform=platform_id, conversation_entity_uid=conv_doc._key
            )
            if presence_info and (card := presence_info.get("cardname")):
                return f"@{card}"
            # 其次尝试获取平台昵称
            self_entity = await self.entity_graph_service.get_self_entity_by_platform(platform_id)
            if self_entity and (nickname := self_entity.get("details", {}).get("nickname")):
                return f"@{nickname}"
            # 最终回退
            return f"@{self.self_bot_ids.get(platform_id, '我')}"

        # Case 3: @其他用户 (这是关键的修复点)
        # 尝试从数据库中查找这个用户的档案来获取名称
        target_entity_uid = build_conversation_entity_uid(
            conv_doc.details.platform, "private", str(target_id)
        )
        target_entity = await self.entity_graph_service.get_entity_by_key(target_entity_uid)

        if target_entity and hasattr(target_entity.details, "nickname"):
            # 优先使用好友备注，其次是昵称
            remark = getattr(target_entity.details, "friend_remark", None)
            nickname = getattr(target_entity.details, "nickname", None)
            if remark:
                return f"@{remark}"
            if nickname:
                return f"@{nickname}"

        # Case 4: 如果数据库查不到，或者协议里有 display_name，使用它作为回退
        if display_name := data.get("display_name"):
            return f"@{display_name}"

        # 最终回退：显示原始ID
        return f"@{target_id}"

    # Helper 4: 从消息段列表构建内容预览
    async def _build_content_preview_from_segments(
        self, content: list[dict] | str, conv_doc: EntityDocument
    ) -> str:
        """从事件的 content 字段（消息段列表）构建核心预览字符串."""
        content_as_list = []
        if isinstance(content, str):
            try:
                parsed_content = json.loads(content)
                if isinstance(parsed_content, list):
                    content_as_list = parsed_content
                else:
                    # --- [PROBE ENHANCEMENT] ---
                    logger.warning(
                        f"会话 '{conv_doc._key}' 的 'content' 字段是一个JSON字符串，但解析后不是列表: "  # noqa: E501
                        f"Type={type(parsed_content)}, Content='{content[:200]}...'"
                    )
                    # --- [PROBE END] ---
            except json.JSONDecodeError:
                logger.warning(
                    f"无法解析会话 '{conv_doc._key}' 的 'content' JSON字符串: '{content[:200]}...'"
                )
                # 尝试用正则表达式从损坏的字符串中提取文本
                try:
                    # 这个正则会查找所有 "text": "..." 的部分并提取其中的内容
                    text_parts = re.findall(r'"text"\s*:\s*"([^"]*)"', content)
                    if text_parts:
                        # 如果找到了，就用它们来构建一个预览
                        logger.debug(f"从损坏的JSON中成功提取到文本: {text_parts}")
                        return "".join(text_parts).strip()
                except Exception:
                    # 如果正则也失败了，就放弃
                    pass
        elif isinstance(content, list):
            content_as_list = content

        preview_parts = []
        text_buffer = []
        for seg in content_as_list:
            if not isinstance(seg, dict):
                continue
            formatted_text = await self._format_segment_to_text(seg, conv_doc)
            if seg.get("type") == "text":
                text_buffer.append(formatted_text)
            else:
                if text_buffer:
                    preview_parts.append("".join(text_buffer))
                    text_buffer = []
                if formatted_text:
                    preview_parts.append(formatted_text)
        if text_buffer:
            preview_parts.append("".join(text_buffer))
        return "".join(preview_parts).strip()

    # --- Refactoring Helper 5: 最终格式化与截断 ---
    def _format_and_truncate_preview(self, text: str) -> str:
        """对生成的预览文本进行最终的格式化和截断处理."""
        if not text or not text.strip():
            return "[消息]"
        if "\n" in text:
            return text.split("\n")[0].strip() + "..."
        return f"{text[:20]}..." if len(text) > 20 else text

    # 主函数
    async def _create_message_preview(
        self, event: dict, display_name: str, conv_doc: EntityDocument
    ) -> str:
        """生成消息预览内容，包含发送者名称和消息摘要 (重构版)."""
        event_type = event.get("event_type", "")
        if event_type.endswith("user.poke"):
            return self._create_poke_preview(event, display_name)

        priority_tag = self._get_message_priority_tag(event)
        content_list = event.get("content", [])
        raw_preview = await self._build_content_preview_from_segments(content_list, conv_doc)
        formatted_preview = self._format_and_truncate_preview(raw_preview)
        final_preview = f"{display_name}：{formatted_preview}"
        return f"{priority_tag} {final_preview}" if priority_tag else final_preview

    async def get_conversation_list_summary(
        self, platform_id: str, scroll_offset: int = 0, page_size: int = 10
    ) -> str:
        """生成特定平台的会话列表摘要，支持分页和头尾提示."""
        all_active_convs = await self._get_recently_active_conversations_with_details()
        if not all_active_convs:
            return (
                f"<conversation_list>\n"
                f"  <!-- 在平台 '{platform_id}' 下，没有发现任何新消息。 -->\n"
                f"</conversation_list>"
            )
        platform_convs = [
            c
            for c in all_active_convs
            if c.get("conv_doc") and c["conv_doc"].details.platform == platform_id
        ]

        total_count = len(platform_convs)
        if not platform_convs:
            return (
                f"<conversation_list>\n"
                f"  <!-- 在平台 '{platform_id}' 下，没有发现任何新消息。 -->\n"
                f"</conversation_list>"
            )

        summary_parts = ["<conversation_list>"]
        if total_count <= page_size:
            summary_parts.append("--- 已经到顶了 ---")
            convs_to_display = platform_convs
            summary_parts.append("--- 没有更多会话 ---")
        else:
            start_index = scroll_offset
            end_index = start_index + page_size
            convs_to_display = platform_convs[start_index:end_index]
            if start_index > 0:
                summary_parts.append(
                    f"<!-- 提示：你可以使用 scroll(params='up') 来查看更多 -->\n"
                    f"--- 上方还有 {start_index} 条未展示的对话 ---"
                )
            else:
                summary_parts.append("--- 已经到顶了 ---")
            remaining_count = total_count - end_index
            footer_text = (
                f"--- 下方还有 {remaining_count} 条未展示的对话 ---\n"
                f"<!-- 提示：你可以使用 scroll(params='down') 来查看更多 -->"
                if remaining_count > 0
                else "--- 已经到底了 ---"
            )
        for item in convs_to_display:
            conv_doc = item["conv_doc"]
            latest_event = item["latest_event"]
            unread_count = item["unread_count"]
            if not (
                conv_doc
                and hasattr(conv_doc, "details")
                and isinstance(conv_doc.details, ConversationDetails)
            ):
                continue
            conv_details = conv_doc.details
            entity_uid = conv_doc._key
            is_temporary = conv_details.extra.get("is_temporary", False)
            conv_type = conv_details.type
            sender_display_name = await self._get_sender_display_name(latest_event, conv_doc)
            if conv_type == "group":
                conv_name = conv_details.name or f"未知群聊({conv_details.conversation_id})"
            else:
                conv_name = conv_details.name or sender_display_name
            time_str = format_relative_time(latest_event.get("timestamp", 0))
            message_preview = await self._create_message_preview(
                latest_event, sender_display_name, conv_doc
            )
            # 优化：当未读数为0时，不显示未读信息部分
            if unread_count > 0:
                status_line = f"(时间：{time_str}/共 {unread_count} 条未读信息)"
            else:
                status_line = f"(时间：{time_str})"
            header = f"- [{'临时会话' if is_temporary else '用户名称'}]：{conv_name}"
            if conv_type == "group":
                header = f"- [群名称]：{conv_name}"
            summary_parts.extend(
                [
                    header,
                    f"  - [ID]：{entity_uid}",
                    f"  - [最新消息]：{message_preview}",
                    f"  - {status_line}",
                    "",
                ]
            )
        if "footer_text" in locals():
            summary_parts.append(footer_text)
        summary_parts.append("</conversation_list>")
        return "\n".join(summary_parts).strip()

    async def _format_single_conversation_summary(self, item: dict[str, Any]) -> list[str]:
        """辅助函数: 将单个会话实体的信息格式化为摘要文本，使用 entity_uid."""
        conv_doc = item["conv_doc"]
        event_for_preview = item["latest_event"]
        unread_count = item["unread_count"]
        entity_uid = conv_doc._key
        if not (
            conv_doc
            and hasattr(conv_doc, "details")
            and isinstance(conv_doc.details, ConversationDetails)
        ):
            return []
        conv_details = conv_doc.details
        conv_type = conv_details.type
        sender_name = await self._get_sender_display_name(event_for_preview, conv_doc)
        is_temporary = conv_details.extra.get("is_temporary", False)
        if conv_type == "group":
            conv_name = conv_details.name or f"未知群聊({conv_details.conversation_id})"
        else:
            conv_name = conv_details.name or sender_name
        time_str = format_relative_time(event_for_preview.get("timestamp", 0))
        preview = await self._create_message_preview(event_for_preview, sender_name, conv_doc)
        header = f"- [{'临时会话' if is_temporary else '[用户名称]'}]：{conv_name}"
        if conv_type == "group":
            header = f"- [群名称]：{conv_name}"
        return [
            header,
            f"  - [ID]：{entity_uid}",
            f"  - [最新消息]：{preview}",
            f"  - (时间：{time_str}/共 {unread_count} 条未读信息)",
            "",
        ]

    async def _format_chat_type_section(
        self, chat_type: str, items: list[dict[str, Any]]
    ) -> list[str]:
        """辅助函数: 格式化特定聊天类型的整个XML块."""
        if not items:
            return []
        tag = "from_group" if chat_type == "group" else "from_private"
        section_parts = [f"<{tag}>"]
        for item in items:
            section_parts.extend(await self._format_single_conversation_summary(item))
        section_parts.append(f"</{tag}>")
        return section_parts

    async def _format_platform_section(
        self, platform: str, items: list[dict[str, Any]]
    ) -> list[str]:
        """辅助函数: 格式化单个平台的完整XML块."""
        section_parts = [f"<from_{platform}>"]
        items.sort(key=lambda x: x.get("has_high_priority", False), reverse=True)
        group_chats = [
            c
            for c in items
            if c.get("conv_doc")
            and hasattr(c["conv_doc"], "details")
            and isinstance(c["conv_doc"].details, ConversationDetails)
            and c["conv_doc"].details.type == "group"
        ]
        private_chats = [
            c
            for c in items
            if c.get("conv_doc")
            and hasattr(c["conv_doc"], "details")
            and isinstance(c["conv_doc"].details, ConversationDetails)
            and c["conv_doc"].details.type == "private"
        ]
        section_parts.extend(await self._format_chat_type_section("group", group_chats))
        section_parts.extend(await self._format_chat_type_section("private", private_chats))
        section_parts.append(f"</from_{platform}>")
        return section_parts

    async def generate_unread_summary_text(self, exclude_conversation_id: str | None = None) -> str:
        """生成顶层所需的、带XML标签的未读消息摘要."""
        all_active_convs = await self._get_recently_active_conversations_with_details(
            exclude_conversation_id
        )
        unread_convs = [item for item in all_active_convs if item.get("unread_count", 0) > 0]
        if not unread_convs:
            return "所有其他会话均无未读消息。"
        grouped_by_platform = defaultdict(list)
        for item in unread_convs:
            conv_doc = item.get("conv_doc")
            if (
                conv_doc
                and hasattr(conv_doc, "details")
                and isinstance(conv_doc.details, ConversationDetails)
            ):
                platform = conv_doc.details.platform
                if platform:
                    grouped_by_platform[platform].append(item)
                else:
                    logger.warning(f"跳过一个缺少 platform 信息的 item: {item}")
            else:
                logger.warning(f"跳过一个缺少 conv_doc 或 details 的 item: {item}")
        summary_parts = []
        for platform, items in grouped_by_platform.items():
            summary_parts.extend(await self._format_platform_section(platform, items))
        return "\n".join(summary_parts).strip()

    async def get_platform_summary(self) -> str:
        """生成顶层所需的平台级摘要，能感知高优事件."""
        logger.debug("开始生成平台级摘要...")
        logger.debug("[PROBE 1] get_platform_summary - 入口")
        all_active_convs = await self._get_recently_active_conversations_with_details()
        unread_convs = [item for item in all_active_convs if item.get("unread_count", 0) > 0]
        if not unread_convs:
            return "所有平台均无新消息。"
        platforms_with_news = defaultdict(
            lambda: {"has_high_priority": False, "latest_timestamp": 0, "has_any_news": False}
        )
        for item in unread_convs:
            conv_doc = item.get("conv_doc")
            if not (conv_doc and conv_doc._key):
                continue
            if (
                hasattr(conv_doc, "details")
                and isinstance(conv_doc.details, ConversationDetails)
                and (platform := conv_doc.details.platform)
            ):
                platforms_with_news[platform]["has_any_news"] = True
                if item.get("has_high_priority"):
                    platforms_with_news[platform]["has_high_priority"] = True
                event_ts = item.get("latest_event", {}).get("timestamp", 0)
                if event_ts > platforms_with_news[platform]["latest_timestamp"]:
                    platforms_with_news[platform]["latest_timestamp"] = event_ts
        if not platforms_with_news:
            return "所有平台均无新消息。"
        summary_lines = []
        for platform, info in sorted(platforms_with_news.items()):
            relative_time_str = format_relative_time(info["latest_timestamp"])
            if info["has_high_priority"]:
                summary_lines.append(f"[{relative_time_str}] 你的 '{platform}' 上似乎有人找你。")
            else:
                summary_lines.append(f"[{relative_time_str}] 你的 '{platform}' 上似乎有未读消息。")
        return "\n".join(summary_lines) or "所有平台均无新消息。"

