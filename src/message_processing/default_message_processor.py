# src/message_processing/default_message_processor.py
import time
from typing import TYPE_CHECKING, Optional

from aicarus_protocols import Event as ProtocolEvent
from aicarus_protocols import UserInfo as ProtocolUserInfo
from src.common.custom_logging.logging_config import get_logger
from src.common.intelligent_interrupt_system.models import SemanticModel
from src.common.interruption_broker import InterruptionEventBroker
from src.config import config
from src.database import (
    ActionLogStorageService,
    DBEventDocument,
    EntityGraphService,
)
from src.database.services.event_storage_service import EventStorageService
from src.domain.models import Stimulus
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
        logger.info("DefaultMessageProcessor 初始化完成 (领域驱动改造版)。")

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
            # _handle_event_persistence 内部逻辑不变，它依然负责处理原始协议数据与数据库的交互
            saved_event_doc = await self._handle_event_persistence(
                proto_event, platform_id, needs_persistence
            )

            # --- 步骤 2: 分发事件 ---
            # ======================== [ 核心改造点 ] ========================
            # 即使事件不需要持久化，我们也应该处理它（例如心跳、内部事件等）
            # 并且，分发的是原始的 proto_event，而不是数据库文档
            await self._dispatch_event_action(proto_event, saved_event_doc)
            # =============================================================

        except Exception as e:
            logger.error(
                f"处理事件 (ID: {proto_event.event_id}) 的核心逻辑中发生错误: {e}", exc_info=True
            )

    async def _handle_event_persistence(
        self, event: ProtocolEvent, platform_id: str, needs_persistence: bool
    ) -> dict | None:
        """专门负责事件的身份关联、持久化和会话档案更新."""
        person_id, _ = await self._associate_person_and_update_membership(event, platform_id)

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

            saved_doc_dict = db_event_doc.to_dict()
            if await self.event_service.save_event_document(saved_doc_dict):
                logger.debug(f"事件文档 '{event.event_id}' 已保存。")
                saved_doc = saved_doc_dict

                has_image = any(seg.type == "image" for seg in event.content)
                if has_image and self.image_analysis_service:
                    logger.debug(f"事件 '{event.event_id}' 包含图片，已提交至后台进行分析。")
                    await self.image_analysis_service.submit_event_for_analysis(saved_doc)

        if event.conversation_info and event.conversation_info.conversation_id:
            await self.entity_service.get_or_create_conversation_entity(
                conversation_id=event.conversation_info.conversation_id,
                platform=platform_id,
                conv_type=event.conversation_info.type,
                name=event.conversation_info.name,
                extra=event.conversation_info.extra,
            )
        return saved_doc

    # <--- [核心修改] 重构此方法以实现统一逻辑 ---
    async def _associate_person_and_update_membership(
        self, event: ProtocolEvent, platform_id: str
    ) -> tuple[str | None, str | None]:
        """统一处理事件参与者与会话的关系."""
        # 1. 如果事件没有发送者信息，则无法进行关联
        if not (sender_user_info := event.user_info) or not sender_user_info.user_id:
            return None, None

        # 2. 查找或创建发送者的 Profile 和 Account Entity
        (
            sender_profile_id,
            sender_account_uid,
        ) = await self.entity_service.find_or_create_profile_and_account_entity(
            user_info=sender_user_info, platform=platform_id
        )

        if not sender_account_uid:
            logger.error(f"无法为事件 {event.event_id} 的发送者找到或创建 account_entity_uid。")
            return sender_profile_id, None

        # 3. (可选) 处理好友请求等特殊事件类型
        if event.event_type.endswith("request.friend.add"):
            request_data = event.content[0].data if event.content else {}
            await self.entity_service.update_friend_request_status(
                entity_uid=sender_account_uid,
                flag=request_data.get("request_flag"),
                comment=request_data.get("comment"),
                timestamp=event.time,
            )

        # 4. 注入好友备注
        entity_doc = await self.entity_service.get_entity_by_key(sender_account_uid)
        if entity_doc and (remark := entity_doc.details.friend_remark):
            if not sender_user_info.extra:
                sender_user_info.extra = {}
            sender_user_info.extra["friend_remark"] = remark

        # 5. 如果事件没有会话信息，则关联结束
        if not (conv_info := event.conversation_info) or not conv_info.conversation_id:
            return sender_profile_id, sender_account_uid

        # 6. 统一处理所有参与者在会话中的存在关系
        conversation_entity_uid = f"{platform_id}_{conv_info.type}_{conv_info.conversation_id}"

        # 7. 确定所有需要更新关系的参与者 (使用集合确保唯一性)
        participants_to_update = set()
        # 参与者A: 发送者
        participants_to_update.add((sender_user_info, sender_account_uid))

        # 参与者B: 机器人自身 (如果它不是发送者)
        if sender_user_info.user_id != event.bot_id:
            bot_account_uid = f"{platform_id}_{event.bot_id}"
            bot_user_info = ProtocolUserInfo(
                user_id=event.bot_id, user_nickname=config.persona.bot_name
            )
            participants_to_update.add((bot_user_info, bot_account_uid))

        # 8. 遍历所有参与者，更新他们与会话的关系
        for user_info_obj, acc_uid in participants_to_update:
            # 确定此次更新时，会话的"名称"应该是什么
            # - 对于群聊，名称就是群名
            # - 对于私聊，对任何一方来说，会话的"名称"都是对方的名称
            conversation_name_for_this_update = conv_info.name
            if conv_info.type == "private":
                if acc_uid == sender_account_uid:
                    # 对于发送者来说，会话名是机器人的名字
                    conversation_name_for_this_update = config.persona.bot_name
                else:  # 对于机器人来说，会话名是发送者的名字
                    conversation_name_for_this_update = sender_user_info.user_nickname

            await self.entity_service.update_presence_in_conversation(
                account_entity_uid=acc_uid,
                conversation_entity_uid=conversation_entity_uid,
                user_info=user_info_obj,
                conversation_name=conversation_name_for_this_update,
            )

        return sender_profile_id, sender_account_uid

    async def _dispatch_event_action(
        self, event: ProtocolEvent, saved_event_doc: dict | None
    ) -> None:
        """专门负责根据事件类型和当前状态，决定后续动作.

        这是领域模型转换和事件分发的关键点.
        """
        # 无论事件是什么类型，只要它可能影响核心逻辑，就应该被广播
        # 我们在这里进行“翻译”，将协议对象转换为领域模型
        stimulus = Stimulus.from_protocol_event(event)

        # 检查发言人并重置连续发言计数器 (此逻辑依赖会话状态，保持不变)
        if (
            event.event_type.startswith("message.")
            and self.qq_chat_session_manager
            and self.core_logic
            and (session := self.core_logic._get_current_session())
            and session.conversation_id
            == f"{stimulus.platform}_{stimulus.conversation_type}_{stimulus.conversation_id}"
        ):
            bot_profile = await session.get_bot_profile()
            bot_platform_id = str(bot_profile.get("user_id"))
            if stimulus.sender_id and stimulus.sender_id != bot_platform_id:
                session.reset_consecutive_bot_message_count()

        # 向中断代理发布纯净的领域模型，而不是原始的数据库文档
        await self.interruption_broker.publish(stimulus)
        logger.debug(f"领域对象 Stimulus (源自事件 '{event.event_id}') 已发布到中断代理。")

        # 处理其他需要主动处理的特殊事件 (例如机器人档案更新)
        if event.event_type.endswith(".bot.profile_update"):
            await self._handle_bot_profile_update(event)
        else:
            logger.debug(f"事件类型 '{event.event_type}' 无需在此主动处理。")

    async def _handle_bot_profile_update(self, event: ProtocolEvent) -> None:
        """处理机器人自身档案（如群名片）的更新事件."""
        try:
            if not event.content:
                return
            report_data = event.content[0].data
            conversation_entity_uid = report_data.get("conversation_id")
            update_type = report_data.get("update_type")
            new_value = report_data.get("new_value")

            if not all([conversation_entity_uid, update_type]):
                return

            logger.info(
                f"收到会话实体 '{conversation_entity_uid}' 中祂的档案更新通知: "
                f"'{update_type}' -> '{new_value}'"
            )

            success = await self.entity_service.update_bot_profile_in_conversation(
                conversation_entity_uid=conversation_entity_uid,
                update_type=update_type,
                new_value=new_value,
            )

            if not success:
                logger.error(f"通过服务层更新会话实体 '{conversation_entity_uid}' 档案失败。")
                return

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
