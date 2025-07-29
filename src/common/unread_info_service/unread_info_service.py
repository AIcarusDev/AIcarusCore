# src/common/unread_info_service/unread_info_service.py
from collections import defaultdict
from typing import Any

from src.common.custom_logging.logging_config import get_logger
from src.common.time_utils import format_relative_time
from src.database import ConversationStorageService, EventStorageService

logger = get_logger(__name__)


class UnreadInfoService:
    """未读消息信息服务，用于处理和生成未读消息摘要.

    该服务提供了获取未读消息摘要和结构化未读会话列表的功能，支持排除特定会话ID.

    Attributes:
        event_storage (EventStorageService): 事件存储服务，用于访问消息事件数据.
        conversation_storage (ConversationStorageService): 会话存储服务，用于访问会话数据.
        self_bot_ids (dict[str, str]): 祂在不同平台上的ID映射.
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

    async def _get_recently_active_conversations_with_details(
        self, exclude_conversation_id: str | None = None
    ) -> list[dict[str, Any]]:
        """【全新核心方法】获取所有最近活跃的会话及其详细信息.

        这个方法现在通过调用 ConversationStorageService 来获取数据，
        以保持职责分离。

        Args:
            exclude_conversation_id: 要从结果中排除的会话ID。

        Returns:
            一个字典列表，每个字典代表一个会话，包含 'conv_doc', 'latest_event',
            'unread_count', 'has_high_priority'。
        """
        # 调用底层服务时，传入权威的 self.self_bot_ids 字典
        return await self.conversation_storage.get_recently_active_conversations_with_details(
            exclude_conversation_id, self.self_bot_ids
        )

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

        # 1. 先准备好我所有的马甲ID，以备不时之需
        all_my_bot_ids = set(self.self_bot_ids.values())

        for seg in content:
            target_user_id = None
            seg_type = seg.get("type")

            if seg_type == "at" or seg_type == "quote":
                target_user_id = str(seg.get("data", {}).get("user_id", ""))

            if target_user_id:
                # 2. 优先路径：如果事件有平台信息，就精确匹配
                platform = event.get("platform")
                if platform:
                    bot_id_for_this_platform = self.self_bot_ids.get(platform)
                    if bot_id_for_this_platform and target_user_id == bot_id_for_this_platform:
                        if seg_type == "at":
                            is_at_me = True
                        if seg_type == "quote":
                            is_reply_to_me = True
                else:
                    # 3. 回退路径：如果事件没平台信息，就用我所有的马甲去比对
                    #    并且大声抱怨一下！
                    logger.warning(
                        f"事件 (ID: {event.get('_key', '未知')}, "
                        f"Type: {event_type}) 缺少 'platform' 字段！"
                        f"正在进行回退检查..."
                    )
                    if target_user_id in all_my_bot_ids:
                        if seg_type == "at":
                            is_at_me = True
                        if seg_type == "quote":
                            is_reply_to_me = True
                        logger.warning(f"回退检查命中！事件 {event.get('_key')} 确实是@或回复我。")

        if event_type.endswith("user.poke"):
            target_id = (
                event.get("content", [{}])[0]
                .get("data", {})
                .get("target_user_info", {})
                .get("user_id")
            )
            # 判断戳的是不是我
            # 这里也用更健壮的检查
            if target_id and str(target_id) in all_my_bot_ids:
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

        # 1. 调用新的核心方法获取数据
        all_active_convs = await self._get_recently_active_conversations_with_details(
            exclude_conversation_id
        )

        if not all_active_convs:
            return (
                f"<conversation_list>\n"
                f"  <!-- 在平台 '{platform_id}' 下，没有发现任何其他会话有未读消息。 -->\n"
                f"</conversation_list>"
            )

        # 2. 筛选出属于当前平台的会话，并取前10条
        platform_convs = [
            c for c in all_active_convs if c.get("conv_doc", {}).get("platform") == platform_id
        ][:10]

        if not platform_convs:
            return (
                f"<conversation_list>\n"
                f"  <!-- 在平台 '{platform_id}' 下，没有发现任何其他会话有未读消息。 -->\n"
                f"</conversation_list>"
            )

        summary_parts = ["<conversation_list>"]

        # 3. 遍历排序好的会话，构建输出
        for item in platform_convs:
            conv_doc = item["conv_doc"]
            latest_event = item["latest_event"]
            unread_count = item["unread_count"]

            is_temporary = conv_doc.get("extra", {}).get("is_temporary", False)

            conv_id = conv_doc.get("conversation_id", "unknown_id")
            conv_type = conv_doc.get("type")

            sender_display_name = self._get_sender_display_name(latest_event, conv_type)
            conv_name = conv_doc.get("name") or sender_display_name

            timestamp = latest_event.get("timestamp", 0)
            # 使用我们新的相对时间函数
            time_str = format_relative_time(timestamp)
            message_preview = self._create_message_preview(latest_event, sender_display_name)

            # 4. 根据 unread_count 决定状态文本
            if unread_count > 0:
                status_line = f"(时间：{time_str}/共 {unread_count} 条未读信息)"
            else:
                status_line = f"(时间：{time_str}/全部已读)"

            if conv_type == "group":
                summary_parts.append(f"- [群名称]：{conv_name}")
            else:  # private or other
                if is_temporary:
                    summary_parts.append(f"- [临时会话]：{conv_name}")
                else:
                    summary_parts.append(f"- [用户名称]：{conv_name}")
            summary_parts.extend((f"  - [ID]：{conv_id}", f"  - [最新消息]：{message_preview}"))
            summary_parts.extend((f"  - {status_line}", ""))

        summary_parts.append("</conversation_list>")
        return "\n".join(summary_parts).strip()

    async def _format_single_conversation_summary(self, item: dict[str, Any]) -> list[str]:
        """辅助函数: 将单个会话的信息格式化为多行摘要文本列表.

        Args:
            item (dict[str, Any]): 包含会话文档、最新事件和未读计数的字典.

        Returns:
            list[str]: 格式化后的多行文本列表，包含会话的摘要信息.
        """
        conv_doc, latest_event, unread_count, has_high_priority = (
            item["conv_doc"],
            item["latest_event"],
            item["unread_count"],
            item["has_high_priority"],
        )

        event_for_preview = latest_event

        if has_high_priority:
            logger.debug(
                f"检测到会话 '{conv_doc.get('conversation_id')}' 存在高优先级消息，尝试精确查找..."
            )
            last_read_ts = conv_doc.get("last_processed_timestamp", 0)
            # 调用我们刚刚在 EventStorageService 中添加的新方法
            high_priority_event = await self.event_storage.get_latest_high_priority_unread_event(
                conv_doc.get("conversation_id"), last_read_ts, self.self_bot_ids
            )
            if high_priority_event:
                logger.debug(
                    f"已找到高优先级事件 '{high_priority_event.get('_key')}' 用于生成预览。"
                )
                event_for_preview = high_priority_event

        conv_type = conv_doc.get("type", "private")
        sender_name = self._get_sender_display_name(event_for_preview, conv_type)
        # 在这里也使用相对时间
        time_str = format_relative_time(event_for_preview.get("timestamp", 0))
        preview = self._create_message_preview(event_for_preview, sender_name)

        summary_lines = []
        is_temporary = conv_doc.get("extra", {}).get("is_temporary", False)

        if conv_type == "group":
            summary_lines.append(f"- [群名称]：{conv_doc.get('name') or '未知群聊'}")
        else:  # private
            prefix = "[临时会话]" if is_temporary else "[用户名称]"
            summary_lines.append(f"- {prefix}：{conv_doc.get('name') or sender_name}")

        summary_lines.extend(
            [
                f"  - [ID]：{conv_doc.get('conversation_id')}",
                f"  - [最新消息]：{preview}",
                f"  - (时间：{time_str}/共 {unread_count} 条未读信息)",
                "",  # 用于换行
            ]
        )

        return summary_lines

    def _format_chat_type_section(self, chat_type: str, items: list[dict[str, Any]]) -> list[str]:
        """辅助函数: 格式化特定聊天类型（群聊/私聊）的整个XML块.

        Args:
            chat_type (str): 聊天类型，可能是 "group" 或 "private".
            items (list[dict[str, Any]]): 对应聊天类型的会话列表.

        Returns:
            list[str]: 格式化后的XML块，包含每个会话的摘要信息.
        """
        if not items:
            return []

        tag = "from_group" if chat_type == "group" else "from_private"
        section_parts = [f"<{tag}>"]

        for item in items:
            section_parts.extend(self._format_single_conversation_summary(item))

        section_parts.append(f"</{tag}>")
        return section_parts

    def _format_platform_section(self, platform: str, items: list[dict[str, Any]]) -> list[str]:
        """辅助函数: 格式化单个平台的完整XML块.

        Args:
            platform (str): 平台名称.
            items (list[dict[str, Any]]): 平台下的会话列表.

        Returns:
            list[str]: 格式化后的XML块.
        """
        section_parts = [f"<from_{platform}>"]

        # 按高优排序
        items.sort(key=lambda x: x["has_high_priority"], reverse=True)

        group_chats = [c for c in items if c["conv_doc"].get("type") == "group"]
        private_chats = [c for c in items if c["conv_doc"].get("type") == "private"]

        section_parts.extend(self._format_chat_type_section("group", group_chats))
        section_parts.extend(self._format_chat_type_section("private", private_chats))

        section_parts.append(f"</from_{platform}>")
        return section_parts

    async def generate_unread_summary_text(self, exclude_conversation_id: str | None = None) -> str:
        """生成顶层所需的、带XML标签的未读消息摘要.

        这个方法会排除指定的会话ID，并将所有未读消息按平台和聊天类型分组.

        Args:
            exclude_conversation_id (str | None): 要排除的会话ID，默认为None.

        Returns:
            str: 格式化的未读消息摘要，包含XML标签和分组信息.
        """
        logger.debug(f"开始生成精装修版未读消息摘要... (将排除: {exclude_conversation_id})")

        # --- 步骤 1: 获取数据 ---
        unread_convs = [
            item
            for item in await self._get_recently_active_conversations_with_details(
                exclude_conversation_id
            )
            if item["unread_count"] > 0
        ]

        # --- Guard Clause: 卫语句，提前返回，减少嵌套 ---
        if not unread_convs:
            return "所有其他会话均无未读消息。"

        # --- 步骤 2: 数据分组 ---
        grouped_by_platform = defaultdict(list)
        for item in unread_convs:
            platform = item["conv_doc"].get("platform", "unknown_platform")
            grouped_by_platform[platform].append(item)

        # --- 步骤 3: 委托构建并合并结果 ---
        summary_parts = []
        for platform, items in grouped_by_platform.items():
            summary_parts.extend(self._format_platform_section(platform, items))

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

        # 结构增强，现在不仅记录高优，还记录最新事件的时间戳
        platforms_with_news = defaultdict(
            lambda: {"has_high_priority": False, "latest_timestamp": 0}
        )
        for item in unread_convs:
            if platform := item["conv_doc"].get("platform"):
                if item["has_high_priority"]:
                    platforms_with_news[platform]["has_high_priority"] = True

                # 更新最新时间戳
                event_ts = item.get("latest_event", {}).get("timestamp", 0)
                if event_ts > platforms_with_news[platform]["latest_timestamp"]:
                    platforms_with_news[platform]["latest_timestamp"] = event_ts

                # 只要有未读，就标记一下，方便后续统一处理
                platforms_with_news[platform]["has_any_news"] = True

        if not platforms_with_news:
            return "所有平台均无新消息。"

        summary_lines = []
        # 使用新的数据结构来构建更丰富的摘要
        for platform, info in sorted(platforms_with_news.items()):
            relative_time_str = format_relative_time(info["latest_timestamp"])

            # 将相对时间移动到句首，并用方括号包裹
            if info["has_high_priority"]:
                summary_lines.append(f"[{relative_time_str}] 你的 '{platform}' 上似乎有人找你。")
            elif info.get("has_any_news"):
                summary_lines.append(f"[{relative_time_str}] 你的 '{platform}' 上似乎有未读消息。")

        return "\n".join(summary_lines) or "所有平台均无新消息。"
