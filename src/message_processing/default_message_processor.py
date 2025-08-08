# src/message_processing/default_message_processor.py
import time
from typing import TYPE_CHECKING, Optional

from aicarus_protocols import Event as ProtocolEvent
from src.common.custom_logging.logging_config import get_logger
from src.common.intelligent_interrupt_system.models import SemanticModel
from src.common.interruption_broker import InterruptionEventBroker
from src.common.utils import build_conversation_entity_uid
from src.database import (
    ActionLogStorageService,
    CoreDBCollections,
    DBEventDocument,
    EntityGraphService,
)
from src.database.services.event_storage_service import EventStorageService
from src.focus_chat_mode.chat_session_manager import ChatSessionManager
from src.message_processing.image_analysis_service import ImageAnalysisService
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
        entity_service: EntityGraphService,
        action_log_service: ActionLogStorageService,  # 注入 ActionLog 服务
        image_analysis_service: "ImageAnalysisService",
        semantic_model: "SemanticModel",
        interruption_broker: "InterruptionEventBroker",
        core_websocket_server: Optional["CoreWebsocketServer"] = None,
        qq_chat_session_manager: Optional["ChatSessionManager"] = None,
    ) -> None:
        self.event_service: EventStorageService = event_service
        self.entity_service: EntityGraphService = entity_service
        self.action_log_service: ActionLogStorageService = (
            action_log_service  # 保存 ActionLog 服务实例
        )
        self.semantic_model: SemanticModel = semantic_model
        self.interruption_broker = interruption_broker
        self.core_comm_layer: CoreWebsocketServer | None = core_websocket_server
        self.qq_chat_session_manager = qq_chat_session_manager
        self.core_logic: CoreLogicFlow | None = None
        self.image_analysis_service: ImageAnalysisService | None = image_analysis_service
        logger.info("DefaultMessageProcessor 初始化完成 (竞速模式适配版)。")

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
            # --- 步骤 1: 持久化并获取保存后的文档 ---
            saved_event_doc = await self._handle_event_persistence(
                proto_event, platform_id, needs_persistence
            )

            # --- 步骤 2: 分发事件 ---
            if saved_event_doc:  # 只有成功保存的事件才会被分发
                await self._dispatch_event_action(saved_event_doc)

        except Exception as e:
            logger.error(
                f"处理事件 (ID: {proto_event.event_id}) 的核心逻辑中发生错误: {e}", exc_info=True
            )

    async def _handle_event_persistence(
        self, event: ProtocolEvent, platform_id: str, needs_persistence: bool
    ) -> dict | None:
        """专门负责事件的身份关联、持久化和会话档案更新."""
        # 1. 关联 Person 和 Account
        person_id, _ = await self._associate_person_and_update_membership(event, platform_id)

        # 2. 持久化 Event 文档
        saved_doc = None
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

            saved_doc_dict = db_event_doc.to_dict()  # <-- 获取要保存的字典
            if await self.event_service.save_event_document(saved_doc_dict):
                logger.debug(f"事件文档 '{event.event_id}' 已保存。")
                saved_doc = saved_doc_dict  # <-- 保存下来

                # 检查事件是否包含图片
                has_image = any(seg.type == "image" for seg in event.content)
                if has_image and self.image_analysis_service:
                    logger.debug(f"事件 '{event.event_id}' 包含图片，已提交至后台进行分析。")
                    # 将已保存的文档字典提交给分析服务
                    await self.image_analysis_service.submit_event_for_analysis(saved_doc)

        # 3. 更新 Conversation 档案
        if event.conversation_info and event.conversation_info.conversation_id:
            # 直接调用新服务，让它处理会话实体的创建或获取
            await self.entity_service.get_or_create_conversation_entity(
                conversation_id=event.conversation_info.conversation_id,
                platform=platform_id,
                conv_type=event.conversation_info.type,
                name=event.conversation_info.name,
                extra=event.conversation_info.extra,
            )
        return saved_doc

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
        (
            profile_id,
            account_entity_uid,
        ) = await self.entity_service.find_or_create_profile_and_account_entity(
            user_info=event.user_info, platform=platform_id
        )

        if not account_entity_uid:
            # 如果连最基础的实体都无法创建或找到，后续操作无法进行
            logger.error(
                f"无法为事件 {event.event_id} 找到或创建 account_entity_uid，身份关联中止。"
            )
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
                    "_key": account_entity_uid,
                    "friend_request_pending": {
                        "flag": flag,
                        "comment": comment,
                        "timestamp": event.time,
                    },
                }
            )
            logger.info(f"已将实体 '{account_entity_uid}' 的好友请求标记为待处理。")

        # --- 步骤 3: 丰富事件信息（注入好友备注） ---
        # 这个逻辑对所有类型的事件都适用
        entity_doc = await entities_collection.get(account_entity_uid)
        if entity_doc and (remark := entity_doc.get("details", {}).get("friend_remark")):
            # 如果数据库中有备注，就把它“塞”进当前事件的 user_info 里
            if not event.user_info.extra:
                event.user_info.extra = {}
            event.user_info.extra["friend_remark"] = remark
            logger.debug(f"已为事件 '{event.event_id}' (来自 {account_entity_uid}) 注入好友备注。")

        # --- 步骤 4: 更新在会话中的存在信息 (Membership) ---
        # 这个逻辑只对发生在具体会话中的事件有效
        if event.conversation_info and event.conversation_info.conversation_id:
            conv_info = event.conversation_info
            conversation_entity_uid = f"{platform_id}_{conv_info.type}_{conv_info.conversation_id}"

            await self.entity_service.update_presence_in_conversation(
                account_entity_uid=account_entity_uid,
                conversation_entity_uid=conversation_entity_uid,
                user_info=event.user_info,
                conversation_name=event.conversation_info.name,
            )

        return profile_id, account_entity_uid

    async def _dispatch_event_action(self, event_doc: dict) -> None:
        """专门负责根据事件类型和当前状态，决定是否触发核心逻辑."""
        event_type = event_doc.get("event_type", "")
        if event_type.startswith("message."):
            # 检查发言人并重置连续发言计数器
            if (
                self.qq_chat_session_manager
                and self.core_logic
                and (session := self.core_logic._get_current_session())
            ):
                # 2. 检查事件是否属于当前会话
                platform = event_doc.get("platform")
                conv_info = event_doc.get("conversation_info", {})
                conv_type = conv_info.get("type")
                conv_id = conv_info.get("conversation_id")
                # 如果平台、会话类型和会话ID都存在
                if platform and conv_type and conv_id:
                    event_session_uid = build_conversation_entity_uid(platform, conv_type, conv_id)
                    # 3. 如果事件属于当前专注的会话
                    if session.conversation_id == event_session_uid:
                        # 4. 检查发言人是否是自己
                        sender_id = str(event_doc.get("user_info", {}).get("user_id", ""))
                        bot_profile = await session.get_bot_profile()
                        bot_platform_id = str(bot_profile.get("user_id"))

                        # 5. 如果发言人不是自己，则重置计数器
                        if sender_id and sender_id != bot_platform_id:
                            session.reset_consecutive_bot_message_count()

            await self.interruption_broker.publish(event_doc)
            logger.debug(f"事件 '{event_doc.get('_key')}' 已发布到中断代理。")

        # 处理其他需要主动处理的特殊事件
        if event_type.endswith(".bot.profile_update"):
            event_obj = ProtocolEvent.from_dict(event_doc)  # 做一次转换
            await self._handle_bot_profile_update(event_obj)
        else:
            logger.debug(f"事件类型 '{event_type}' 无需在此主动处理。")

    async def _handle_bot_profile_update(self, event: ProtocolEvent) -> None:
        """处理机器人自身档案（如群名片）的更新事件."""
        try:
            if not event.content:
                return
            report_data = event.content[0].data
            # 在新架构中，这里的 conversation_id 就是 conversation entity 的 UID
            conversation_entity_uid = report_data.get("conversation_id")
            update_type = report_data.get("update_type")
            new_value = report_data.get("new_value")

            if not all([conversation_entity_uid, update_type]):
                return

            logger.info(
                f"收到会话实体 '{conversation_entity_uid}' 中祂的档案更新通知: "
                f"'{update_type}' -> '{new_value}'"
            )

            # 获取 Entities 集合的句柄
            entities_collection = await self.entity_service._get_collection(
                CoreDBCollections.ENTITIES
            )

            # 获取当前的会话实体文档
            conv_entity_doc = await entities_collection.get(conversation_entity_uid)
            if not conv_entity_doc:
                logger.warning(f"无法更新档案，因为找不到会话实体 '{conversation_entity_uid}'")
                return

            # 更新 bot_profile_in_this_conversation 字段
            profile_to_update = conv_entity_doc.get("bot_profile_in_this_conversation", {})
            if not isinstance(profile_to_update, dict):  # 健壮性检查
                profile_to_update = {}

            if update_type == "card_change":
                profile_to_update["card"] = new_value

            profile_to_update["updated_at"] = int(time.time() * 1000)

            # 将更新后的字段写回数据库
            await entities_collection.update(
                {
                    "_key": conversation_entity_uid,
                    "bot_profile_in_this_conversation": profile_to_update,
                }
            )

            # 如果会话当前处于激活状态，也更新内存中的缓存
            session = (
                self.qq_chat_session_manager.sessions.get(conversation_entity_uid)
                if self.qq_chat_session_manager
                else None
            )
            if session:
                logger.info(
                    f"会话 '{conversation_entity_uid}' 处于激活状态，正在实时更新其档案缓存。"
                )
                if "bot_profile_cache" not in session or not session.bot_profile_cache:
                    session.bot_profile_cache = {}
                if update_type == "card_change":
                    session.bot_profile_cache["card"] = new_value
                session.last_profile_update_time = time.time()

        except Exception as e:
            logger.error(f"处理祂的档案更新通知时出错: {e}", exc_info=True)
