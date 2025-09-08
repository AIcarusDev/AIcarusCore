# 文件路径: src/os/apps/qq/builder.py

from __future__ import annotations

import time
import uuid
from typing import TYPE_CHECKING
from xml.etree.ElementTree import Element

from aicarus_protocols import ConversationInfo, Event, Seg
from aicarus_protocols import Event as ProtocolEvent
from src.common.custom_logging.logging_config import get_logger
from src.common.utils import build_conversation_entity_uid
from src.os.apps.interfaces import IApp, ISession
from src.os.models import Window, WindowStatus
from src.os.window_manager import WindowManager
from src.services.action.components.base_builder import BaseAppBuilder

from .qq_chat_session_manager import QQChatSessionManager
from .qq_inspection_service import inspect_and_initialize_self_profile
from .qq_renderer import QQWindowRenderer

if TYPE_CHECKING:
    from src.bootstrap.container import ServiceContainer
    from src.services.database.services.entity_graph_service import EntityGraphService
    from src.services.database.services.event_storage_service import EventStorageService


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

    # 处理表情包动作的后端逻辑
    async def handle_sticker_action(
        self, params: dict, container: ServiceContainer, thought_key: str
    ) -> None:
        """处理 manage_stickers 动作的实际逻辑."""
        if not container.qq_sticker_service:
            logger.error("QQStickerService not available in container.")
            return

        result_message = await container.qq_sticker_service.manage_stickers(params)
        await container.thought_storage_service.save_action_result_to_thought(
            thought_key=thought_key, result_text=result_message
        )

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
        ui_mapping: dict,
        generate_semantic_id: callable,
    ) -> QQWindowRenderer:
        """按需创建或返回渲染器实例."""
        if self._renderer is None:
            self._renderer = QQWindowRenderer(
                entity_service, event_service, ui_mapping, generate_semantic_id
            )
        # 确保 renderer 使用的是当前轮次的上下文
        self._renderer.ui_mapping = ui_mapping
        self._renderer.generate_semantic_id = generate_semantic_id
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
        ui_mapping: dict,
        generate_semantic_id: callable,
        image_collector: list[dict],
    ) -> None:
        """实现基类的渲染接口，委托给QQWindowRenderer处理."""
        renderer = self._get_renderer(
            entity_service, event_service, ui_mapping, generate_semantic_id
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
        ui_mapping: dict,
        generate_semantic_id: callable,
        image_collector: list[dict],
    ) -> None:
        """实现弹窗渲染，委托给QQWindowRenderer处理。."""
        renderer = self._get_renderer(
            entity_service, event_service, ui_mapping, generate_semantic_id
        )
        await renderer.render_popup_content(
            parent_element,
            current_path,
            window,
        )

    # 实现动态 Schema
    async def get_action_definitions(
        self, window_manager: WindowManager, container: ServiceContainer
    ) -> dict:
        """动态定义 QQ 平台的所有动作，特别是 send_message 和 manage_stickers."""
        all_actions = {}
        if not container.qq_sticker_service:
            return {}

        # 1. 获取所有表情包数据
        all_stickers = await container.qq_sticker_service.get_all_stickers()
        sticker_ids = [s["sticker_id"] for s in all_stickers]

        # 2. 构建 send_message Schema
        visible_conv_uids = [
            window.content_state.get("conversation_uid")
            for window in window_manager.get_all_windows_sorted()
            if window.window_class == "conversation"
            and window.status != WindowStatus.MINIMIZE
            and window.content_state.get("conversation_uid")
        ]

        if visible_conv_uids:
            send_message_schema = self._build_send_message_schema(visible_conv_uids, sticker_ids)
            all_actions["send_message"] = send_message_schema

        # 3. 构建 manage_stickers Schema (仅在聊天窗口激活时)
        if visible_conv_uids:
            manage_stickers_schema = self._build_manage_stickers_schema(sticker_ids)
            if manage_stickers_schema["properties"]:  # 确保有内容才添加
                all_actions["manage_stickers"] = manage_stickers_schema

        return all_actions

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
                            "要发送的文本内容。建议内容简短、自然，"
                            "可省略主语和大部分标点符号。"
                        )
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
                        )
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
                            "单个操作步骤，必须包含一个指令(command)和其"
                            "对应的唯一参数(params)。"
                        ),
                        "oneOf": steps_oneof
                    }
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
                            "要添加为表情包的图片的哈希ID "
                            "(从聊天记录的 `(hash:...)` 中获取)。"
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
                event_type=f"action.{self.app_name}.{action_name}",
                time=int(time.time() * 1000),
                bot_id=bot_id,
                content=content_segs,
                conversation_info=conversation_info,
            )
        return None
