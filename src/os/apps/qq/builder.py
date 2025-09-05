# 文件路径: src/os/apps/qq/builder.py

from __future__ import annotations

import asyncio
import time
import uuid
from typing import TYPE_CHECKING
from xml.etree.ElementTree import Element

from aicarus_protocols import Event, Seg
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

        # 增加一个1秒的延迟，以确保 Adapter 侧的连接状态完全就绪
        await asyncio.sleep(1)

        # 优先使用从 ready 事件中缓存的档案
        profile_data = None
        success = False

        # 尝试从 Websocket 服务器获取缓存的档案
        ws_server = container.core_comm_layer
        if ws_server and self.app_name in ws_server.adapter_clients_info:
            connection_info = ws_server.adapter_clients_info[self.app_name]
            cached_profile = connection_info.get("bot_profile")
            if cached_profile:
                logger.info(
                    f"安检流程：发现已缓存的档案 for '{self.app_name}'，直接使用该档案。"
                    )
                profile_data = cached_profile
                # 假设数据格式正确，直接认定为成功
                success = True

        # 如果没有缓存的档案，则回退到主动获取模式
        if not success:
            logger.warning(
                f"安检流程：未发现缓存的档案 for '{self.app_name}'，将回退到主动获取模式。"
            )
            success, profile_data = await inspect_and_initialize_self_profile(
                entity_service=container.entity_graph_service,
                action_handler=container.action_handler,
                platform_id=self.app_name,
            )

        if success and profile_data:
            logger.success("[QQBuilder] 安检成功，获取到自身档案。")
            bot_id = profile_data.get("user_id")
            if bot_id:
                container.application_manager.set_self_bot_id_for_platform(
                    self.app_name,
                    str(bot_id)
                )
        else:
            logger.critical(f"[QQBuilder] 安检失败！app '{self.app_name}' 的功能将严重受影响。")

    async def on_before_start(self, container: ServiceContainer) -> tuple[bool, str | None]:
        """QQ启动前检查是否已完成安检."""
        if not container.application_manager.get_self_bot_ids_map().get(self.app_name):
            error_message = (
                "无法启动应用 'QQ'。\n"
                "原因：QQ 应用尚未完成身份检查。"
            )
            logger.error(error_message.replace('\n', ' '))
            return False, error_message
        return True, None

    async def on_after_start(self, container: ServiceContainer, app_id: str) -> Window:
        """QQ启动后创建主窗口。."""
        return Window(
            name="qq_main",
            parent_app_id=app_id,
            title="QQ",
            window_class="main",
            content_state={"view": "conversation_list"}
        )

    # OS 实时事件处理器
    async def handle_os_level_event(
        self,
        event: ProtocolEvent,
        container: ServiceContainer
    ) -> None:
        """处理分发到 OS 层的实时事件，主要用于触发 UI 变化，如弹窗."""
        # 目前只关心新消息事件
        if not event.event_type.startswith("message."):
            return

        # 获取 OS 层服务
        window_manager = container.window_manager
        application_manager = container.application_manager
        entity_service = container.entity_graph_service

        # --- 弹窗决策逻辑 ---
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
                w for w in reversed(window_manager.get_all_windows_sorted())
                if w.status != WindowStatus.MINIMIZE
            ),
            None,
        )

        target_conv_uid = build_conversation_entity_uid(
            event.get_platform(),
            event.conversation_info.type,
            event.conversation_info.conversation_id
        )

        if active_window and active_window.content_state.get("conversation_uid") == target_conv_uid:
            return

        # --- 所有条件满足，创建弹窗 ---
        logger.info(f"[QQBuilder] 检测到来自 '{target_conv_uid}' 的新消息，触发弹窗。")

        # 通过 app_name 查找应用信息
        app_info = next(
            (app for app in application_manager.get_all_apps() if app.name == self.app_name),
            None
        )
        if not app_info:
            logger.error(f"无法为新消息创建弹窗，因为找不到 name 为 '{self.app_name}' 的应用定义。")
            return

        conv_doc = await entity_service.get_entity_by_key(target_conv_uid)
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
            popup_type='interactive',
            transient_cycles_remaining=1, # 显示一轮
        )
        window_manager.open_window(popup)

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
        generate_semantic_id: callable
    ) -> QQWindowRenderer:
        """按需创建或返回渲染器实例."""
        if self._renderer is None:
            self._renderer = QQWindowRenderer(
                entity_service,
                event_service,
                ui_mapping,
                generate_semantic_id
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
            entity_service,
            event_service,
            ui_mapping,
            generate_semantic_id
        )
        await renderer.render_content(
            parent_element,
            current_path,
            window,
            bot_ids_map,
            image_collector
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
            entity_service,
            event_service,
            ui_mapping,
            generate_semantic_id
        )
        await renderer.render_popup_content(
            parent_element,
            current_path,
            window,
        )

    def get_action_definitions(self, window_manager: WindowManager) -> dict:
        """动态定义 QQ 平台的所有动作，特别是 send_message."""
        # 这个方法现在只定义了 send_message
        # 1. 查找所有当前可见的聊天窗口，并提取其对应的会话 UID
        visible_conv_uids = [
            window.content_state.get("conversation_uid")
            for window in window_manager.get_all_windows_sorted()
            if window.window_class == "conversation"
                and window.status != WindowStatus.MINIMIZE
                and window.content_state.get("conversation_uid")
        ]
        # 2. 构建 send_message 的 Schema
        send_message_schema = {
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
                    },
                    "steps": {
                        "type": "array",
                        "description": (
                            "一个指令对象序列，用于构建最终要发送的消息内容。"
                            "不同指令可以组合使用。例如，先@某人再说你好："
                            "`[{\"command\": \"at\", ...}, {\"command\": \"text\", ...}]`。"
                            "使用`send_and_compose_next`可分多条发送。"
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "command": {
                                    "title": "操作指令",
                                    "type": "string",
                                    "description": (
                                        "选择一个具体的操作指令。"
                                        "这个指令将决定下方`params`对象必须采用的结构。"
                                    ),
                                    "enum": [
                                        "reply",
                                        "at",
                                        "text",
                                        "sticker",
                                        "send_and_compose_next"
                                    ],
                                },
                                "params": {
                                    "type": "object",
                                    "description": (
                                        "一个字典，必须且只能包含一个与上方`command`值对应的键值对。规则如下：\n"
                                        "- 当 command 为 'reply' 时, "
                                        "params 必须为 message_id\": \"...\"} "
                                        "(仅在需要明确上下文时使用，避免滥用)。\n"
                                        "- 当 command 为 'at' 时, "
                                        "params 必须为 {\"user_id\": \"...\"} "
                                        "(ID从user_logs获取，仅在需要特别提醒某人时使用)。\n"
                                        "- 当 command 为 'text' 时, "
                                        "params 必须为 {\"content\": \"...\"} "
                                        "(建议内容简短自然，可省略主语和大部分标点)。\n"
                                        "- 当 command 为 'sticker' 时, "
                                        "params 必须为 {\"sticker_id\": \"...\"} "
                                        "(ID从sticker_collection_preview获取)。\n"
                                        "- 当 command 为 'send_and_compose_next' 时, "
                                        "params 必须为空对象 {}"
                                        "(此指令会触发一次发送操作，将其前面所有的指令作为一条消息发送出去。它也标志着下一条新消息的开始)。"
                                    )
                                },
                            },
                            "required": ["command", "params"],
                        },
                    },
                    "motivation": {"type": "string"},
                },
                "required": ["target_conversation_uid", "steps", "motivation"],
            }
        # 3. 如果找到了可见的聊天窗口，就动态添加 enum 约束
        if visible_conv_uids:
            send_message_schema["properties"]["target_conversation_uid"]["enum"] = visible_conv_uids
        else:
            # 如果没有可见的聊天窗口，不返回 send_message 动作
            return {}

        return {"send_message": send_message_schema}

    def build_action_event(
            self,
            action_name: str,
            params: dict,
            bot_id: str
        ) -> Event | None:
        """将 Core 的指令转换成发往 Adapter 的标准 Event."""
        if action_name == "send_message":
            # send_message 的逻辑现在由 DecisionDispatcher 直接处理，这里可以留空或返回一个通用结构
            # 为保持一致性，我们仍然构建一个事件
            final_event_type = f"action.{self.platform_id}.{action_name}"
            action_seg = Seg(type="action_params", data=params)
            return Event(
                event_id=str(uuid.uuid4()),
                event_type=final_event_type,
                time=int(time.time() * 1000),
                bot_id=bot_id,
                content=[action_seg],
            )
        return None
