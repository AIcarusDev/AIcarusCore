# 文件路径: src/os/apps/qq/builder.py

from __future__ import annotations

import time
import uuid
from typing import TYPE_CHECKING
from xml.etree.ElementTree import Element

from aicarus_protocols import ConversationInfo, Event, Seg
from aicarus_protocols import Event as ProtocolEvent
from src.common.custom_logging.logging_config import get_logger
from src.common.utils import build_conversation_entity_uid, parse_entity_uid
from src.os.apps.interfaces import IApp, ISession
from src.os.apps.qq.qq_chat_session_manager import QQChatSessionManager
from src.os.apps.qq.qq_inspection_service import inspect_and_initialize_self_profile
from src.os.apps.qq.qq_renderer import QQWindowRenderer
from src.os.models import Window, WindowStatus
from src.os.window_manager import WindowManager
from src.services.action.components.base_builder import BaseAppBuilder

if TYPE_CHECKING:
    from src.bootstrap.container import ServiceContainer
    from src.os.file_system_manager import FileSystemManager
    from src.services.database.services.entity_graph_service import EntityGraphService
    from src.services.database.services.event_storage_service import EventStorageService
    from src.services.database.services.media_cache_service import MediaCacheService


logger = get_logger(__name__)


class QQBuilder(BaseAppBuilder, IApp):
    """QQ 平台的构建器，现在负责处理 OS 级别的实时事件."""

    def __init__(self) -> None:
        self._session_manager_instance: QQChatSessionManager | None = None
        self._renderer: QQWindowRenderer | None = None

    @property
    def app_name(self) -> str:
        """返回平台ID."""
        return "qq"

    @property
    def needs_on_connect_inspection(self) -> bool:
        """告知 Core，此平台连接后需要执行安检."""
        return True

    async def run_on_connect_inspection(self, container: ServiceContainer) -> None:
        """由 CoreWebsocketServer 调用的、平台专属的安检流程."""
        logger.info(f"--- [QQBuilder] 开始执行平台 '{self.app_name}' 的上线安检仪式 ---")

        # 无论档案来源如何，都必须执行完整的检查和持久化流程。
        # inspect_and_initialize_self_profile 函数内部会处理好一切。
        success, profile_data = await inspect_and_initialize_self_profile(
            entity_service=container.entity_graph_service,
            action_handler=container.action_handler,
            platform_id=self.app_name,
            # 将从 WS Server 缓存的档案作为优先数据源传入
            cached_profile_data=container.core_comm_layer.get_cached_profile_for_platform(
                self.app_name
            ),
        )

        if success and profile_data:
            logger.success("[QQBuilder] 安检成功，自身档案已确认或更新。")
            bot_id = profile_data.get("user_id")
            if bot_id:
                container.application_manager.set_self_bot_id_for_platform(
                    self.app_name, str(bot_id)
                )
        else:
            logger.critical(f"[QQBuilder] 安检失败！app '{self.app_name}' 的功能将严重受影响。")

    async def on_before_start(self, container: ServiceContainer) -> tuple[bool, str | None]:
        """QQ启动前检查是否已完成安检."""
        if not container.application_manager.get_self_bot_ids_map().get(self.app_name):
            error_message = "无法启动应用 'QQ'。\n原因：QQ 应用尚未完成身份检查。"
            logger.error(error_message.replace("\n", " "))
            return False, error_message
        return True, None

    async def on_after_start(self, container: ServiceContainer, app_id: str) -> Window:
        """QQ启动后创建主窗口。."""
        return Window(
            name="qq_main",
            parent_app_id=app_id,
            title="QQ",
            window_class="main",
            content_state={"view": "conversation_list"},
        )

    # OS 实时事件处理器
    async def handle_os_level_event(
        self, event: ProtocolEvent, container: ServiceContainer
    ) -> None:
        """处理分发到 OS 层的实时事件，主要用于触发 UI 变化，如弹窗."""
        # 目前只关心新消息事件
        if not event.event_type.startswith("message."):
            return

        # 获取 OS 层服务
        window_manager = container.window_manager
        application_manager = container.application_manager
        entity_service = container.entity_graph_service

        # 弹窗逻辑
        # 1. 必须是别人发的消息
        bot_id = application_manager.get_self_bot_ids_map().get(event.get_platform())
        if not bot_id or not event.user_info or str(event.user_info.user_id) == str(bot_id):
            return

        # 2. 当前不能有模态弹窗锁定UI
        if window_manager.get_active_modal_popup():
            return

        # 3. 消息不能来自当前聚焦的聊天窗口
        active_window = next(
            (
                w
                for w in reversed(window_manager.get_all_windows_sorted())
                if w.status != WindowStatus.MINIMIZE
            ),
            None,
        )

        target_conv_uid = build_conversation_entity_uid(
            event.get_platform(),
            event.conversation_info.type,
            event.conversation_info.conversation_id,
        )

        if active_window and active_window.content_state.get("conversation_uid") == target_conv_uid:
            return

        # --- 所有条件满足，创建弹窗 ---
        logger.info(f"[QQBuilder] 检测到来自 '{target_conv_uid}' 的新消息，触发弹窗。")

        # 通过 app_name 查找应用信息
        app_info = next(
            (app for app in application_manager.get_all_apps() if app.name == self.app_name), None
        )
        if not app_info:
            logger.error(f"无法为新消息创建弹窗，因为找不到 name 为 '{self.app_name}' 的应用定义。")
            return

        # 使用 get_or_create_conversation_entity 防止竞态条件
        conv_doc = await entity_service.get_or_create_conversation_entity(
            conversation_id=event.conversation_info.conversation_id,
            platform=event.get_platform(),
            conv_type=event.conversation_info.type,
            name=event.conversation_info.name,
        )

        sender_name = await entity_service.get_sender_display_name_for_event(
            event.to_dict(), conv_doc, application_manager.get_self_bot_ids_map()
        )

        snippet = await container.event_storage_service.get_event_text_summary(event.to_dict())

        popup = Window(
            name=f"win-popup-qq-newmsg-{uuid.uuid4().hex[:6]}",
            parent_app_id=app_info.id,
            title="新消息提醒",
            window_class="qq_new_message_popup",
            content_state={
                "sender_name": sender_name,
                "message_snippet": snippet,
                "target_conversation_uid": target_conv_uid,
            },
            is_popup=True,
            popup_type="interactive",
            transient_cycles_remaining=1,  # 显示一轮
        )
        window_manager.open_window(popup)

    # 实现 handle_ui_command 来处理由点击触发的内部指令
    async def handle_ui_command(
        self, command: str, target_uid: str, container: ServiceContainer
    ) -> None:
        """处理由 UI Dispatcher 转发来的、通过点击触发的应用专属指令."""
        if command == "initiate_leave_group":
            await self._handle_initiate_leave_group(
                {"target_conversation_uid": target_uid}, container
            )
        elif command == "confirm_leave_group":
            # 对于确认操作，我们需要一个 thought_key 来回写结果，但点击事件没有。
            # 我们可以创建一个临时的，或者让 action_handler 返回结果。
            # 这里我们简化，直接执行，结果只打印日志。
            await self._handle_confirm_leave_group(
                {"target_conversation_uid": target_uid}, container, None
            )
        else:
            logger.warning(f"QQBuilder 收到一个未知的 UI 指令: {command}")

    # 实现 handle_input_override 方法
    async def handle_input_override(
        self, target_id: str, content: str, container: ServiceContainer, thought_key: str
    ) -> None:
        """处理 QQ 应用专属的 input_override 动作."""
        if "card_input" in target_id:
            try:
                parts = target_id.split('.')
                conv_uid = parts[1].replace("conv_", "")
                params = {"target_conversation_uid": conv_uid, "new_card": content}
                await self._handle_set_self_card(params, container, thought_key)
            except IndexError:
                logger.error(f"无法从 input target_id '{target_id}' 中解析出 conversation_uid。")
        else:
            logger.warning(f"QQBuilder 收到一个未知的 input_override target_id: {target_id}")

    # [修改] handle_llm_action 现在只处理参数驱动的动作
    async def handle_llm_action(
        self, action_name: str, params: dict, container: ServiceContainer, thought_key: str
    ) -> None:
        """处理由LLM决策的、分发到QQ应用的特定动作."""
        # 目前只处理 manage_stickers
        if action_name == "manage_stickers":
            if not container.qq_sticker_service:
                logger.error("QQStickerService not available in container.")
                return

            result_message = await container.qq_sticker_service.manage_stickers(params)
            await container.thought_storage_service.save_action_result_to_thought(
                thought_key=thought_key, result_text=result_message
            )
        else:
            logger.warning(f"QQBuilder收到了一个未知的LLM动作请求: {action_name}")

    async def _handle_set_self_card(
            self, params: dict, container: ServiceContainer, thought_key: str
            ) -> None:
        target_uid = params.get("target_conversation_uid")
        new_card = params.get("new_card")
        if not target_uid or new_card is None:
            return

        _, _, group_id = parse_entity_uid(target_uid)
        bot_id = container.application_manager.get_self_bot_ids_map().get(self.app_name)

        if not group_id or not bot_id:
            return

        result = await container.action_handler.execute_simple_action(
            platform_id=self.app_name,
            action_name="set_member_card",
            params={"group_id": group_id, "user_id": bot_id, "card": new_card},
            bot_id=bot_id,
            description="修改自身群名片"
        )

        result_text = f"修改群名片操作完成。结果: {
            '成功' if result.is_success else '失败: ' + (result.error_message or '未知错误')
        }"
        if thought_key:
            await container.thought_storage_service.save_action_result_to_thought(
                thought_key=thought_key, result_text=result_text
            )
        logger.info(result_text)

    async def _handle_initiate_leave_group(self, params: dict, container: ServiceContainer) -> None:
        target_uid = params.get("target_conversation_uid")
        if not target_uid:
            return

        conv_doc = await container.entity_graph_service.get_entity_by_key(target_uid)
        group_name = conv_doc.details.name if conv_doc and conv_doc.details else target_uid

        popup = Window(
            name=f"win-modal-qq-leave-{uuid.uuid4().hex[:6]}",
            parent_app_id="app-qq",
            title="确认操作",
            window_class="qq_confirm_leave_group_modal",
            content_state={
                "message": f"你确定要退出群聊 '{group_name}' 吗？此操作不可恢复。",
                "target_conversation_uid": target_uid,
            },
            is_popup=True,
            popup_type="modal",
        )
        container.window_manager.open_window(popup)

    async def _handle_confirm_leave_group(
            self, params: dict, container: ServiceContainer, thought_key: str | None
        ) -> None:
        target_uid = params.get("target_conversation_uid")
        if not target_uid:
            return

        _, _, group_id = parse_entity_uid(target_uid)
        bot_id = container.application_manager.get_self_bot_ids_map().get(self.app_name)

        if not group_id or not bot_id:
            return

        result = await container.action_handler.execute_simple_action(
            platform_id=self.app_name,
            action_name="leave_conversation",
            params={"group_id": group_id},
            bot_id=bot_id,
            description="确认退出群聊"
        )

        if result.is_success:
            window_to_close = f"conv_{target_uid}"
            container.window_manager.close_window(window_to_close)
            for w in container.window_manager.get_all_windows_sorted():
                if w.window_class == "qq_confirm_leave_group_modal":
                    container.window_manager.close_window(w.name)

        result_text = f"退出群聊操作完成。结果: {
            '成功' if result.is_success else '失败: ' + (result.error_message or '未知错误')
        }"
        if thought_key: # 如果是通过 LLM Action 触发的（虽然现在不会），则回写
            await container.thought_storage_service.save_action_result_to_thought(
                thought_key=thought_key, result_text=result_text
            )
        logger.info(result_text)

    def get_session_manager(self, container: ServiceContainer) -> QQChatSessionManager:
        """按需创建并返回 QQChatSessionManager 的单例.

        这是实现懒加载的核心。
        """
        if self._session_manager_instance is None:
            # 只有在第一次被请求时，才创建实例
            self._session_manager_instance = QQChatSessionManager(
                llm_client=container.main_consciousness_llm_client,
                event_storage=container.event_storage_service,
                action_handler=container.action_handler,
                self_bot_ids_map=container.application_manager.get_self_bot_ids_map(),
                intelligent_interrupter=container.intelligent_interrupter,
                entity_graph_service=container.entity_graph_service,
                thought_storage_service=container.thought_storage_service,
                core_logic=container.core_logic,
            )
        return self._session_manager_instance

    def _get_renderer(
        self,
        entity_service: EntityGraphService,
        event_service: EventStorageService,
        media_cache_service: MediaCacheService,
        ui_mapping: dict,
        generate_semantic_id: callable,
        action_handler: callable,
    ) -> QQWindowRenderer:
        """按需创建或返回渲染器实例."""
        if self._renderer is None:
            self._renderer = QQWindowRenderer(
                entity_service,
                event_service,
                ui_mapping,
                generate_semantic_id,
                media_cache_service,
                action_handler,
            )
        # 确保 renderer 使用的是当前轮次的上下文
        self._renderer.ui_mapping = ui_mapping
        self._renderer.generate_semantic_id = generate_semantic_id
        self._renderer.media_cache_service = media_cache_service
        self._renderer.action_handler = action_handler
        return self._renderer

    # 实现 IApp 接口的方法
    async def get_session(
        self, conversation_uid: str, container: ServiceContainer
    ) -> ISession | None:
        """根据会话 UID 获取一个会话实例."""
        session_manager = self.get_session_manager(container)
        if session_manager:
            return await session_manager.get_or_create_session(conversation_uid)
        return None

    # 实现渲染器接口
    async def render_window_content(
        self,
        parent_element: Element,
        current_path: list[str],
        window: Window,
        bot_ids_map: dict,
        entity_service: EntityGraphService,
        event_service: EventStorageService,
        media_cache_service: MediaCacheService,
        ui_mapping: dict,
        generate_semantic_id: callable,
        image_collector: list[dict],
        action_handler: callable,
        file_system_manager: FileSystemManager,
    ) -> None:
        """实现基类的渲染接口，委托给QQWindowRenderer处理."""
        renderer = self._get_renderer(
            entity_service,
            event_service,
            media_cache_service,
            ui_mapping,
            generate_semantic_id,
            action_handler,
        )
        await renderer.render_content(
            parent_element, current_path, window, bot_ids_map, image_collector
        )

    async def render_popup_content(
        self,
        parent_element: Element,
        current_path: list[str],
        window: Window,
        bot_ids_map: dict,
        entity_service: EntityGraphService,
        event_service: EventStorageService,
        media_cache_service: MediaCacheService,
        ui_mapping: dict,
        generate_semantic_id: callable,
        image_collector: list[dict],
        action_handler: callable,
        file_system_manager: FileSystemManager,
    ) -> None:
        """实现弹窗渲染，委托给QQWindowRenderer处理。."""
        renderer = self._get_renderer(
            entity_service,
            event_service,
            media_cache_service,
            ui_mapping,
            generate_semantic_id,
            action_handler,
        )
        await renderer.render_popup_content(
            parent_element,
            current_path,
            window,
        )

    # [修改] 现在只定义参数驱动的动作
    async def get_action_definitions(
        self, window_manager: WindowManager, container: ServiceContainer
    ) -> dict:
        """动态定义 QQ 平台提供给LLM的所有动作."""
        llm_actions = {}
        if not container.qq_sticker_service:
            return {}

        # 1. 获取所有表情包数据
        all_stickers = await container.qq_sticker_service.get_all_stickers()
        sticker_ids = [s["sticker_id"] for s in all_stickers]

        visible_conv_windows = [
            window
            for window in window_manager.get_all_windows_sorted()
            if window.window_class == "conversation"
            and window.status != WindowStatus.MINIMIZE
            and window.content_state.get("conversation_uid")
        ]

        send_message_enabled_conv_uids = [
            w.content_state.get("conversation_uid") for w in visible_conv_windows
            if not w.content_state.get("sidebar_visible", False)
        ]

        if send_message_enabled_conv_uids:
            send_message_schema = self._build_send_message_schema(
                send_message_enabled_conv_uids, sticker_ids
            )
            llm_actions["send_message"] = send_message_schema

        if visible_conv_windows:
            manage_stickers_schema = self._build_manage_stickers_schema(sticker_ids)
            if manage_stickers_schema["properties"]:
                llm_actions["manage_stickers"] = manage_stickers_schema

        # [修改] 将应用专属的动作都放在一个 'qq' 的 namespace 下
        if llm_actions:
            return {"qq": {"type": "object", "properties": llm_actions}}

        return {}


    def _build_send_message_schema(
        self, visible_conv_uids: list[str], sticker_ids: list[str]
    ) -> dict:
        """辅助方法：构建 send_message 的 JSON Schema."""
        # 基础指令
        steps_oneof = [
            {
                "title": "引用/回复消息",
                "properties": {
                    "command": {"type": "string", "enum": ["reply"]},
                    "params": {
                        "type": "object",
                        "properties": {"message_id": {"type": "string"}},
                        "required": ["message_id"],
                        "description": (
                            "ID从聊天记录中目标用户发言的`div`元素的"
                            "`sender_id`属性获取，仅在需要特别提醒某人时使用，避免滥用。"
                        ),
                    },
                },
            },
            {
                "title": "@某人",
                "properties": {
                    "command": {"type": "string", "enum": ["at"]},
                    "params": {
                        "type": "object",
                        "properties": {"user_id": {"type": "string"}},
                        "required": ["user_id"],
                        "description": (
                            "ID从聊天记录中目标消息的`id`属性获取，"
                            "仅在需要明确上下文时使用，避免滥用。"
                        ),
                    },
                },
            },
            {
                "title": "发送文本",
                "properties": {
                    "command": {"type": "string", "enum": ["text"]},
                    "params": {
                        "type": "object",
                        "properties": {"content": {"type": "string"}},
                        "required": ["content"],
                        "description": (
                            "要发送的文本内容。建议内容简短、自然，可省略主语和大部分标点符号。"
                        ),
                    },
                },
            },
            {
                "title": "发送并开启新消息",
                "properties": {
                    "command": {"type": "string", "enum": ["send_and_compose_next"]},
                    "params": {
                        "type": "object",
                        "properties": {},
                        "description": (
                            "此指令会触发一次发送操作，"
                            "将其前面所有的指令作为一条消息发送出去。"
                            "它也标志着下一条新消息的开始"
                        ),
                    },
                },
            },
        ]

        # 如果有表情包，才添加发送表情包的选项
        if sticker_ids:
            sticker_step = {
                "title": "发送表情包",
                "properties": {
                    "command": {"type": "string", "enum": ["sticker"]},
                    "params": {
                        "type": "object",
                        "properties": {"sticker_id": {"type": "string", "enum": sticker_ids}},
                        "required": ["sticker_id"],
                        "description": "从收藏中选择的表情包ID (从<sticker_collection_preview>获取)。",  # noqa: E501
                    },
                },
            }
            # 插入到倒数第二的位置，在 send_and_compose_next 之前
            steps_oneof.insert(-1, sticker_step)

        return {
            "type": "object",
            "title": "发送QQ消息",
            "description": (
                "在指定的、当前可见的qq聊天窗口中发送消息，"
                "通常发言应简洁自然,偏口语化，"
                "建议**省略主语**，"
                "建议**省略标点符号**。"
                "关注对话的自然流转。若发送消息后对方没有立即回应是正常的现象，可能是对方在忙或话题已结束等等。"
                "可以一次只发送一条消息，也可以选择把一段完整的消息拆分为多条。"
                "在已经拆分了多条消息的情况下，每条消息可以**非常简短**。"
                "但是需要注意一下拆分的消息数量，避免依次发送过多的消息导致刷屏。"
            ),
            "properties": {
                "target_conversation_uid": {
                    "title": "目标会话ID",
                    "type": "string",
                    "description": "需要是当前屏幕上可见会话的ID。",
                    "enum": visible_conv_uids,
                },
                "steps": {
                    "type": "array",
                    "description": (
                        "一个指令对象序列，用于构建最终要发送的消息内容。"
                        "不同指令可以组合使用。例如，先@某人再说你好："
                        '`[{"command": "at", ...}, {"command": "text", ...}]`。'
                        "使用`send_and_compose_next`可分多条发送。"
                    ),
                    "items": {
                        "description": (
                            "单个操作步骤，必须包含一个指令(command)和其对应的唯一参数(params)。"
                        ),
                        "oneOf": steps_oneof,
                    },
                },
                "motivation": {"type": "string"},
            },
            "required": ["target_conversation_uid", "steps", "motivation"],
        }

    def _build_manage_stickers_schema(self, sticker_ids: list[str]) -> dict:
        """辅助方法：构建 manage_stickers 的 JSON Schema."""
        properties = {
            "add": {
                "type": "object",
                "description": "从聊天记录中添加一张图片到你的表情包收藏。",
                "properties": {
                    "image_hash": {
                        "type": "string",
                        "description": (
                            "要添加为表情包的图片的哈希ID (从聊天记录的 `(hash:...)` 中获取)。"
                        ),
                    },
                    "impression": {
                        "type": "string",
                        "description": "你对这张表情包的主观印象/描述。",
                    },
                },
                "required": ["image_hash", "impression"],
            }
        }

        # 如果有表情包，才添加 remove 和 edit_impression
        if sticker_ids:
            properties["remove"] = {
                "type": "object",
                "description": "从你的表情包收藏中移除一个已有的表情包。",
                "properties": {
                    "sticker_id": {
                        "type": "string",
                        "description": "要移除的表情包的唯一编号。",
                        "enum": sticker_ids,
                    }
                },
                "required": ["sticker_id"],
            }
            properties["edit_impression"] = {
                "type": "object",
                "description": "编辑一个已有的表情包的印象/描述。",
                "properties": {
                    "sticker_id": {
                        "type": "string",
                        "description": "要编辑印象/描述的表情包的编号。",
                        "enum": sticker_ids,
                    },
                    "new_impression": {"type": "string", "description": "新的印象/描述。"},
                },
                "required": ["sticker_id", "new_impression"],
            }

        return {
            "type": "object",
            "description": "管理你在QQ平台的表情包收藏。",
            "properties": {
                **properties,
                "motivation": {"type": "string"},
            },
            "required": ["motivation"],
            "oneOf": [{"required": [key]} for key in properties],
        }

    def build_action_event(self, action_name: str, params: dict, bot_id: str) -> Event | None:
        """将 Core 的指令转换成发往 Adapter 的标准 Event."""
        # LLM 可用动作: send_message
        if action_name == "send_message":
            conv_id = params.get("conversation_id")
            conv_type = params.get("conversation_type")
            if not conv_id or not conv_type:
                logger.error("构建 send_message 事件失败：params 中缺少会话信息。")
                return None
            conversation_info = ConversationInfo(conversation_id=str(conv_id), type=str(conv_type))
            content_segs = [Seg.from_dict(seg) for seg in params.get("content", [])]
            return Event(
                event_id=str(uuid.uuid4()),
                event_type=f"action.{self.app_name}.send_message",
                time=int(time.time() * 1000),
                bot_id=bot_id,
                content=content_segs,
                conversation_info=conversation_info,
            )

        elif action_name in [
            "get_group_member_list", "set_member_card", "leave_conversation"
        ]:
            return Event(
                event_id=str(uuid.uuid4()),
                event_type=f"action.{self.app_name}.{action_name}",
                time=int(time.time() * 1000),
                bot_id=bot_id,
                content=[Seg(type="action_params", data=params)],
            )

        # 其他未来可能添加的内部动作...
        # elif action_name == "get_bot_profile":
        #     ...

        # 如果动作未被识别
        logger.warning(f"QQBuilder 无法为未知的动作 '{action_name}' 构建事件。")
        return None
