# src/common/unread_info_service/unread_info_service.py
from collections import defaultdict
from typing import Any

from src.common.custom_logging.logging_config import get_logger
from src.common.time_utils import format_relative_time
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

    def _get_sender_display_name(self, event: dict, conversation_type: str) -> str:
        """获取发送者的显示名称，优先使用群名片或昵称."""
        user_info = event.get("user_info", {})
        if not isinstance(user_info, dict):
            return "未知用户"

        if remark := user_info.get("extra", {}).get("friend_remark"):
            return remark

        if (
            conversation_type == "group"
            and (card := user_info.get("user_cardname"))
            and isinstance(card, str)
            and card.strip()
        ):
            return card

        if (
            (nickname := user_info.get("user_nickname"))
            and isinstance(nickname, str)
            and nickname.strip()
        ):
            return nickname

        if (user_id := user_info.get("user_id")) and isinstance(user_id, str):
            return f"用户({user_id[-4:]})"

        return "未知用户"

    # --- Refactoring Helper 1: 优先级标签生成器 ---
    def _get_message_priority_tag(self, event: dict) -> str:
        """检查事件内容，如果包含@我或回复我，则返回一个高亮标签."""
        # 直接检查 target_user_id 是否在我们所有的机器人ID中
        all_my_bot_ids = set(self.self_bot_ids.values())

        for seg in event.get("content", []):
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
            # 智能获取 @ 对象的名称
            all_my_bot_ids = set(self.self_bot_ids.values())
            target_id = data.get("user_id")

            if str(target_id) in all_my_bot_ids:
                # 是在 @ 机器人自己
                platform_id = conv_doc.details.platform
                # 1. 优先尝试获取群名片
                presence_info = (
                    await self.entity_graph_service.get_self_presence_in_conversation(
                        platform=platform_id, conversation_entity_uid=conv_doc._key
                    )
                )
                if presence_info and (card := presence_info.get("cardname")):
                    return f"@{card}"

                # 2. 其次尝试获取平台昵称
                self_entity = await self.entity_graph_service.get_self_entity_by_platform(
                    platform_id
                )
                if self_entity and (nickname := self_entity.get("details", {}).get("nickname")):
                    return f"@{nickname}"

                # 3. 如果都失败，这是一个严重问题，必须报错
                logger.critical(
                    f"逻辑错误！无法在会话 '{conv_doc._key}' 中获取机器人自身的群名片或昵称！"
                )
                return "@[数据错误：无法获取名称]"

            # 如果 @ 的是其他人，保持原有逻辑
            return data.get("display_name", f"@{target_id or '某人'}")
        return ""  # 其他未知类型暂时忽略

    # Helper 4: 从消息段列表构建内容预览
    async def _build_content_preview_from_segments(
        self, content: list[dict], conv_doc: EntityDocument
    ) -> str:
        """从事件的 content 字段（消息段列表）构建核心预览字符串."""
        preview_parts = []
        text_buffer = []
        for seg in content:
            # 传递 conv_doc 上下文
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
        # 步骤 1: 使用卫语句处理特殊事件类型
        event_type = event.get("event_type", "")
        if event_type.endswith("user.poke"):
            return self._create_poke_preview(event, display_name)

        # 步骤 2: 获取优先级标签 (@我/回复我)
        priority_tag = self._get_message_priority_tag(event)

        # 步骤 3: 从消息段构建核心内容 (现在是 await 调用)
        content_list = event.get("content", [])
        raw_preview = await self._build_content_preview_from_segments(content_list, conv_doc)

        # 步骤 4: 格式化并截断核心内容
        formatted_preview = self._format_and_truncate_preview(raw_preview)

        # 步骤 5: 组合最终的预览字符串
        final_preview = f"{display_name}：{formatted_preview}"

        # 如果有优先级标签，则加在最前面
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
        conversation_list = await self.entity_graph_service.get_conversations_by_platform(
            platform_uid=platform_id
        )
        platform_convs = [
            c
            for c in all_active_convs
            if c.get("conv_doc") and c["conv_doc"]._key in conversation_list
        ]

        total_count = len(platform_convs)

        # 如果没有找到任何会话，直接返回提示信息
        if not platform_convs:
            return (
                f"<conversation_list>\n"
                f"  <!-- 在平台 '{platform_id}' 下，没有发现任何新消息。 -->\n"
                f"</conversation_list>"
            )

        summary_parts = ["<conversation_list>"]

        # 1. 处理边界情况：总数小于等于页面大小
        if total_count <= page_size:
            summary_parts.append("--- 已经到顶了 ---")
            convs_to_display = platform_convs
            summary_parts.append("--- 没有更多会话 ---")
        else:
            # 2. 计算分页和头尾提示
            start_index = scroll_offset
            end_index = start_index + page_size
            convs_to_display = platform_convs[start_index:end_index]

            # 构造头部提示
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

            # `conv_doc.details` 是 ConversationDetails 对象，直接用 `.` 访问属性
            if not (
                conv_doc
                and hasattr(conv_doc, "details")
                and isinstance(conv_doc.details, ConversationDetails)
            ):
                continue  # 跳过无效的 conv_doc
            conv_details = conv_doc.details
            entity_uid = conv_doc._key
            is_temporary = conv_details.extra.get("is_temporary", False)
            conv_type = conv_details.type
            sender_display_name = self._get_sender_display_name(latest_event, conv_type)

            # 核心逻辑修正：根据会话类型决定名称
            if conv_type == "group":
                conv_name = conv_details.name or f"未知群聊({conv_details.conversation_id})"
            else: # private
                conv_name = conv_details.name or sender_display_name
            # ========================== [FIX END] ==========================

            time_str = format_relative_time(latest_event.get("timestamp", 0))
            # --- [MODIFIED] 调用现在是 await ---
            message_preview = await self._create_message_preview(
                latest_event, sender_display_name, conv_doc
            )

            status_line = f"(时间：{time_str}/共 {unread_count} 条未读信息)"
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
        sender_name = self._get_sender_display_name(event_for_preview, conv_type)
        is_temporary = conv_details.extra.get("is_temporary", False)

        # 核心逻辑修正：根据会话类型决定名称
        if conv_type == "group":
            conv_name = conv_details.name or f"未知群聊({conv_details.conversation_id})"
        else: # private
            conv_name = conv_details.name or sender_name

        time_str = format_relative_time(event_for_preview.get("timestamp", 0))
        # --- [MODIFIED] 调用现在是 await ---
        preview = await self._create_message_preview(event_for_preview, sender_name, conv_doc)

        header = f"- [{'临时会话' if is_temporary else '[用户名称]'}]：{conv_name}"
        if conv_type == "group":
            header = f"- [群名称]：{conv_name}"

        # 生成单个会话的摘要文本列表
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
        items.sort(key=lambda x: x["has_high_priority"], reverse=True)

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
                if item["has_high_priority"]:
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
