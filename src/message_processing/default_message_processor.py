# src/message_processing/default_message_processor.py
import dataclasses
import hashlib
import time
from typing import TYPE_CHECKING, Optional

from aicarus_protocols import Event as ProtocolEvent
from aicarus_protocols import UserInfo as ProtocolUserInfo
from src.common.custom_logging.logging_config import get_logger
from src.common.intelligent_interrupt_system.models import SemanticModel
from src.common.interruption_broker import InterruptionEventBroker
from src.common.narrative_vectorizer.narrative_vectorizer import NarrativeVectorizer
from src.common.utils import build_conversation_entity_uid
from src.config import config
from src.database import (
    ActionLogStorageService,
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
    """默认消息处理器."""

    def __init__(
        self,
        event_service: EventStorageService,
        entity_service: EntityGraphService,
        action_log_service: ActionLogStorageService,
        image_analysis_service: "ImageAnalysisService",
        semantic_model: "SemanticModel",
        interruption_broker: "InterruptionEventBroker",
        narrative_vectorizer: "NarrativeVectorizer",
        core_websocket_server: Optional["CoreWebsocketServer"] = None,
        qq_chat_session_manager: Optional["ChatSessionManager"] = None,
    ) -> None:
        self.event_service: EventStorageService = event_service
        self.entity_service: EntityGraphService = entity_service
        self.action_log_service: ActionLogStorageService = action_log_service
        self.semantic_model: SemanticModel = semantic_model
        self.interruption_broker = interruption_broker
        self.narrative_vectorizer = narrative_vectorizer
        self.core_comm_layer: CoreWebsocketServer | None = core_websocket_server
        self.qq_chat_session_manager = qq_chat_session_manager
        self.core_logic: CoreLogicFlow | None = None
        self.image_analysis_service: ImageAnalysisService | None = image_analysis_service
        logger.info("DefaultMessageProcessor 初始化完成。")

    async def process_event(
        self,
        proto_event: ProtocolEvent,
        websocket: WebSocketServerProtocol,
        needs_persistence: bool = True,
    ) -> None:
        """处理来自适配器的事件."""
        if not isinstance(proto_event, ProtocolEvent):
            logger.error(f"传入的事件不是 ProtocolEvent 类型，而是 {type(proto_event)}。跳过处理。")
            return

        if not (platform_id := proto_event.get_platform()):
            logger.error(
                f"无法从事件类型 '{proto_event.event_type}' 中解析出平台ID，事件处理中止。"
            )
            return

        logger.debug(f"开始处理事件: {proto_event.event_type}, ID: {proto_event.event_id}")

        if (
            self.qq_chat_session_manager
            and proto_event.event_type.startswith("message.")
            and proto_event.user_info
            and proto_event.user_info.user_id != proto_event.bot_id
            and proto_event.conversation_info
            and proto_event.conversation_info.conversation_id
            and proto_event.conversation_info.type
        ):
            conv_entity_uid = build_conversation_entity_uid(
                platform_id,
                proto_event.conversation_info.type,
                proto_event.conversation_info.conversation_id,
            )
            session = self.qq_chat_session_manager.sessions.get(conv_entity_uid)
            if session:
                session.reset_consecutive_bot_message_count()

        try:
            saved_event_doc = await self._handle_event_persistence(
                proto_event, platform_id, needs_persistence
            )
            await self._dispatch_event_action(proto_event, saved_event_doc)

        except Exception as e:
            logger.error(
                f"处理事件 (ID: {proto_event.event_id}) 的核心逻辑中发生错误: {e}", exc_info=True
            )

    def _calculate_and_inject_hashes(self, event_dict: dict) -> None:
        """遍历事件内容，为图片Seg计算并注入哈希值."""
        content = event_dict.get("content")
        if not isinstance(content, list):
            return

        for seg in content:
            if (
                isinstance(seg, dict)
                and seg.get("type") == "image"
                and (data := seg.get("data"))
                and isinstance(data, dict)
                and (b64 := data.get("base64"))
            ):
                try:
                    full_hash = hashlib.sha256(b64.encode("utf-8")).hexdigest()
                    short_hash = full_hash[:8]
                    data["hash"] = short_hash
                    logger.debug(
                        f"为事件 {event_dict.get('event_id')} 中的图片注入哈希: {short_hash}"
                    )
                except Exception as e:
                    logger.error(f"为事件 {event_dict.get('event_id')} 的图片计算哈希时出错: {e}")

    async def _handle_event_persistence(
        self, event: ProtocolEvent, platform_id: str, needs_persistence: bool
    ) -> dict | None:
        """专门负责事件的身份关联、持久化和会话档案更新."""
        person_id, _ = await self._associate_person_and_update_membership(event, platform_id)

        if not needs_persistence:
            return None

        event_dict = event.to_dict()
        event_dict["platform"] = platform_id
        event_dict["person_id_associated"] = person_id
        self._calculate_and_inject_hashes(event_dict)

        if self.narrative_vectorizer and event.event_type.startswith("message."):
            logger.debug(f"事件 {event.event_id} 正在进入叙事化向量流程...")
            sentence, vector = await self.narrative_vectorizer.build_and_vectorize(event)
            if sentence and vector:
                event_dict["narrative_sentence"] = sentence
                event_dict["embedding"] = vector
                logger.info(f"事件 {event.event_id} 成功升维为叙事向量。")
            else:
                logger.warning(f"事件 {event.event_id} 叙事化向量失败，将使用纯文本向量作为后备。")
                if text_content := event.get_text_content():
                    embedding_vector = self.semantic_model.encode([text_content])[0]
                    event_dict["embedding"] = embedding_vector.tolist()

        if await self.event_service.save_event_document(event_dict):
            logger.debug(f"事件文档 '{event.event_id}' 已保存。")

            has_image = any(seg.type == "image" for seg in event.content)
            if has_image and self.image_analysis_service:
                await self.image_analysis_service.submit_event_for_analysis(event_dict)

            return event_dict

        return None

    async def _associate_person_and_update_membership(
        self, event: ProtocolEvent, platform_id: str
    ) -> tuple[str | None, str | None]:
        """统一处理事件参与者与会话的关系，使用字典避免哈希问题."""
        if not (sender_user_info := event.user_info) or not sender_user_info.user_id:
            return None, None

        (
            sender_profile_id,
            sender_account_uid,
        ) = await self.entity_service.find_or_create_profile_and_account_entity(
            user_info=sender_user_info, platform=platform_id
        )

        if not sender_account_uid:
            logger.error(f"无法为事件 {event.event_id} 的发送者找到或创建 account_entity_uid。")
            return sender_profile_id, None

        if event.event_type.endswith("request.friend.add"):
            request_data = event.content[0].data if event.content else {}
            await self.entity_service.update_friend_request_status(
                entity_uid=sender_account_uid,
                flag=request_data.get("request_flag"),
                comment=request_data.get("comment"),
                timestamp=event.time,
            )

        if not (conv_info := event.conversation_info) or not conv_info.conversation_id:
            return sender_profile_id, sender_account_uid

        # 确定私聊会话的正确名称应为对方的昵称
        conversation_name_for_creation = conv_info.name
        if conv_info.type == "private":
            conversation_name_for_creation = sender_user_info.user_nickname

        conversation_entity = await self.entity_service.get_or_create_conversation_entity(
            conversation_id=str(conv_info.conversation_id),
            platform=platform_id,
            conv_type=conv_info.type,
            name=conversation_name_for_creation,
        )

        if not conversation_entity or not conversation_entity._key:
            logger.error(f"为事件 {event.event_id} 获取或创建 conversation_entity 失败。")
            return sender_profile_id, sender_account_uid
        conversation_entity_uid = conversation_entity._key

        participants_to_update: dict[str, ProtocolUserInfo] = {}

        participants_to_update[sender_account_uid] = sender_user_info

        if sender_user_info.user_id != event.bot_id:
            bot_account_uid = f"{platform_id}_{event.bot_id}"
            bot_user_info = ProtocolUserInfo(
                user_id=event.bot_id, user_nickname=config.persona.bot_name
            )
            participants_to_update[bot_account_uid] = bot_user_info

        for acc_uid, user_info_obj in participants_to_update.items():
            await self.entity_service.update_presence_in_conversation(
                account_entity_uid=acc_uid,
                conversation_entity_uid=conversation_entity_uid,
                user_info=user_info_obj,
            )

        return sender_profile_id, sender_account_uid

    async def _dispatch_event_action(
        self, event: ProtocolEvent, saved_event_doc: dict | None
    ) -> None:
        """专门负责根据事件类型和当前状态，决定后续动作."""
        stimulus = Stimulus.from_protocol_event(event)

        if saved_event_doc:
            embedding_vector = saved_event_doc.get("embedding")
            narrative = saved_event_doc.get("narrative_sentence")

            stimulus = dataclasses.replace(
                stimulus, embedding=embedding_vector, narrative_sentence=narrative
            )

        await self.interruption_broker.publish(stimulus)
        logger.debug(f"领域对象 Stimulus (源自事件 '{event.event_id}') 已发布到中断代理。")

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
