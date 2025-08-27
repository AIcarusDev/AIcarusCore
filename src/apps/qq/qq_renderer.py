# 文件路径: src/apps/qq/qq_renderer.py
# [新增] 这是一个全新的文件，负责所有QQ应用窗口内容的渲染逻辑。

import time
from xml.etree.ElementTree import Element, SubElement

from src.common.utils import parse_entity_uid
from src.os.models import Window, WindowStatus
from src.services.database.services.entity_graph_service import EntityGraphService
from src.services.database.services.event_storage_service import EventStorageService


class QQWindowRenderer:
    """专用于QQ应用的窗口内容渲染器.

    它将从通用的OS层接管所有QQ界面的具体渲染工作，实现职责分离。
    """

    def __init__(
        self,
        entity_service: EntityGraphService,
        event_service: EventStorageService,
        ui_mapping: dict,
        generate_semantic_id: callable,
    ) -> None:
        self.entity_service = entity_service
        self.event_service = event_service
        self.ui_mapping = ui_mapping
        self._generate_semantic_id = generate_semantic_id

    async def render_content(
        self,
        parent_element: Element,
        current_path: list[str],
        window: Window,
        bot_ids_map: dict,
        image_collector: list[dict]
    ) -> None:
        """根据窗口类型，分发到具体的渲染方法."""
        if window.window_class == "main/conversation_list":
            # 会话列表窗口不处理图片，直接传递空的收集器
            await self._render_conversation_list(parent_element, current_path, window, bot_ids_map)
        elif window.window_class == "conversation":
            # 聊天窗口需要处理图片，传递收集器
            await self._render_conversation_window(
                parent_element, current_path, window, bot_ids_map, image_collector
            )

    async def _render_conversation_list(
        self, window_node: Element, current_path: list[str], window: Window, bot_ids_map: dict
    ) -> None:
        # [移动] 此方法逻辑从 AICOSStateGenerator 移动至此
        # (此处的具体实现可以保持原样，核心是职责的转移)
        page = window.content_state.get("page", 1)
        page_size = 30 if window.status == WindowStatus.MAXIMIZE else 15

        bot_id = bot_ids_map.get("qq")
        if not bot_id:
            SubElement(window_node, "error").text = "内部错误：无法获取机器人ID。"
            return

        conversations, total_pages = await self.entity_service.get_paged_conversations(
            platform_id="qq", page=page, page_size=page_size, self_bot_ids={"qq": bot_id}
        )

        list_node = SubElement(
            window_node,
            "conversation_list",
            attrib={
                "pagination": "true",
                "page_current": str(page),
                "page_total": str(total_pages),
                "items_per_page": str(page_size),
            },
        )
        list_path = [*current_path, "conversation_list"]

        if not conversations:
            SubElement(list_node, "desc").text = "没有会话。"
            return

        for conv_data in conversations:
            conv_doc = conv_data.get("conv_doc")
            if not conv_doc:
                continue

            conv_uid = conv_doc._key
            conv_name = conv_doc.details.name or "未知会话"

            conv_path = [*list_path, conv_name]
            conv_node = SubElement(
                list_node,
                "conversation",
                attrib={
                    "title": conv_name,
                    "type": conv_doc.details.type,
                    "unread": str(conv_data.get("unread_count", 0)),
                },
            )

            latest_msg_text = await self.event_service.get_event_text_summary(
                conv_data.get("latest_event")
            )
            SubElement(conv_node, "desc").text = f"[最新消息]: {latest_msg_text}"

            enter_btn_id = self._generate_semantic_id([*conv_path, "enter_button"])
            SubElement(
                conv_node,
                "button",
                attrib={"id": enter_btn_id, "name": "enter", "title": "进入会话"},
            )
            self.ui_mapping[enter_btn_id] = {
                "action_type": "click",
                "action": "open_conversation_window",
                "target_uid": conv_uid,
            }

    async def _render_conversation_window(
        self,
        window_node: Element,
        current_path: list[str],
        window: Window,
        bot_ids_map: dict,
        image_collector: list[dict]
    ) -> None:
        conversation_uid = window.content_state.get("conversation_uid")
        if not conversation_uid:
            SubElement(window_node, "error").text = "无法加载聊天记录：未指定会话ID。"
            return

        # 自我认知
        platform, _, _ = parse_entity_uid(conversation_uid)
        bot_id = bot_ids_map.get(platform)

        # 获取机器人在当前会话的身份信息
        self_presence = await self.entity_service.get_self_presence_in_conversation(
            platform=platform, conversation_entity_uid=conversation_uid
        )
        self_entity = await self.entity_service.get_self_entity_by_platform(platform)

        self_profile_attrs = {
            "name": self_entity.get("details", {}).get("nickname", "未知昵称")
            if self_entity
            else "未知昵称",
            "card": self_presence.get("cardname", "") if self_presence else "",
            "role": self_presence.get("permission_level", "成员") if self_presence else "成员",
        }
        # 过滤掉空的属性
        self_profile_attrs = {k: v for k, v in self_profile_attrs.items() if v}
        SubElement(window_node, "self_profile_in_chat", attrib=self_profile_attrs)

        # ==================== 增强方案: 分页与消息渲染 ====================
        page = window.content_state.get("page", 1)
        page_size = 30 if window.status == WindowStatus.MAXIMIZE else 15

        (messages, current_page, total_pages) = await self.event_service.get_paged_chat_history(
            conversation_uid, page, page_size
        )
        window.content_state["total_pages"] = total_pages

        list_node = SubElement(
            window_node,
            "list",
            attrib={
                "name": "chat_history",
                "pagination": "true",
                "page_current": str(current_page),
                "page_total": str(total_pages),
                "items_per_page": str(page_size),
            },
        )
        list_path = [*current_path, "chat_history"]

        # 渲染向上滚动按钮
        if current_page > 1:
            scroll_up_id = self._generate_semantic_id([*list_path, "scroll_up_button"])
            SubElement(
                list_node,
                "button",
                attrib={"id": scroll_up_id, "name": "scroll_up", "title": "向上滚动查看更早的消息"},
            )
            self.ui_mapping[scroll_up_id] = {
                "action_type": "click",
                "action": "scroll_chat_window",
                "target_uid": window.id,
                "direction": "up",
            }

        # 渲染消息
        for msg in messages:
            msg_sender_id = msg.get("user_info", {}).get("user_id")

            # "发言方向"自我识别
            align = "right" if str(msg_sender_id) == str(bot_id) else "left"

            # 移除多余的 name 属性
            msg_node = SubElement(
                list_node,
                "div",
                attrib={"class": "message", "id": msg.get("event_id"), "align": align},
            )

            sender_name = msg.get("user_info", {}).get("user_cardname") or msg.get(
                "user_info", {}
            ).get("user_nickname", "未知用户")
            timestamp = time.strftime("%H:%M:%S", time.localtime(msg.get("timestamp", 0) / 1000))

            SubElement(msg_node, "sender").text = sender_name
            SubElement(msg_node, "timestamp").text = timestamp

            content_node = SubElement(msg_node, "content")
            await self._render_rich_content(content_node, msg.get("content", []), image_collector)

        # 渲染向下滚动按钮
        if current_page < total_pages:
            scroll_down_id = self._generate_semantic_id([*list_path, "scroll_down_button"])
            SubElement(
                list_node,
                "button",
                attrib={
                    "id": scroll_down_id,
                    "name": "scroll_down",
                    "title": "向下滚动查看最新的消息",
                },
            )
            self.ui_mapping[scroll_down_id] = {
                "action_type": "click",
                "action": "scroll_chat_window",
                "target_uid": window.id,
                "direction": "down",
            }

        action_bar_node = SubElement(window_node, "action_bar")
        SubElement(action_bar_node, "desc").text = "你可以使用 send_message 动作来回复。"

    async def _render_rich_content(
            self,
            content_node: Element,
            segments: list[dict],
            image_collector: list[dict]
        ) -> None:
        """渲染富文本消息内容，处理文本、图片、引用等."""
        for seg in segments:
            seg_type = seg.get("type")
            data = seg.get("data", {})
            if seg_type == "text":
                if content_node.text:
                    content_node.text += data.get("text", "")
                else:
                    content_node.text = data.get("text", "")
            elif seg_type == "image":
                base64_data = data.get("base64")
                if not base64_data:
                    continue

                # 1. 确定占位符文本，区分图片和动画表情
                is_sticker = data.get("summary") == "sticker"
                placeholder_prefix = "[动画表情" if is_sticker else "[图片"

                # 2. 生成唯一的占位符ID (从1开始)
                placeholder_id = len(image_collector) + 1
                placeholder_text = f"{placeholder_prefix}_{placeholder_id}]"

                # 3. 收集图像数据和元信息
                image_info = {
                    "id": placeholder_id,
                    "placeholder": placeholder_text, # 存储占位符本身，方便后续查找
                    "mime_type": data.get("mime_type", "image/png"),
                    "data": base64_data
                }
                image_collector.append(image_info)

                # 4. 在XML中直接将占位符作为文本内容添加
                # 我们不再需要 <media> 标签，因为占位符本身已经足够说明问题
                # 这也让XML更干净，更接近真实客户端的显示
                if content_node.text:
                    content_node.text += placeholder_text
                else:
                    content_node.text = placeholder_text

            elif seg_type == "quote":
                # TODO: 需要通过 message_id 从 event_service 查询被引用的消息详情
                # 为了简化，暂时使用 data 中的信息
                snippet = data.get("text", "...")
                if len(snippet) > 20:
                    snippet = snippet[:20] + "..."
                SubElement(
                    content_node,
                    "quote",
                    attrib={
                        "author": data.get("user_nickname", "未知用户"),
                        "message_id": data.get("message_id", "unknown"),
                        "snippet": snippet,
                    },
                )
