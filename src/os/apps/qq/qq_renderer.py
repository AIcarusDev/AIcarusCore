# 文件路径: src/os/apps/qq/qq_renderer.py

import base64
import time
from pathlib import Path
from xml.etree.ElementTree import Element, SubElement

from pypinyin import Style, pinyin
from src.common.custom_logging.logging_config import get_logger
from src.common.time_utils import format_relative_time
from src.common.utils import parse_entity_uid
from src.config import config
from src.os.models import Window, WindowStatus
from src.services.database.services.entity_graph_service import EntityGraphService
from src.services.database.services.event_storage_service import EventStorageService

logger = get_logger(__name__)

# 定义一个唯一的常量来标识预览图资源
STICKER_PREVIEW_SOURCE_ID = "qq_stickers_preview"

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

    async def _get_message_priority_tag(self, event: dict, bot_ids_map: dict) -> str:
        """检查事件内容，如果包含@我或回复我，则返回一个高亮标签."""
        all_my_bot_ids = set(bot_ids_map.values())
        for seg in event.get("content", []):
            if not isinstance(seg, dict):
                continue
            seg_type = seg.get("type")
            if seg_type in ("at", "quote"):
                target_user_id = str(seg.get("data", {}).get("user_id", ""))
                if target_user_id in all_my_bot_ids:
                    return "<b>[有人@你]</b>" if seg_type == "at" else "<b>[有人回复你]</b>"
        return ""

    async def _create_message_preview(
        self, event: dict, conv_doc: dict, bot_ids_map: dict
    ) -> tuple[str, str, str]:
        """生成消息预览所需的所有组件 (发送者, 摘要, 优先级标签)."""
        sender_display_name = await self.entity_service.get_sender_display_name_for_event(
            event, conv_doc, bot_ids_map
        )
        priority_tag = await self._get_message_priority_tag(event, bot_ids_map)

        snippet = await self.event_service.get_event_text_summary(event)

        full_sender = (
            f"{priority_tag} {sender_display_name}" if priority_tag else sender_display_name
        )
        return full_sender, snippet, format_relative_time(event.get("timestamp", 0))

    async def render_popup_content(
        self,
        parent_element: Element,
        current_path: list[str],
        window: Window,
    ) -> None:
        """专门渲染QQ新消息弹窗的内容."""
        if window.window_class != "qq_new_message_popup":
            return

        content_node = SubElement(parent_element, "content", attrib={"type": "new_message_alert"})
        state = window.content_state
        SubElement(content_node, "sender_name").text = state.get("sender_name", "未知发件人")
        SubElement(content_node, "message_snippet").text = state.get("message_snippet", "...")

        actions_node = SubElement(content_node, "actions")
        view_btn_path = [*current_path, "content", "view_now"]
        view_btn_id = self._generate_semantic_id(view_btn_path)

        SubElement(
            actions_node,
            "button",
            attrib={"id": view_btn_id, "name": "view_now", "title": "立即查看"},
        )
        self.ui_mapping[view_btn_id] = {
            "action_type": "click",
            "action": "open_conversation_window",
            "target_uid": state.get("target_conversation_uid"),
        }

    async def render_content(
        self,
        parent_element: Element,
        current_path: list[str],
        window: Window,
        bot_ids_map: dict,
        image_collector: list[dict],
    ) -> None:
        """视图分发器."""
        # 优先判断窗口是否为聊天窗口
        if window.window_class == "conversation":
            # 如果是聊天窗口，直接渲染聊天内容，不显示视图切换器
            await self._render_conversation_window(
                parent_element, current_path, window, bot_ids_map, image_collector
            )
            return

        # --- 1. 渲染通用部分：视图切换器 ---
        self._render_view_switcher(parent_element, current_path, window)

        # --- 2. 根据 view 状态分发到不同的渲染方法 ---
        current_view = window.content_state.get("view", "conversation_list")

        if current_view == "conversation_list":
            await self._render_conversation_list(parent_element, current_path, window, bot_ids_map)
        elif current_view == "contacts_list":
            await self._render_contacts_list(parent_element, current_path, window, bot_ids_map)
        elif window.window_class == "conversation":  # 兼容旧的聊天窗口逻辑
            await self._render_conversation_window(
                parent_element, current_path, window, bot_ids_map, image_collector
            )

    def _render_view_switcher(
        self, window_node: Element, current_path: list[str], window: Window
    ) -> None:
        """渲染视图切换按钮栏."""
        current_view = window.content_state.get("view", "conversation_list")
        switcher_node = SubElement(window_node, "view_switcher")
        switcher_path = [*current_path, "view_switcher"]

        views = {"conversation_list": "会话", "contacts_list": "联系人"}
        for view_name, view_title in views.items():
            btn_id = self._generate_semantic_id([*switcher_path, f"{view_name}"])
            btn_status = "active" if current_view == view_name else "inactive"
            SubElement(
                switcher_node,
                "button",
                attrib={"id": btn_id, "name": view_name, "title": view_title, "status": btn_status},
            )
            if btn_status == "inactive":
                self.ui_mapping[btn_id] = {
                    "action_type": "click",
                    "action": "switch_window_view",
                    "target_uid": window.name,
                    "view_name": view_name,
                }

    async def _render_contacts_list(
        self, window_node: Element, current_path: list[str], window: Window, bot_ids_map: dict
    ) -> None:
        await self._render_self_platform_profile(window_node, "qq")

        self_entity = await self.entity_service.get_self_entity_by_platform("qq")
        if not self_entity or not self_entity.get("entity_uid"):
            SubElement(window_node, "error").text = "无法加载联系人：自身实体信息丢失。"
            return

        friends, groups = await self.entity_service.get_all_contacts(self_entity["entity_uid"])

        # 按拼音首字母排序
        def sort_key(item: dict) -> str:
            # 优先使用备注/群名，其次是昵称
            name = item.get("name", "")
            # pinyin返回一个二维列表，例如 [['nǐ'], ['hǎo']]
            pinyin_list = pinyin(name, style=Style.FIRST_LETTER, strict=False)
            # 拼接首字母
            return "".join(part[0] for part in pinyin_list if part).lower()

        friends.sort(key=sort_key)
        groups.sort(key=sort_key)

        contacts_node = SubElement(window_node, "contacts_list")

        # 渲染两个可折叠列表
        await self._render_collapsible_list(
            contacts_node, current_path, window, "friends", "好友", friends
        )
        await self._render_collapsible_list(
            contacts_node, current_path, window, "groups", "群聊", groups
        )

    async def _render_collapsible_list(
        self,
        parent_node: Element,
        current_path: list[str],
        window: Window,
        list_name: str,
        list_title: str,
        items: list[dict],
    ) -> None:
        """通用辅助方法，用于渲染一个可折叠、可分页的列表."""
        list_states = window.content_state.get("collapsible_lists", {})
        status = list_states.get(list_name, "collapsed")

        list_path = [*current_path, list_name]
        list_id = self._generate_semantic_id(list_path)

        list_node = SubElement(
            parent_node,
            "collapsible_list",
            attrib={
                "id": list_id,
                "name": list_name,
                "title": f"{list_title} ({len(items)})",
                "status": status,
            },
        )
        self.ui_mapping[list_id] = {
            "action_type": "click",
            "action": "toggle_collapsible_list",
            "target_uid": window.name,
            "list_name": list_name,
        }

        if status == "expanded":
            page_size = 30 if window.status == WindowStatus.MAXIMIZE else 15
            total_items = len(items)
            total_pages = max(1, (total_items + page_size - 1) // page_size)

            # 存储总页数，供翻页指令使用
            window.content_state.setdefault("list_total_pages", {})[list_name] = total_pages

            current_page = window.content_state.get("list_pages", {}).get(list_name, 1)

            start_index = (current_page - 1) * page_size
            end_index = start_index + page_size
            paginated_items = items[start_index:end_index]

            for item in paginated_items:
                item_node = SubElement(
                    list_node,
                    "item",
                    attrib={
                        "name": item["uid"],
                        "title": item["name"],
                        "type": item["type"],
                    },
                )
                item_path = [*list_path, item["uid"]]
                chat_btn_id = self._generate_semantic_id([*item_path, "chat"])
                SubElement(
                    item_node,
                    "button",
                    attrib={"id": chat_btn_id, "name": "chat", "title": "发起聊天"},
                )
                self.ui_mapping[chat_btn_id] = {
                    "action_type": "click",
                    "action": "open_conversation_window",
                    "target_uid": item["uid"],
                }

            # 渲染分页控件
            pagination_node = SubElement(list_node, "pagination_controls")
            pagination_path = [*list_path, "pagination"]
            if current_page > 1:
                prev_btn_id = self._generate_semantic_id([*pagination_path, "prev"])
                SubElement(
                    pagination_node,
                    "button",
                    attrib={"id": prev_btn_id, "name": "prev_page", "title": "上一页"},
                )
                self.ui_mapping[prev_btn_id] = {
                    "action_type": "click",
                    "action": "paginate_collapsible_list",
                    "target_uid": window.name,
                    "list_name": list_name,
                    "direction": "prev",
                }

            SubElement(pagination_node, "desc").text = f"第 {current_page} / {total_pages} 页"

            if current_page < total_pages:
                next_btn_id = self._generate_semantic_id([*pagination_path, "next"])
                SubElement(
                    pagination_node,
                    "button",
                    attrib={"id": next_btn_id, "name": "next_page", "title": "下一页"},
                )
                self.ui_mapping[next_btn_id] = {
                    "action_type": "click",
                    "action": "paginate_collapsible_list",
                    "target_uid": window.name,
                    "list_name": list_name,
                    "direction": "next",
                }

    async def _render_self_platform_profile(self, window_node: Element, platform_id: str) -> None:
        """负责查询并渲染机器人在指定平台的基础档案（ID和昵称）."""
        self_entity = await self.entity_service.get_self_entity_by_platform(platform_id)
        if self_entity and self_entity.get("details"):
            details = self_entity["details"]
            profile_attrs = {
                "user_id": details.get("platform_id"),
                "nickname": details.get("nickname"),
            }
            SubElement(window_node, "self_profile_on_platform", attrib=profile_attrs)

    async def _render_conversation_list(
        self, window_node: Element, current_path: list[str], window: Window, bot_ids_map: dict
    ) -> None:
        await self._render_self_platform_profile(window_node, "qq")

        page = window.content_state.get("page", 1)
        page_size = 30 if window.status == WindowStatus.MAXIMIZE else 15

        conversations, total_pages = await self.entity_service.get_paged_conversations(
            platform_id="qq", page=page, page_size=page_size, self_bot_ids=bot_ids_map
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
            latest_event = conv_data.get("latest_event")
            if not conv_doc or not latest_event:
                continue

            conv_uid = conv_doc._key
            conv_name = conv_doc.details.name
            conv_path = [*list_path, conv_uid]

            conv_node = SubElement(
                list_node,
                "conversation",
                attrib={
                    "name": conv_uid,
                    "title": conv_name,
                    "type": conv_doc.details.type,
                    "unread": str(conv_data.get("unread_count", 0)),
                },
            )

            # 调用新的预览生成逻辑
            sender, snippet, time_str = await self._create_message_preview(
                latest_event, conv_doc, bot_ids_map
            )

            # 渲染精细化的最新消息
            latest_msg_node = SubElement(conv_node, "latest_message")
            SubElement(latest_msg_node, "sender").text = sender
            SubElement(latest_msg_node, "snippet").text = snippet
            SubElement(latest_msg_node, "time").text = time_str

            # 渲染进入按钮
            enter_btn_id = self._generate_semantic_id([*conv_path, "enter"])
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
        image_collector: list[dict],
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

        # 分页与消息渲染
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
                "target_uid": window.name,
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
                attrib={
                    "class": "message",
                    "id": msg.get("event_id"),
                    "align": align,
                    "sender_id": str(msg_sender_id),
                },
            )

            sender_name = msg.get("user_info", {}).get("user_cardname") or msg.get(
                "user_info", {}
            ).get("user_nickname")
            timestamp = time.strftime("%H:%M:%S", time.localtime(msg.get("timestamp", 0) / 1000))

            SubElement(msg_node, "sender").text = sender_name
            SubElement(msg_node, "timestamp").text = timestamp

            content_node = SubElement(msg_node, "content")
            await self._render_rich_content(content_node, msg.get("content", []), image_collector)

        # 渲染向下滚动按钮
        if current_page < total_pages:
            scroll_down_id = self._generate_semantic_id([*list_path, "scroll_down"])
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
                "target_uid": window.name,
                "direction": "down",
            }

        # 渲染表情包预览图的逻辑
        action_bar_node = SubElement(window_node, "action_bar")
        SubElement(action_bar_node, "desc").text = "你可以使用 send_message 动作来回复。"

        # 检查预览图是否已被其他窗口加载
        sticker_preview_placeholder = None
        existing_preview = next(
            (img for img in image_collector if img.get("source_id") == STICKER_PREVIEW_SOURCE_ID),
            None,
        )

        if existing_preview:
            # 如果已加载，直接复用其占位符
            sticker_preview_placeholder = existing_preview["placeholder"]
            logger.debug(f"复用已加载的表情包预览图: {sticker_preview_placeholder}")
        else:
            # 如果未加载，执行加载流程
            sticker_preview_path = (
                Path(config.runtime_environment.stickers_dir) / "qq_stickers_preview.jpg"
            )
            if sticker_preview_path.exists():
                try:
                    with open(sticker_preview_path, "rb") as f:
                        image_bytes = f.read()

                    base64_data = base64.b64encode(image_bytes).decode("utf-8")

                    placeholder_id = len(image_collector) + 1
                    placeholder_text = f"[表情包收藏预览_{placeholder_id}]"

                    image_info = {
                        "id": placeholder_id,
                        "placeholder": placeholder_text,
                        "mime_type": "image/jpeg",
                        "data": base64_data,
                        "source_id": STICKER_PREVIEW_SOURCE_ID,  # 添加唯一标识符
                    }

                    image_collector.append(image_info)
                    sticker_preview_placeholder = placeholder_text
                    logger.debug(f"首次加载表情包预览图: {sticker_preview_placeholder}")

                except Exception as e:
                    logger.error(f"加载表情包预览图 '{sticker_preview_path}' 失败: {e}")
                    SubElement(
                        action_bar_node, "sticker_collection_preview"
                    ).text = "[预览图加载失败]"

        # 无论如何，都使用获取到的占位符渲染XML
        if sticker_preview_placeholder:
            SubElement(
                action_bar_node,
                "sticker_collection_preview",
                attrib={"src": sticker_preview_placeholder},
            ).text = "你的表情包收藏预览"

    async def _render_rich_content(
        self, content_node: Element, segments: list[dict], image_collector: list[dict]
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

                # 如果有哈希，附加到占位符后面
                image_hash = data.get("hash")
                if image_hash:
                    placeholder_text += f"(hash: {image_hash})"

                image_info = {
                    "id": placeholder_id,
                    "placeholder": placeholder_text,  # 存储占位符本身，方便后续查找
                    "mime_type": data.get("mime_type", "image/png"),
                    "data": base64_data,
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
