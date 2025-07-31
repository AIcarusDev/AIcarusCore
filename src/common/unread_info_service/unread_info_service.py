# src/common/unread_info_service/unread_info_service.py
from collections import defaultdict
from typing import Any

from src.common.custom_logging.logging_config import get_logger
from src.common.time_utils import format_relative_time
from src.database import EntityGraphService, EventStorageService

logger = get_logger(__name__)


class UnreadInfoService:
    """未读消息信息服务，用于处理和生成未读消息摘要.

    该服务现在依赖于新的 EntityGraphService 来获取所有实体（包括会话）的信息。
    """

    def __init__(
        self,
        event_storage: EventStorageService,
        # (±) 依赖注入变更！现在注入的是我们万能的实体图谱服务！
        entity_graph_service: EntityGraphService,
    ) -> None:
        """初始化未读信息服务."""
        self.event_storage = event_storage
        # (±) 存储新神的服务实例
        self.entity_graph_service = entity_graph_service
        self.self_bot_ids: dict[str, str] = {}

    def update_self_bot_ids(self, new_bot_ids: dict[str, str]) -> None:
        """从外部更新服务所知的、所有平台上的祂自身的ID."""
        self.self_bot_ids.update(new_bot_ids)
        logger.info(f"UnreadInfoService 已更新自身ID列表: {self.self_bot_ids}")

    async def _get_recently_active_conversations_with_details(
        self, exclude_conversation_id: str | None = None
    ) -> list[dict[str, Any]]:
        """【核心改造】获取所有最近活跃的会话实体及其详细信息.

        这个方法现在直接调用 EntityGraphService 的新方法，获取统一的实体数据。
        """
        # // 看！现在它直接向新神祈祷，获取神谕！
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

    def _create_message_preview(self, event: dict, display_name: str) -> str:
        """生成消息预览内容，包含发送者名称和消息摘要."""
        # ... (此方法内部逻辑不变，因为它只处理事件内容) ...
        content = event.get("content", [])
        event_type = event.get("event_type", "")
        preview_parts = []
        text_buffer = []
        is_at_me = False
        is_reply_to_me = False
        all_my_bot_ids = set(self.self_bot_ids.values())

        for seg in content:
            target_user_id = None
            seg_type = seg.get("type")

            if seg_type in ("at", "quote"):
                target_user_id = str(seg.get("data", {}).get("user_id", ""))

            if target_user_id:
                if platform := event.get("platform"):
                    if (bot_id := self.self_bot_ids.get(platform)) and target_user_id == bot_id:
                        if seg_type == "at":
                            is_at_me = True
                        if seg_type == "quote":
                            is_reply_to_me = True
                elif target_user_id in all_my_bot_ids:
                    logger.warning(f"事件 {event.get('_key')} 缺少platform，回退检查命中！")
                    if seg_type == "at":
                        is_at_me = True
                    if seg_type == "quote":
                        is_reply_to_me = True

        if event_type.endswith("user.poke"):
            target_info = event.get("content", [{}])[0].get("data", {}).get("target_user_info", {})
            if str(target_info.get("user_id")) in all_my_bot_ids:
                return f'{display_name} "戳了戳" 你'
            return f'{display_name} "戳了戳" {target_info.get("user_nickname", "某人")}'

        for seg in content:
            seg_type = seg.get("type")
            data = seg.get("data", {})
            if seg_type == "text":
                text_buffer.append(data.get("text", ""))
            else:
                if text_buffer:
                    preview_parts.append("".join(text_buffer))
                    text_buffer = []
                if seg_type == "image":
                    preview_parts.append(
                        "[动画表情]" if data.get("summary") == "sticker" else "[图片]"
                    )
                elif seg_type == "at":
                    preview_parts.append(
                        data.get("display_name", f"@{data.get('user_id', '某人')}")
                    )

        if text_buffer:
            preview_parts.append("".join(text_buffer))

        full_preview = "".join(preview_parts).strip()
        if "\n" in full_preview:
            full_preview = full_preview.split("\n")[0].strip() + "..."
        elif len(full_preview) > 20:
            full_preview = full_preview[:20] + "..."
        full_preview = full_preview or "[消息]"

        final_preview = f"{display_name}：{full_preview}"
        if is_at_me:
            return f"<b>[有人@你]</b> {final_preview}"
        if is_reply_to_me:
            return f"<b>[有人回复你]</b> {final_preview}"
        return final_preview

    async def get_conversation_list_summary(
        self, platform_id: str, exclude_conversation_id: str | None = None
    ) -> str:
        """生成中层所需的、特定平台的会话列表摘要."""
        all_active_convs = await self._get_recently_active_conversations_with_details(
            exclude_conversation_id
        )
        if not all_active_convs:
            return f"<conversation_list>\n  <!-- 在平台 '{platform_id}' 下，没有发现任何其他会话有未读消息。 -->\n</conversation_list>"  # noqa: E501

        # (±) 核心适配点：从 entity.details 中获取 platform
        platform_convs = [
            c
            for c in all_active_convs
            if c.get("conv_doc", {}).get("details", {}).get("platform") == platform_id
        ][:10]

        if not platform_convs:
            return f"<conversation_list>\n  <!-- 在平台 '{platform_id}' 下，没有发现任何其他会话有未读消息。 -->\n</conversation_list>"  # noqa: E501

        summary_parts = ["<conversation_list>"]
        for item in platform_convs:
            conv_doc = item["conv_doc"]
            latest_event = item["latest_event"]
            unread_count = item["unread_count"]
            # (±) 核心适配点：所有会话信息都从 entity.details 中来
            conv_details = conv_doc.get("details", {})

            is_temporary = conv_details.get("extra", {}).get("is_temporary", False)
            conv_id = conv_details.get("conversation_id", "unknown_id")
            conv_type = conv_details.get("type")
            sender_display_name = self._get_sender_display_name(latest_event, conv_type)
            conv_name = conv_details.get("name") or sender_display_name
            time_str = format_relative_time(latest_event.get("timestamp", 0))
            message_preview = self._create_message_preview(latest_event, sender_display_name)

            status_line = (
                f"(时间：{time_str}/共 {unread_count} 条未读信息)"
                if unread_count > 0
                else f"(时间：{time_str}/全部已读)"
            )

            if conv_type == "group":
                summary_parts.append(f"- [群名称]：{conv_name}")
            else:
                summary_parts.append(
                    f"- [{'临时会话' if is_temporary else '用户名称'}]：{conv_name}"
                )

            summary_parts.extend(
                [
                    f"  - [ID]：{conv_id}",
                    f"  - [最新消息]：{message_preview}",
                    f"  - {status_line}",
                    "",
                ]
            )
        summary_parts.append("</conversation_list>")
        return "\n".join(summary_parts).strip()

    async def _format_single_conversation_summary(self, item: dict[str, Any]) -> list[str]:
        """辅助函数: 将单个会话实体的信息格式化为摘要文本."""
        conv_doc = item["conv_doc"]
        latest_event = item["latest_event"]
        unread_count = item["unread_count"]
        has_high_priority = item["has_high_priority"]

        event_for_preview = latest_event
        # (±) 核心适配点：所有会话信息都从 entity.details 中来
        conv_details = conv_doc.get("details", {})
        conv_id_for_query = conv_details.get("conversation_id")

        if has_high_priority and conv_id_for_query:
            logger.debug(f"会话 '{conv_id_for_query}' 存在高优消息，精确查找...")
            # TODO: last_read_ts 应该从一个专门的状态管理服务获取，暂时用0
            high_priority_event = await self.event_storage.get_latest_high_priority_unread_event(
                conv_id_for_query, 0, self.self_bot_ids
            )
            if high_priority_event:
                event_for_preview = high_priority_event

        conv_type = conv_details.get("type", "private")
        sender_name = self._get_sender_display_name(event_for_preview, conv_type)
        time_str = format_relative_time(event_for_preview.get("timestamp", 0))
        preview = self._create_message_preview(event_for_preview, sender_name)
        is_temporary = conv_details.get("extra", {}).get("is_temporary", False)

        if conv_type == "group":
            header = f"- [群名称]：{conv_details.get('name') or '未知群聊'}"
        else:
            prefix = "[临时会话]" if is_temporary else "[用户名称]"
            header = f"- {prefix}：{conv_details.get('name') or sender_name}"

        return [
            header,
            f"  - [ID]：{conv_details.get('conversation_id')}",
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
        # (±) 核心适配点：从 entity.details 中获取 type
        group_chats = [c for c in items if c["conv_doc"].get("details", {}).get("type") == "group"]
        private_chats = [
            c for c in items if c["conv_doc"].get("details", {}).get("type") == "private"
        ]
        section_parts.extend(await self._format_chat_type_section("group", group_chats))
        section_parts.extend(await self._format_chat_type_section("private", private_chats))
        section_parts.append(f"</from_{platform}>")
        return section_parts

    async def generate_unread_summary_text(self, exclude_conversation_id: str | None = None) -> str:
        """生成顶层所需的、带XML标签的未读消息摘要."""
        logger.debug(f"开始生成精装修版未读消息摘要... (将排除: {exclude_conversation_id})")
        unread_convs = [
            item
            for item in await self._get_recently_active_conversations_with_details(
                exclude_conversation_id
            )
            if item["unread_count"] > 0
        ]
        if not unread_convs:
            return "所有其他会话均无未读消息。"

        grouped_by_platform = defaultdict(list)
        for item in unread_convs:
            # (±) 核心适配点：从 entity.details 中获取 platform
            platform = item["conv_doc"].get("details", {}).get("platform", "unknown_platform")
            grouped_by_platform[platform].append(item)

        summary_parts = []
        for platform, items in grouped_by_platform.items():
            summary_parts.extend(await self._format_platform_section(platform, items))
        return "\n".join(summary_parts).strip()

    async def get_platform_summary(self) -> str:
        """生成顶层所需的平台级摘要，能感知高优事件."""
        logger.debug("开始生成平台级摘要...")
        unread_convs = [
            item
            for item in await self._get_recently_active_conversations_with_details()
            if item["unread_count"] > 0
        ]
        if not unread_convs:
            return "所有平台均无新消息。"

        platforms_with_news = defaultdict(
            lambda: {"has_high_priority": False, "latest_timestamp": 0, "has_any_news": False}
        )
        for item in unread_convs:
            # (±) 核心适配点：从 entity.details 中获取 platform
            if platform := item["conv_doc"].get("details", {}).get("platform"):
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
            else:  # has_any_news is guaranteed to be true here
                summary_lines.append(f"[{relative_time_str}] 你的 '{platform}' 上似乎有未读消息。")

        return "\n".join(summary_lines) or "所有平台均无新消息。"
