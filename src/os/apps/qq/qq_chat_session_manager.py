# src/os/apps/qq/qq_chat_session_manager.py

import asyncio
import time
from typing import TYPE_CHECKING, Optional

from src.common.custom_logging.logging_config import get_logger
from src.services.action.action_handler import ActionHandler
from src.services.database.models import ConversationDetails, EnrichedConversationInfo
from src.services.database.services.event_storage_service import EventStorageService
from src.services.database.services.thought_storage_service import ThoughtStorageService
from src.services.llmrequest.llm_processor import Client as LLMProcessorClient

from .qq_chat_session import QQChatSession

if TYPE_CHECKING:
    from src.common.intelligent_interrupt_system.intelligent_interrupter import (
        IntelligentInterrupter,
    )
    from src.mind.consciousness_flow import CoreLogic as CoreLogicFlow
    from src.services.database.services.entity_graph_service import EntityGraphService

logger = get_logger(__name__)


class QQChatSessionManager:
    """管理所有 QQChatSession 实例，作为会话对象的工厂和缓存池.

    负责提供会话的业务逻辑和数据上下文。
    """

    def __init__(
        self,
        llm_client: LLMProcessorClient,
        event_storage: EventStorageService,
        action_handler: ActionHandler,
        self_bot_ids_map: dict[str, str],
        intelligent_interrupter: "IntelligentInterrupter",
        entity_graph_service: "EntityGraphService",
        thought_storage_service: "ThoughtStorageService",
        core_logic: Optional["CoreLogicFlow"] = None,
    ) -> None:
        """初始化 QQChatSessionManager."""
        self.llm_client = llm_client
        self.event_storage = event_storage
        self.action_handler = action_handler
        self.self_bot_ids_map = self_bot_ids_map
        self.thought_storage_service = thought_storage_service
        self.intelligent_interrupter = intelligent_interrupter
        self.entity_graph_service = entity_graph_service
        self.core_logic = core_logic
        self.sessions: dict[str, QQChatSession] = {}
        self.lock = asyncio.Lock()

        logger.info("QQChatSessionManager 初始化完成。")

    async def get_or_create_session(self, conversation_entity_uid: str) -> QQChatSession | None:
        """
        根据会话实体的UID获取或创建QQChatSession.
        【增强版】: 增加了重试机制，以处理因数据同步延迟导致实体暂未创建的情况。
        """
        # --- [新增] 定义重试参数 ---
        max_retries = 3
        initial_delay = 0.5  # 初始延迟0.5秒

        # --- [新增] 重试循环 ---
        for attempt in range(max_retries + 1):
            async with self.lock:
                # 步骤 1: 检查内存缓存，如果存在则直接返回 (最快路径)
                if conversation_entity_uid in self.sessions:
                    return self.sessions[conversation_entity_uid]

                # 步骤 2: 尝试从数据库获取实体
                logger.debug(f"[SessionManager] 尝试第 {attempt + 1}/{max_retries + 1} 次获取实体: {conversation_entity_uid}")
                conv_entity_doc = await self.entity_graph_service.get_entity_by_key(
                    conversation_entity_uid
                )

                # 步骤 3: 【核心判断】如果成功获取到实体，则继续创建 Session
                if conv_entity_doc and isinstance(conv_entity_doc.details, ConversationDetails):
                    logger.info(f"[SessionManager] 成功获取实体，为 '{conversation_entity_uid}' 创建新的会话实例。")
                    
                    # --- [原有的创建逻辑] ---
                    conv_details = conv_entity_doc.details
                    bot_id_for_session = self.self_bot_ids_map.get(conv_details.platform)
                    if not bot_id_for_session:
                        logger.error(f"无法为平台 '{conv_details.platform}' 创建会话，ID地图中找不到对应ID。")
                        return None

                    conversation_info_obj = EnrichedConversationInfo(
                        conversation_id=conv_details.conversation_id,
                        platform=conv_details.platform,
                        bot_id=bot_id_for_session,
                        type=conv_details.type,
                        name=conv_details.name,
                        # ... 其他字段
                    )

                    if not self.core_logic:
                        raise RuntimeError("CoreLogic未注入，QQChatSessionManager无法创建会话。")

                    initial_last_processed_timestamp = (
                        getattr(conv_entity_doc, "last_read_timestamp", 0.0) or time.time() * 1000.0
                    )

                    new_session = QQChatSession(
                        conversation_info=conversation_info_obj,
                        conversation_id=conversation_entity_uid,
                        # ... 其他参数
                        llm_client=self.llm_client,
                        event_storage=self.event_storage,
                        action_handler=self.action_handler,
                        bot_id=bot_id_for_session,
                        core_logic=self.core_logic,
                        chat_session_manager=self,
                        intelligent_interrupter=self.intelligent_interrupter,
                        thought_storage_service=self.thought_storage_service,
                        entity_graph_service=self.entity_graph_service,
                        initial_last_processed_timestamp=initial_last_processed_timestamp,
                    )
                    self.sessions[conversation_entity_uid] = new_session
                    return new_session
                
                # 步骤 4: 如果实体未找到，并且还不是最后一次尝试，则准备重试
                if attempt < max_retries:
                    delay = initial_delay * (2 ** attempt)  # 指数退避策略
                    logger.warning(
                        f"[SessionManager] 未找到实体 '{conversation_entity_uid}' (尝试次数 {attempt + 1})。"
                        f"将在 {delay:.2f} 秒后重试..."
                    )
                    # 【关键】跳出 lock 范围，然后异步等待
                else:
                    # 如果所有尝试都失败了，记录最终错误并返回 None
                    logger.error(
                        f"严重错误：在尝试 {max_retries + 1} 次后，仍然找不到ID为"
                        f"'{conversation_entity_uid}'的会话实体或类型不匹配！"
                    )
                    return None
            
            # 【关键】在 lock 外执行异步等待，避免长时间持有锁
            if attempt < max_retries:
                 await asyncio.sleep(delay)

        return None # 循环结束后如果还没返回，则最终失败

    async def deactivate_session(self, conversation_entity_uid: str) -> None:
        """当聊天窗口关闭时，执行清理工作，例如将会话的最后已读时间戳持久化."""
        async with self.lock:
            if session := self.sessions.pop(conversation_entity_uid, None):
                logger.info(
                    f"[SessionManager] 会话 '{conversation_entity_uid}' "
                    f"对应的窗口已关闭，停用会话实例。"
                )
                final_timestamp = session.last_processed_timestamp
                await self.entity_graph_service.update_conversation_last_read_timestamp(
                    conversation_entity_uid, final_timestamp
                )
                logger.info(
                    f"[SessionManager] 会话 '{conversation_entity_uid}' 的最后已读时间戳已更新。"
                )

    def shutdown(self) -> None:
        """在应用程序关闭时执行清理操作."""
        logger.info("QQChatSessionManager 正在关闭...")
        # 未来可以在这里添加需要保存到数据库的批量操作
        logger.info("QQChatSessionManager 关闭完成。")
