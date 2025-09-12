# 文件路径: src/services/perception/default_message_processor.py

import asyncio
import dataclasses
from typing import TYPE_CHECKING

from aicarus_protocols import Event as ProtocolEvent
from aicarus_protocols import UserInfo as ProtocolUserInfo
from src.common.custom_logging.logging_config import get_logger
from src.common.intelligent_interrupt_system.models import SemanticModel
from src.common.interruption_broker import InterruptionEventBroker
from src.common.narrative_vectorizer.narrative_vectorizer import NarrativeVectorizer
from src.common.utils import build_conversation_entity_uid
from src.config import config
from src.domain.models import Stimulus
from src.os.models import WindowStatus
from src.services.database import (
    ActionLogStorageService,
    EntityGraphService,
)
from src.services.database.services.event_storage_service import EventStorageService
from src.services.database.services.media_cache_service import MediaCacheService
from src.services.perception.image_analysis_service import ImageAnalysisService
from websockets.server import WebSocketServerProtocol

if TYPE_CHECKING:
    from src.os.window_manager import WindowManager
    from src.services.action.action_handler import ActionHandler

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
        window_manager: "WindowManager",
        action_handler: "ActionHandler",
    ) -> None:
        self.event_service = event_service
        self.entity_service = entity_service
        self.action_log_service = action_log_service
        self.semantic_model = semantic_model
        self.media_cache_service = media_cache_service
        self.interruption_broker = interruption_broker
        self.narrative_vectorizer = narrative_vectorizer
        self.image_analysis_service = image_analysis_service
        self.window_manager = window_manager
        self.action_handler = action_handler
        self._background_tasks: set[asyncio.Task] = set()
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
            # 核心修复：在事件持久化后，立即检查并更新激活窗口的已读时间戳。
            if proto_event.event_type.startswith("message."):
                await self._update_timestamp_for_active_chat(proto_event)

            await self._dispatch_event_action(proto_event, saved_event_doc)
        except Exception as e:
            logger.error(
                f"处理事件 (ID: {proto_event.event_id}) 的核心逻辑中发生错误: {e}", exc_info=True
            )

    async def _update_timestamp_for_active_chat(self, event: ProtocolEvent) -> None:
        """如果消息来自一个当前打开的聊天窗口，则立即更新其已读时间戳."""
        if not event.conversation_info or not event.conversation_info.conversation_id:
            return

        conv_uid = build_conversation_entity_uid(
            event.get_platform(),
            event.conversation_info.type,
            event.conversation_info.conversation_id,
        )

        # 检查是否有窗口匹配此 UID 且处于激活状态 (非最小化)
        is_window_active = any(
            w.content_state.get("conversation_uid") == conv_uid
            and w.status != WindowStatus.MINIMIZE
            for w in self.window_manager.get_all_windows_sorted()
        )

        if is_window_active:
            logger.debug(
                f"消息来自激活的聊天窗口 '{conv_uid}'，实时更新已读时间戳至 {event.time}。"
            )
            await self.entity_service.update_conversation_last_read_timestamp(
                conv_uid, float(event.time)
            )

    async def _handle_image_failed_event(self, event: ProtocolEvent) -> None:
        """当检测到图片处理失败时，直接生成一个回复并发布."""
        logger.warning(f"检测到图片处理失败事件 (ID: {event.event_id})，将直接生成失败反馈。")
        failed_seg = next((seg for seg in event.content if seg.type == "image_failed"), None)
        if not failed_seg:
            return

        reason = failed_seg.data.get("reason", "未知错误")
        error_message = (
            f"抱歉，图片加载失败了({reason})，可能是链接失效或网络问题，可以尝试再发一次吗？"
        )
        logger.info(f"向用户发送的错误消息: {error_message}")

        # 构建一个 Stimulus，其内容是直接回复用户
        stimulus = Stimulus.from_protocol_event(event)
        # 正确的用法是使用 dataclasses.replace
        stimulus = dataclasses.replace(stimulus, text_content=error_message)

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
                    # 使用 image_analysis_service 的内部方法来确保哈希算法一致
                    full_hash = self.image_analysis_service._calculate_image_hash(b64)
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

        event_dict = event.to_dict()
        self._calculate_and_inject_hashes(event_dict)

        # 在事件持久化之前，先处理媒体文件
        content_copy = event_dict.get("content", [])
        for seg in content_copy:
            if (
                seg.get("type") in ["image", "video"]
                and seg.get("data", {}).get("base64")
                and seg.get("data", {}).get("hash")
            ):
                await self.media_cache_service.save_image_b64(
                    seg["data"]["hash"],
                    seg["data"]["base64"],
                    seg["data"].get("mime_type", "application/octet-stream"),
                )
                # 从事件中移除base64，减轻数据库负担
                del seg["data"]["base64"]

        event_dict["platform"] = platform_id
        event_dict["person_id_associated"] = person_id

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
            if (
                any(seg.get("type") == "image" for seg in event_dict.get("content", []))
                and self.image_analysis_service
            ):
                await self.image_analysis_service.submit_event_for_analysis(event_dict)
            return event_dict
        return None

    async def _associate_person_and_update_membership(
        self, event: ProtocolEvent, platform_id: str
    ) -> tuple[str | None, str | None]:
        """统一处理事件参与者与会话的关系，并触发按需同步."""
        if not (sender_user_info := event.user_info) or not sender_user_info.user_id:
            return None, None

        # 1. 查找或创建发送者的实体
        (
            sender_profile_id,
            sender_account_uid,
        ) = await self.entity_service.find_or_create_profile_and_account_entity(
            user_info=sender_user_info, platform=platform_id
        )

        if not sender_account_uid:
            logger.error(f"无法为事件 {event.event_id} 的发送者找到或创建 account_entity_uid。")
            return sender_profile_id, None

        # 处理好友请求
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

        # 2. 查找或创建会话实体
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

        # 3. 立即、精准地更新当前发言人的信息
        await self.entity_service.update_presence_in_conversation(
            account_entity_uid=sender_account_uid,
            conversation_entity_uid=conv_entity_uid,
            user_info=sender_user_info,
        )
        logger.debug(f"已更新发言者 {sender_account_uid} 在会话 {conv_entity_uid} 中的存在信息。")

        # 4. 检查是否需要触发对整个群的后台批量同步 (哨兵逻辑)
        if conv_info.type == "group":
            # action_handler 在 __init__ 中已经注入，可以直接使用
            if hasattr(self, 'action_handler') and self.action_handler:
                if await self.entity_service.is_group_sync_due(
                    conv_entity_uid, ttl_seconds=86400
                ): # 24小时同步一次
                    logger.info(
                        f"检测到群聊 '{conv_entity_uid}' 需要进行后台成员列表同步，"
                        f"已启动任务。"
                    )
                    sync_task = asyncio.create_task(
                        self.action_handler.trigger_group_member_sync(conv_entity_uid)
                    )
                    self._background_tasks.add(sync_task)
                    sync_task.add_done_callback(self._background_tasks.discard)
            else:
                logger.error(
                    "ActionHandler 未在 DefaultMessageProcessor 中初始化，"
                    "无法触发后台同步。"
                )

        # 5. 更新机器人自身在会话中的存在
        if sender_user_info.user_id != event.bot_id:
            bot_account_uid = f"{platform_id}_{event.bot_id}"
            bot_user_info = ProtocolUserInfo(
                user_id=event.bot_id, user_nickname=config.persona.bot_name
            )
            await self.entity_service.update_presence_in_conversation(
                account_entity_uid=bot_account_uid,
                conversation_entity_uid=conv_entity_uid,
                user_info=bot_user_info,
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
