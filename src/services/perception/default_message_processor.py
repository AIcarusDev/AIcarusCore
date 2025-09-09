# 文件路径: src/services/perception/default_message_processor.py

import dataclasses
import hashlib

from aicarus_protocols import Event as ProtocolEvent
from aicarus_protocols import UserInfo as ProtocolUserInfo
from src.common.custom_logging.logging_config import get_logger
from src.common.intelligent_interrupt_system.models import SemanticModel
from src.common.interruption_broker import InterruptionEventBroker
from src.common.narrative_vectorizer.narrative_vectorizer import NarrativeVectorizer
from src.config import config
from src.domain.models import Stimulus
from src.services.database import (
    ActionLogStorageService,
    EntityGraphService,
)
from src.services.database.services.event_storage_service import EventStorageService
from src.services.database.services.media_cache_service import MediaCacheService
from src.services.perception.image_analysis_service import ImageAnalysisService
from websockets.server import WebSocketServerProtocol

logger = get_logger(__name__)


class DefaultMessageProcessor:
    """事件预处理器 & 领域事件转换器.

    接收原始事件，进行实体化、信息增强、持久化，
    最终将 `Stimulus` 领域模型发布到 InterruptionBroker，仅为 Mind 感知流水线服务。
    """

    def __init__(
        self,
        event_service: EventStorageService,
        entity_service: EntityGraphService,
        action_log_service: ActionLogStorageService,
        image_analysis_service: "ImageAnalysisService",
        semantic_model: "SemanticModel",
        media_cache_service: "MediaCacheService",
        interruption_broker: "InterruptionEventBroker",
        narrative_vectorizer: "NarrativeVectorizer",
    ) -> None:
        self.event_service = event_service
        self.entity_service = entity_service
        self.action_log_service = action_log_service
        self.semantic_model = semantic_model
        self.media_cache_service = media_cache_service
        self.interruption_broker = interruption_broker
        self.narrative_vectorizer = narrative_vectorizer
        self.image_analysis_service = image_analysis_service
        logger.info("DefaultMessageProcessor (纯净版) 初始化完成。")

    async def process_event(
        self,
        proto_event: ProtocolEvent,
        websocket: WebSocketServerProtocol,
        needs_persistence: bool = True,
    ) -> None:
        """处理来自适配器的事件."""
        # [NEW] 专门处理图片加载失败的逻辑
        if any(seg.type == "image_failed" for seg in proto_event.content):
            await self._handle_image_failed_event(proto_event)
            return  # 提前终止，不进入常规处理流程

        if not (platform_id := proto_event.get_platform()):
            logger.error(f"无法从事件类型 '{proto_event.event_type}' 解析平台ID，处理中止。")
            return

        logger.debug(
            f"[Mind Pipeline] 开始处理事件: {proto_event.event_type}, ID: {proto_event.event_id}"
        )

        try:
            saved_event_doc = await self._handle_event_persistence(
                proto_event, platform_id, needs_persistence
            )
            await self._dispatch_event_action(proto_event, saved_event_doc)
        except Exception as e:
            logger.error(
                f"处理事件 (ID: {proto_event.event_id}) 的核心逻辑中发生错误: {e}", exc_info=True
            )

    async def _handle_image_failed_event(self, event: ProtocolEvent) -> None:
        """当检测到图片处理失败时，直接生成一个回复并发布."""
        logger.warning(f"检测到图片处理失败事件 (ID: {event.event_id})，将直接生成失败反馈。")
        failed_seg = next((seg for seg in event.content if seg.type == "image_failed"), None)
        if not failed_seg:
            return

        reason = failed_seg.data.get("reason", "未知错误")
        error_message = (
            f"抱歉，图片加载失败了({reason})，"
            "可能是链接失效或网络问题，可以尝试再发一次吗？"
        )
        logger.info(f"向用户发送的错误消息: {error_message}")

        # 构建一个 Stimulus，其内容是直接回复用户
        stimulus = Stimulus.from_protocol_event(event)
        stimulus.text_content = error_message  # 我们要让AI说的话
        stimulus.is_direct_command = True  # 标记为直接指令，让思考逻辑直接执行回复

        await self.interruption_broker.publish(stimulus)
        logger.debug(f"为图片加载失败事件 '{event.event_id}' 生成的直接回复 Stimulus 已发布。")

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
                    data["hash"] = full_hash
                    logger.debug(
                        f"为事件 {event_dict.get('event_id')} 中的图片注入哈希: {full_hash[:8]}"
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

        # 在事件持久化之前，先处理媒体文件
        content_copy = list(event.content) # 创建副本以安全修改
        for seg in content_copy:
            if seg.type in ["image", "video"] and seg.data.get("base64") and seg.data.get("hash"):
                await self.media_cache_service.save_image_b64(
                    seg.data["hash"],
                    seg.data["base64"],
                    seg.data.get("mime_type", "application/octet-stream")
                )
                # 从事件中移除base64，减轻数据库负担
                del seg.data["base64"]

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
                logger.debug(f"事件 {event.event_id} 成功升维为叙事向量。")
            elif text_content := event.get_text_content():
                embedding_vector = self.semantic_model.encode([text_content])[0]
                event_dict["embedding"] = embedding_vector.tolist()

        if await self.event_service.save_event_document(event_dict):
            logger.debug(f"事件文档 '{event.event_id}' 已保存。")
            if any(seg.type == "image" for seg in event.content) and self.image_analysis_service:
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

        conversation_name = conv_info.name
        if conv_info.type == "private":
            conversation_name = sender_user_info.user_nickname

        conv_entity = await self.entity_service.get_or_create_conversation_entity(
            conversation_id=str(conv_info.conversation_id),
            platform=platform_id,
            conv_type=conv_info.type,
            name=conversation_name,
        )
        if not conv_entity or not conv_entity._key:
            return sender_profile_id, sender_account_uid
        conv_entity_uid = conv_entity._key

        participants = {sender_account_uid: sender_user_info}
        if sender_user_info.user_id != event.bot_id:
            bot_account_uid = f"{platform_id}_{event.bot_id}"
            bot_user_info = ProtocolUserInfo(
                user_id=event.bot_id, user_nickname=config.persona.bot_name
            )
            participants[bot_account_uid] = bot_user_info

        for acc_uid, user_info in participants.items():
            await self.entity_service.update_presence_in_conversation(
                account_entity_uid=acc_uid,
                conversation_entity_uid=conv_entity_uid,
                user_info=user_info,
            )
        return sender_profile_id, sender_account_uid

    async def _dispatch_event_action(
        self, event: ProtocolEvent, saved_event_doc: dict | None
    ) -> None:
        """只负责发布 Stimulus 到 Broker."""
        stimulus = Stimulus.from_protocol_event(event)
        if saved_event_doc:
            stimulus = dataclasses.replace(
                stimulus,
                embedding=saved_event_doc.get("embedding"),
                narrative_sentence=saved_event_doc.get("narrative_sentence"),
            )
        await self.interruption_broker.publish(stimulus)
        logger.debug(f"领域对象 Stimulus (源自事件 '{event.event_id}') 已发布到中断代理。")
