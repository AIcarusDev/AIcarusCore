# 文件路径: src/os/apps/qq/qq_renderer.py

import base64
import time
from pathlib import Path
from typing import TYPE_CHECKING
from xml.etree.ElementTree import Element, SubElement

from pypinyin import Style, pinyin
from src.common.custom_logging.logging_config import get_logger
from src.common.time_utils import format_relative_time
from src.common.utils import parse_entity_uid
from src.config import config
from src.os.models import Window, WindowStatus
from src.services.database.services.entity_graph_service import EntityGraphService
from src.services.database.services.event_storage_service import EventStorageService

if TYPE_CHECKING:
    from src.services.action.action_handler import ActionHandler
    from src.services.database.services.media_cache_service import MediaCacheService


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
        media_cache_service: "MediaCacheService",
        action_handler: "ActionHandler",
    ) -> None:
        self.entity_service = entity_service
        self.event_service = event_service
        self.ui_mapping = ui_mapping
        self._generate_semantic_id = generate_semantic_id
        self.media_cache_service = media_cache_service
        self.action_handler = action_handler

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
        """专门渲染QQ弹窗的内容."""
        window_class = window.window_class
        content_node = parent_element

        # --- 根据不同的弹窗类型，渲染不同的内容 ---
        # 新消息通知弹窗
        if window_class == "qq_new_message_popup":
            content_node = SubElement(
                parent_element, "content", attrib={"type": "new_message_alert"}
            )
            state = window.content_state
            SubElement(content_node, "sender_name").text = state.get("sender_name")
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

        # 退出群聊确认弹窗
        elif window_class == "qq_confirm_leave_group_modal":
            state = window.content_state
            SubElement(content_node, "message").text = state.get(
                "message", "你确定要退出这个群聊吗？"
            )

            actions_node = SubElement(content_node, "actions")

            confirm_btn_id = self._generate_semantic_id([*current_path, "confirm_leave"])
            SubElement(
                actions_node,
                "button",
                attrib={"id": confirm_btn_id, "name": "confirm", "title": "确定退出"}
            )
            self.ui_mapping[confirm_btn_id] = {
                "action_type": "click",
                "action": "confirm_leave_group", # 内部指令
                "target_uid": state.get("target_conversation_uid"),
            }

        # 删除好友确认弹窗
        elif window_class == "qq_confirm_delete_friend_modal":
            state = window.content_state
            SubElement(content_node, "message").text = state.get(
                "message", "你确定要删除这个好友吗？"
            )
            actions_node = SubElement(content_node, "actions")

            confirm_btn_id = self._generate_semantic_id([*current_path, "confirm_delete"])
            SubElement(
                actions_node,
                "button",
                attrib={"id": confirm_btn_id, "name": "confirm", "title": "确定删除"}
            )
            self.ui_mapping[confirm_btn_id] = {
                "action_type": "click",
                "action": "confirm_delete_friend", # 内部指令
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

    # 私聊侧边栏渲染
    async def _render_private_chat_sidebar(
        self,
        parent_element: Element,
        current_path: list[str],
        window: Window,
        self_entity: dict | None,
    ) -> None:
        """渲染私聊窗口的侧边栏，用于修改备注和删除好友."""
        sidebar_visible = window.content_state.get("sidebar_visible", False)
        conversation_uid = window.content_state.get("conversation_uid")
        if not conversation_uid:
            return

        toggle_btn_id = self._generate_semantic_id([*current_path, "toggle_sidebar"])
        toggle_title = "关闭侧边栏" if sidebar_visible else "打开侧边栏"
        SubElement(
            parent_element,
            "button",
            attrib={"id": toggle_btn_id, "name": "toggle_sidebar", "title": toggle_title}
        )
        self.ui_mapping[toggle_btn_id] = {
            "action_type": "click",
            "action": "toggle_sidebar",
            "target_uid": window.name,
        }

        if not sidebar_visible:
            return

        sidebar_node = SubElement(parent_element, "sidebar", attrib={"status": "visible"})
        sidebar_path = [*current_path, "sidebar"]

        # --- 好友备注管理 ---
        remark_section = SubElement(sidebar_node, "remark_management")
        _, _, friend_native_id = parse_entity_uid(conversation_uid)
        friend_account_uid = f"{self_entity['details']['platform']}_{friend_native_id}"

        friend_doc = await self.entity_service.get_entity_by_key(friend_account_uid)
        current_remark = (
            friend_doc.details.friend_remark
            if friend_doc and hasattr(friend_doc.details, 'friend_remark')
            else ""
        ) or "未设置"

        # 使用唯一的 input_field ID，包含会话UID以确保唯一性
        remark_input_id = self._generate_semantic_id(
            [*sidebar_path, "friend_remark_input", f"conv_{conversation_uid}"]
        )
        SubElement(remark_section, "input_field", attrib={
            "id": remark_input_id,
            "name": "friend_remark",
            "current_value": current_remark,
            "title": "好友备注"
        })
        self.ui_mapping[remark_input_id] = {
            "action_type": "input_override",
            "target_uid": remark_input_id,
        }
        SubElement(remark_section, "desc").text = "你可以使用 'input_override' 动作来修改好友备注。"

        # --- 好友操作 ---
        friend_actions_section = SubElement(sidebar_node, "friend_actions")
        delete_btn_id = self._generate_semantic_id([*sidebar_path, "delete_friend"])
        SubElement(
            friend_actions_section,
            "button",
            attrib={"id": delete_btn_id, "name": "delete_friend", "title": "删除好友"}
        )
        self.ui_mapping[delete_btn_id] = {
            "action_type": "click",
            "action": "initiate_delete_friend",
            "target_uid": conversation_uid,
        }

    # 群聊侧边栏渲染
    async def _render_conversation_sidebar(
        self,
        parent_element: Element,
        current_path: list[str],
        window: Window,
        self_presence: dict | None,
    ) -> None:
        sidebar_visible = window.content_state.get("sidebar_visible", False)

        toggle_btn_id = self._generate_semantic_id([*current_path, "toggle_sidebar"])
        toggle_title = "关闭侧边栏" if sidebar_visible else "打开侧边栏"
        SubElement(
            parent_element,
            "button",
            attrib={"id": toggle_btn_id, "name": "toggle_sidebar", "title": toggle_title}
        )
        self.ui_mapping[toggle_btn_id] = {
            "action_type": "click",
            "action": "toggle_sidebar",
            "target_uid": window.name,
        }

        if not sidebar_visible:
            return

        sidebar_node = SubElement(parent_element, "sidebar", attrib={"status": "visible"})
        sidebar_path = [*current_path, "sidebar"]

        card_section = SubElement(sidebar_node, "card_management")
        current_card = (self_presence.get("cardname") if self_presence else "") or "未设置"

        # 使用 <input_field>
        card_input_id = self._generate_semantic_id([*sidebar_path, "card_input"])
        SubElement(card_section, "input_field", attrib={
            "id": card_input_id,
            "name": "self_card_name",
            "current_value": current_card,
            "title": "你的群名片"
        })
        # 为 input_field 生成映射
        self.ui_mapping[card_input_id] = {
            "action_type": "input_override",
            "target_uid": card_input_id, # target_uid 就是它自己
        }

        SubElement(card_section, "desc").text = "你可以使用 'input_override' 动作来修改你的群名片。"

        leave_section = SubElement(sidebar_node, "group_actions")
        leave_btn_id = self._generate_semantic_id([*sidebar_path, "leave_group"])
        SubElement(
            leave_section,
            "button",
            attrib={"id": leave_btn_id, "name": "leave_group", "title": "退出该群聊"}
        )
        self.ui_mapping[leave_btn_id] = {
            "action_type": "click",
            "action": "initiate_leave_group",
            "target_uid": window.content_state.get("conversation_uid"),
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

        # 前判断会话类型，以便后续逻辑复用
        try:
            conv_type = conversation_uid.split("_")[1]
            is_private_chat = conv_type == "private"
        except IndexError:
            is_private_chat = False
            logger.warning(f"无法从UID中判断会话类型: {conversation_uid}")

        # 获取机器人在当前会话的身份信息
        self_presence = await self.entity_service.get_self_presence_in_conversation(
            platform=platform, conversation_entity_uid=conversation_uid
        )
        self_entity = await self.entity_service.get_self_entity_by_platform(platform)

        if is_private_chat:
            await self._render_private_chat_sidebar(
                window_node, current_path, window, self_entity
            )

        else:
            await self._render_conversation_sidebar(
                window_node,
                current_path,
                window,
                self_presence
            )

        role_value = (
            str(bot_id)
            if is_private_chat
            else (self_presence.get("permission_level", "成员") if self_presence else "成员")
        )

        self_profile_attrs = {
            "name": self_entity.get("details", {}).get("nickname"),
            "card": self_presence.get("cardname", "") if self_presence else "",
            "role": role_value,
        }
        # 过滤掉空的属性
        self_profile_attrs = {k: v for k, v in self_profile_attrs.items() if v}
        SubElement(window_node, "self_profile_in_chat", attrib=self_profile_attrs)

        # --- 判断会话类型，如果是私聊，则在顶部显示对方信息 ---
        conversation_doc = await self.entity_service.get_entity_by_key(conversation_uid)

        if is_private_chat:
            participant_attrs = {
                "name": conversation_doc.details.name,
                "uid": conversation_doc.details.conversation_id,
            }
            SubElement(window_node, "participant_in_chat", attrib=participant_attrs)

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

            # 在这里提取正确的 message_id
            platform_msg_id = None
            for seg in msg.get("content", []):
                if seg.get("type") == "message_metadata":
                    platform_msg_id = seg.get("data", {}).get("message_id")
                    break
            if not platform_msg_id:
                logger.error(f"致命错误：消息缺少 message_id: {msg}")
                platform_msg_id = "未知错误，ID无法获取"

            # --- 根据是否为私聊，动态构建消息 div 的属性 ---
            msg_attrs = {
                "class": "message",
                "id": platform_msg_id,
                "align": align,
            }
            if not is_private_chat:
                msg_attrs["sender_id"] = str(msg_sender_id)

            msg_node = SubElement(list_node, "div", attrib=msg_attrs)

            timestamp = time.strftime("%H:%M:%S", time.localtime(msg.get("timestamp", 0) / 1000))

            # 如果不是私聊（即群聊），则显示每个发言人的名字和群成员总数
            if not is_private_chat:
                # 群成员总数可能会变动，因此每次都实时查询
                member_count = await self.entity_service.get_group_member_count(conversation_uid)
                if member_count > 0:
                    # 渲染 <group_info> 节点
                    SubElement(window_node, "group_info", attrib={"count": str(member_count)})
                # 渲染发言人名字
                sender_name = msg.get("user_info", {}).get("user_cardname") or msg.get(
                    "user_info", {}
                ).get("user_nickname")
                SubElement(msg_node, "sender").text = sender_name

            SubElement(msg_node, "timestamp").text = timestamp

            content_node = SubElement(msg_node, "content")
            await self._render_rich_content(
                content_node, msg.get("content", []), image_collector, window, bot_ids_map
            )

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
        SubElement(action_bar_node, "desc").text = "可以使用 send_message 动作来回复。"

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
        self,
        content_node: Element,
        segments: list[dict],
        image_collector: list[dict],
        window: Window,
        bot_ids_map: dict,
    ) -> None:
        """渲染富文本消息内容，精确分离文本和多模态元素，并保持原始顺序."""
        text_buffer = ""

        def flush_text_buffer() -> None:
            """内部辅助函数：如果缓冲区有内容，则写入 <text> 标签并清空."""
            nonlocal text_buffer
            if text_buffer:
                # 移除可能由 @ 段产生的尾部多余空格
                SubElement(content_node, "text").text = text_buffer.rstrip()
                text_buffer = ""

        conversation_uid = window.content_state.get("conversation_uid")
        conv_doc = (
            await self.entity_service.get_entity_by_key(conversation_uid)
            if conversation_uid
            else None
        )

        for seg in segments:
            seg_type = seg.get("type")
            data = seg.get("data", {})

            # 在循环的最开始，直接跳过元数据
            if seg_type == "message_metadata":
                continue

            # 步骤 1: 将所有文本类内容聚合到缓冲区
            if seg_type == "text":
                text_buffer += data.get("text")
            elif seg_type == "at":
                user_id = data.get("user_id")
                display_name = data.get("display_name")

                if user_id and conv_doc:
                    try:
                        latest_name = await self.entity_service.get_sender_display_name_for_event(
                            {"user_info": {"user_id": user_id}},
                            conv_doc,
                            bot_ids_map,
                        )
                        display_name = f"@{latest_name}"
                    except ValueError:
                        logger.warning(f"无法获取用户 {user_id} 的显示名称，尝试从适配器获取...")
                        if await self._fetch_and_update_user_info(user_id, conv_doc, bot_ids_map):
                            try:
                                latest_name = (
                                    await self.entity_service.get_sender_display_name_for_event(
                                        {"user_info": {"user_id": user_id}},
                                        conv_doc,
                                        bot_ids_map,
                                    )
                                )
                                display_name = f"@{latest_name}"
                            except ValueError:
                                logger.error(
                                    f"从适配器获取信息后，仍然无法获取用户 {user_id} 的显示名称。"
                                )
                                display_name = f"@{user_id}"
                        else:
                            display_name = f"@{user_id}"
                else:
                    logger.error(f"致命错误：无法解析 @ 段的用户信息: {data}")
                    continue
                text_buffer += f"{display_name}"

            # 步骤 2: 遇到非文本元素，先处理缓冲区，再处理该元素
            else:
                flush_text_buffer()  # 冲刷掉此前的文本

                # 现在处理非文本元素
                if seg_type in ["image", "video"]:
                    image_hash = data.get("hash")
                    if not image_hash:
                        logger.error(f"致命错误：图片/视频段缺少 hash 信息: {data}")
                        # 如果图片加载失败，也用一个专门的标签
                        SubElement(content_node, "error").text = "[图片加载失败]"
                        continue

                    # 生成占位符和收集图片信息的逻辑保持不变
                    conversation_uid = window.content_state.get("conversation_uid")
                    platform, _, _ = (
                        parse_entity_uid(conversation_uid)
                        if conversation_uid
                        else logger.error(
                            f"致命错误：无法解析 conversation_uid: {conversation_uid}"
                        )
                    )
                    short_hash = image_hash[:8]
                    summary = data.get("summary", "image")
                    placeholder_prefix = "图片"
                    if summary in ("sticker", "animated_sticker"):
                        placeholder_prefix = "动画表情"

                    placeholder_text = f"[{placeholder_prefix}:{short_hash}]"

                    if not any(img.get("hash") == image_hash for img in image_collector):
                        image_ref = {
                            "placeholder": placeholder_text,
                            "hash": image_hash,
                            "platform_id": platform,
                        }
                        cached_image_data = await self.media_cache_service.get_image_b64_by_hash(
                            image_hash
                        )
                        if cached_image_data:
                            image_ref["mime_type"] = cached_image_data.get("mime_type")
                            image_ref["base64"] = cached_image_data.get("base64")
                        image_collector.append(image_ref)

                    # 使用你建议的 <image> 标签
                    SubElement(content_node, "image").text = placeholder_text

                elif seg_type == "quote":
                    message_id = data.get("message_id")
                    author_name = data.get("nickname")
                    snippet = data.get("text")

                    # 尝试从数据库获取更精确的信息
                    if message_id and window.content_state.get("conversation_uid"):
                        # 获取当前窗口的 conversation_uid
                        conv_uid = window.content_state.get("conversation_uid")
                        # 调用新方法回查事件
                        quoted_event_doc = (
                            await self.event_service.get_event_by_platform_message_id(
                                conv_uid, message_id
                            )
                        )

                        if quoted_event_doc:
                            # 如果找到了，使用高保真信息
                            conv_doc = await self.entity_service.get_entity_by_key(conv_uid)
                            author_name = (
                                await self.entity_service.get_sender_display_name_for_event(
                                    quoted_event_doc,
                                    conv_doc,
                                    bot_ids_map,
                                )
                            )
                            snippet = await self.event_service.get_event_text_summary(
                                quoted_event_doc
                            )

                    # 渲染最终结果（无论是精确的还是降级的）
                    if len(snippet) > 20:
                        snippet = snippet[:20] + "..."
                    SubElement(
                        content_node,
                        "quote",
                        attrib={
                            "author": author_name,
                            "message_id": str(message_id),
                            "snippet": snippet,
                        },
                    )

                # 以后可以为其他非文本类型（如文件、分享链接）添加 elif
                # elif seg_type == "file":
                #     file_name = data.get('file_name')
                #     SubElement(content_node, "file").text = f"[文件: {file_name}]"
                # elif seg_type == "share":
                #     share_text = f"[分享链接: {data.get('url')}]"
                #     SubElement(content_node, "share").text = share_text
        # 最后，冲刷一次缓冲区，确保所有文本都被渲染
        flush_text_buffer()

    async def _fetch_and_update_user_info(
        self, user_id: str, conv_doc: dict, bot_ids_map: dict
    ) -> bool:
        """从适配器获取用户信息并更新数据库."""
        platform = conv_doc.details.platform
        bot_id = bot_ids_map.get(platform)
        if not bot_id:
            return False

        action_name = "get_group_member_info"
        params = {
            "group_id": conv_doc.details.conversation_id,
            "user_id": user_id,
        }

        result = await self.action_handler.execute_simple_action(
            platform_id=platform,
            action_name=action_name,
            params=params,
            bot_id=bot_id,
            description=f"获取群 {conv_doc.details.conversation_id} 成员 {user_id} 的信息",
        )

        if result.is_success and result.payload:
            user_info = result.payload
            account_uid = f"{platform}_{user_id}"
            await self.entity_service.update_presence_in_conversation(
                account_entity_uid=account_uid,
                conversation_entity_uid=conv_doc._key,
                user_info=user_info,
            )
            return True
        return False
