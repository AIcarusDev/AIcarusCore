# 文件: src/message_processing/default_message_processor.py (竞速模式适配版 V1.0)
import time
from typing import TYPE_CHECKING, Optional

from aicarus_protocols import Event as ProtocolEvent
from src.common.custom_logging.logging_config import get_logger
from src.common.intelligent_interrupt_system.models import SemanticModel
from src.common.utils import parse_focus_path
from src.database import (
    ActionLogStorageService,  # 引入ActionLogStorageService
    ConversationStorageService,
    CoreDBCollections,
    DBEventDocument,
    EnrichedConversationInfo,
    EntityGraphService,
)
from src.database.services.event_storage_service import EventStorageService
from src.focus_chat_mode.chat_session_manager import ChatSessionManager
from websockets.server import WebSocketServerProtocol

if TYPE_CHECKING:
    from src.core_communication.core_ws_server import CoreWebsocketServer
    from src.core_logic.consciousness_flow import CoreLogic as CoreLogicFlow

logger = get_logger(__name__)


class DefaultMessageProcessor:
    """默认消息处理器.

    它现在拥有了识别“回声”事件并将其正确路由到 ChatSession 的关键能力.
    """

    def __init__(
        self,
        event_service: EventStorageService,
        conversation_service: ConversationStorageService,
        entity_service: EntityGraphService,
        action_log_service: ActionLogStorageService,  # 注入 ActionLog 服务
        semantic_model: "SemanticModel",
        core_websocket_server: Optional["CoreWebsocketServer"] = None,
        qq_chat_session_manager: Optional["ChatSessionManager"] = None,
    ) -> None:
        self.event_service: EventStorageService = event_service
        self.conversation_service: ConversationStorageService = conversation_service
        self.entity_service: EntityGraphService = entity_service
        self.action_log_service: ActionLogStorageService = (
            action_log_service  # 保存 ActionLog 服务实例
        )
        self.semantic_model: SemanticModel = semantic_model
        self.core_comm_layer: CoreWebsocketServer | None = core_websocket_server
        self.qq_chat_session_manager = qq_chat_session_manager
        self.core_logic: CoreLogicFlow | None = None
        logger.info("DefaultMessageProcessor 初始化完成 (竞速模式适配版)。")

    async def _is_self_echo_message(self, event: ProtocolEvent) -> tuple[bool, str | None]:
        """检查一个消息事件是否是AI自身动作的回声.

        Args:
            event: 传入的消息事件.

        Returns:
            一个元组 (is_echo, original_action_id).
                如果事件是回声，is_echo 为 True，且 original_action_id 是触发该回声的原始动作ID.
        """
        platform_message_id = event.get_message_id()
        platform = event.get_platform()

        conversation_id = None
        if event.conversation_info:
            conversation_id = event.conversation_info.conversation_id

        if not all([platform_message_id, platform, conversation_id]):
            return False, None

        action_log = await self.action_log_service.get_action_log_by_platform_message_id(
            platform=platform,
            conversation_id=conversation_id,
            message_id=platform_message_id,
        )
        if action_log:
            original_action_id = action_log.get("action_id")
            logger.debug(
                f"事件 (坐标: P:{platform}, C:{conversation_id}, M:{platform_message_id}) "
                f"被精确识别为动作 '{original_action_id}' 的回声。"
            )
            return True, original_action_id

        return False, None

    async def process_event(
        self,
        proto_event: ProtocolEvent,
        websocket: WebSocketServerProtocol,
        needs_persistence: bool = True,
    ) -> None:
        """处理来自适配器的事件.

        Args:
            proto_event: 传入的 ProtocolEvent 实例.
            websocket: 连接的 WebSocket 协议实例.
            needs_persistence: 是否需要将事件持久化到数据库.
        """
        # --- Guard Clause: 卫语句，提前过滤无效事件 ---
        if not isinstance(proto_event, ProtocolEvent):
            logger.error(f"传入的事件不是 ProtocolEvent 类型，而是 {type(proto_event)}。跳过处理。")
            return

        if not (platform_id := proto_event.get_platform()):
            logger.error(
                f"无法从事件类型 '{proto_event.event_type}' 中解析出平台ID，事件处理中止。"
            )
            return

        logger.debug(f"开始处理事件: {proto_event.event_type}, ID: {proto_event.event_id}")

        try:
            # --- 步骤 1: 门卫 - 检查是否为回声事件 ---
            if proto_event.event_type.startswith("message."):
                is_echo, original_action_id = await self._is_self_echo_message(proto_event)
                if is_echo and original_action_id:
                    # // 识别为回声，直接交给 ChatSession 处理后就“下班”
                    await self._route_echo_to_session(proto_event, original_action_id)
                    return  # // 重点！回声事件处理完就直接返回，不走后面的流程！

            # --- 步骤 2: 档案管理员 - 处理身份关联和数据持久化 ---
            await self._handle_event_persistence(proto_event, platform_id, needs_persistence)

            # --- 步骤 3: 任务分发员 - 根据事件类型和当前状态决定后续操作 ---
            await self._dispatch_event_action(proto_event)

        except Exception as e:
            logger.error(
                f"处理事件 (ID: {proto_event.event_id}) 的核心逻辑中发生错误: {e}", exc_info=True
            )

    async def _route_echo_to_session(self, event: ProtocolEvent, original_action_id: str) -> None:
        """专门负责将回声信号路由到正确的 ChatSession."""
        if self.qq_chat_session_manager and event.conversation_info:
            conv_id = event.conversation_info.conversation_id
            if session := self.qq_chat_session_manager.sessions.get(conv_id):
                await session.signal_echo_received(original_action_id)
            else:
                logger.warning(f"收到回声但找不到会话 '{conv_id}' 来接收信号。")

    async def _handle_event_persistence(
        self, event: ProtocolEvent, platform_id: str, needs_persistence: bool
    ) -> None:
        """专门负责事件的身份关联、持久化和会话档案更新."""
        # 1. 关联 Person 和 Account
        person_id, _ = await self._associate_person_and_update_membership(event, platform_id)

        # 2. 持久化 Event 文档
        if needs_persistence:
            db_event_doc = DBEventDocument.from_protocol(event)
            db_event_doc.person_id_associated = person_id

            if (
                event.event_type.startswith("message.")
                and self.semantic_model
                and (text_content := event.get_text_content())
            ):
                embedding_vector = self.semantic_model.encode([text_content])[0]
                db_event_doc.embedding = embedding_vector.tolist()

            await self.event_service.save_event_document(db_event_doc.to_dict())
            logger.debug(f"事件文档 '{event.event_id}' 已保存。")

        # 3. 更新 Conversation 档案
        if event.conversation_info and event.conversation_info.conversation_id:
            enriched_info = EnrichedConversationInfo.from_protocol_and_event_context(
                proto_conv_info=event.conversation_info,
                event_platform=platform_id,
                event_bot_id=event.bot_id,
            )
            await self.conversation_service.upsert_conversation_document(
                enriched_info.to_db_document()
            )

    async def _associate_person_and_update_membership(
        self, event: ProtocolEvent, platform_id: str
    ) -> tuple[str | None, str | None]:
        """封装身份关联和成员信息更新的逻辑.

        - 优先处理好友请求事件，更新实体的待处理状态。
        - 对于所有事件，查找或创建实体，并用好友备注丰富事件信息。
        - 对于会话内事件，更新成员关系。
        """
        # 如果事件没有用户信息，则无法进行关联
        if not (event.user_info and event.user_info.user_id):
            return None, None

        # --- 步骤 1: 查找或创建核心的 Profile 和 Entity ---
        # 这是所有后续操作的基础
        profile_id, entity_uid = await self.entity_service.find_or_create_profile_and_entity(
            user_info=event.user_info, platform=platform_id
        )

        if not entity_uid:
            # 如果连最基础的实体都无法创建或找到，后续操作无法进行
            logger.error(f"无法为事件 {event.event_id} 找到或创建 entity_uid，身份关联中止。")
            return profile_id, None

        # --- 步骤 2: 根据事件类型执行特定的数据库更新 ---
        entities_collection = await self.entity_service._get_collection(CoreDBCollections.ENTITIES)

        # <!-- 新增逻辑：专门处理好友请求 -->
        if event.event_type.endswith("request.friend.add"):
            request_data = event.content[0].data if event.content else {}
            flag = request_data.get("request_flag")
            comment = request_data.get("comment")

            # 将好友请求信息更新到实体的 'friend_request_pending' 字段
            await entities_collection.update(
                {
                    "_key": entity_uid,
                    "friend_request_pending": {
                        "flag": flag,
                        "comment": comment,
                        "timestamp": event.time
                    }
                }
            )
            logger.info(f"已将实体 '{entity_uid}' 的好友请求标记为待处理。")

        # --- 步骤 3: 丰富事件信息（注入好友备注） ---
        # 这个逻辑对所有类型的事件都适用
        entity_doc = await entities_collection.get(entity_uid)
        if entity_doc and (remark := entity_doc.get("friend_remark")):
            # 如果数据库中有备注，就把它“塞”进当前事件的 user_info 里
            if event.user_info.extra is None:
                event.user_info.extra = {}
            event.user_info.extra['friend_remark'] = remark
            logger.debug(f"已为事件 '{event.event_id}' (来自 {entity_uid}) 注入好友备注。")

        # --- 步骤 4: 更新在会话中的存在信息 (Membership) ---
        # 这个逻辑只对发生在具体会话中的事件有效
        if event.conversation_info:
            await self.entity_service.update_presence_in_conversation(
                entity_uid=entity_uid,
                conversation_id=event.conversation_info.conversation_id,
                user_info=event.user_info,
                conversation_name=event.conversation_info.name,
            )

        return profile_id, entity_uid

    async def _dispatch_event_action(self, event: ProtocolEvent) -> None:
        """专门负责根据事件类型和当前状态，决定是否触发核心逻辑."""
        # 1. 检查新消息是否来自当前专注的会话，如果是，则触发思考
        if self.core_logic and self.core_logic.chat_session_manager and event.conversation_info:
            focus_entry = self.core_logic.chat_session_manager.current_focus_path
            current_focus_path_str = (
                focus_entry.get("target_path") if isinstance(focus_entry, dict) else focus_entry
            )
            _, _, current_conv_id = parse_focus_path(current_focus_path_str)

            if event.conversation_info.conversation_id == current_conv_id:
                await self._handle_focused_conversation_event(event, current_conv_id)

        # 2. 处理其他需要主动处理的特殊事件
        if event.event_type.endswith(".bot.profile_update"):
            await self._handle_bot_profile_update(event)
        else:
            logger.debug(f"事件类型 '{event.event_type}' 无需在此主动处理，交由核心循环自行发现。")

    async def _handle_focused_conversation_event(self, event: ProtocolEvent, conv_id: str) -> None:
        """处理来自当前专注会话的事件."""
        if session := self.core_logic.chat_session_manager.sessions.get(conv_id):
            bot_profile = await session.get_bot_profile()
            current_bot_id = str(bot_profile.get("user_id") or session.bot_id)

            sender_id = (
                str(event.user_info.user_id)
                if event.user_info and event.user_info.user_id
                else None
            )

            # 如果是别人发的消息，就重置我方连续发言计数器
            if sender_id and sender_id != current_bot_id:
                session.reset_consecutive_bot_message_count()

        logger.info(f"收到当前专注会话 '{conv_id}' 的新消息，触发立即思考。")
        self.core_logic.trigger_immediate_thought_cycle()

    async def _handle_bot_profile_update(self, event: ProtocolEvent) -> None:
        # (这个方法的逻辑保持不变)
        try:
            if not event.content:
                return
            report_data = event.content[0].data
            conversation_id = report_data.get("conversation_id")
            update_type = report_data.get("update_type")
            new_value = report_data.get("new_value")
            if not conversation_id or not update_type:
                return

            logger.info(
                f"收到会话 '{conversation_id}' 中祂的档案更新通知: '{update_type}' -> '{new_value}'"
            )
            session = (
                self.qq_chat_session_manager.sessions.get(conversation_id)
                if self.qq_chat_session_manager
                else None
            )
            if session:
                logger.info(f"会话 '{conversation_id}' 处于激活状态，正在实时更新其祂的档案缓存。")
                if update_type == "card_change":
                    session.bot_profile_cache["card"] = new_value
                session.last_profile_update_time = time.time()
                profile_to_save = session.bot_profile_cache.copy()
                profile_to_save["updated_at"] = int(time.time() * 1000)
                await self.conversation_service.update_conversation_field(
                    conversation_id, "bot_profile_in_this_conversation", profile_to_save
                )
            else:
                logger.info(f"会话 '{conversation_id}' 不活跃，仅更新其在数据库中祂的档案。")
                conv_doc = await self.conversation_service.get_conversation_document_by_id(
                    conversation_id
                )
                profile_to_update = {}
                if (
                    conv_doc
                    and conv_doc.get("bot_profile_in_this_conversation")
                    and isinstance(conv_doc["bot_profile_in_this_conversation"], dict)
                ):
                    profile_to_update = conv_doc["bot_profile_in_this_conversation"]
                if update_type == "card_change":
                    profile_to_update["card"] = new_value
                profile_to_update["updated_at"] = int(time.time() * 1000)
                await self.conversation_service.update_conversation_field(
                    conversation_id, "bot_profile_in_this_conversation", profile_to_update
                )
        except Exception as e:
            logger.error(f"处理祂的档案更新通知时出错: {e}", exc_info=True)
