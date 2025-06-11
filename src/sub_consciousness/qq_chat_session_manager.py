# -*- coding: utf-8 -*-
import asyncio
import time
from typing import Dict,Optional

from aicarus_protocols.event import Event

# 从项目中导入必要的模块
from src.common.custom_logging.logger_manager import get_logger
from src.config.aicarus_configs import SubConsciousnessSettings
from src.llmrequest.llm_processor import Client as LLMProcessorClient
from src.database.services.event_storage_service import EventStorageService
from src.action.action_handler import ActionHandler
from .qq_chat_session import QQChatSession

logger = get_logger(__name__)

class QQChatSessionManager:
    """
    管理所有 QQChatSession 实例，处理消息分发和会话生命周期。
    """
    def __init__(
        self,
        config: SubConsciousnessSettings,
        llm_client: LLMProcessorClient,
        event_storage: EventStorageService,
        action_handler: ActionHandler,
        bot_id: str
    ):
        self.config = config
        self.llm_client = llm_client
        self.event_storage = event_storage
        self.action_handler = action_handler
        self.bot_id = bot_id
        self.logger = logger # 将模块级别的logger赋值给实例属性

        self.sessions: Dict[str, QQChatSession] = {}
        self.lock = asyncio.Lock()
        # self.active_session_context: Dict[str, Dict[str, str]] = {} # 用于存储会话的platform和type

        self.logger.info("QQChatSessionManager 初始化完成。") # 使用 self.logger

    def _get_conversation_id(self, event: Event) -> str:
        # 从 Event 中提取唯一的会话ID (例如 group_id 或 user_id)
        # 此处需要根据 aicarus_protocols 的具体定义来实现
        info = event.conversation_info
        return info.conversation_id if info else "default_conv"

    async def get_or_create_session(
        self, 
        conversation_id: str, 
        platform: Optional[str] = None, 
        conversation_type: Optional[str] = None
    ) -> QQChatSession:
        async with self.lock:
            if conversation_id not in self.sessions:
                logger.info(f"[SessionManager] 为 '{conversation_id}' 创建新的会话实例。")
                
                # 对于新会话，platform 和 conversation_type 应该是必需的
                # 调用者 (handle_incoming_message) 应该从事件中提取并提供这些信息
                if not platform or not conversation_type:
                    logger.error(f"[SessionManager] 创建新会话 '{conversation_id}' 时缺少 platform 或 conversation_type。")
                    # 可以选择抛出错误，或者使用默认值（但不推荐）
                    # raise ValueError(f"Platform and conversation_type are required to create a new session for {conversation_id}")
                    # 使用默认值作为后备，但理想情况下不应发生
                    platform = platform or "unknown_platform"
                    conversation_type = conversation_type or "unknown_type"

                self.sessions[conversation_id] = QQChatSession(
                    conversation_id=conversation_id,
                    llm_client=self.llm_client,
                    event_storage=self.event_storage,
                    action_handler=self.action_handler,
                    bot_qq_id=self.bot_id,
                    platform=platform,
                    conversation_type=conversation_type
                )
                # self.active_session_context[conversation_id] = {"platform": platform, "type": conversation_type}
            
            # # 确保现有会话实例也有 platform 和 conversation_type (如果之前没有)
            # # 这部分逻辑可能不需要，因为这些属性应该在创建时就设置好
            # elif not hasattr(self.sessions[conversation_id], 'platform') or \
            #      not hasattr(self.sessions[conversation_id], 'conversation_type'):
            #     if platform and conversation_type:
            #         self.sessions[conversation_id].platform = platform
            #         self.sessions[conversation_id].conversation_type = conversation_type
            #         # self.active_session_context[conversation_id] = {"platform": platform, "type": conversation_type}
            #     # else:
            #         # logger.warning(f"[SessionManager] 尝试更新现有会话 '{conversation_id}' 的上下文，但未提供 platform/type。")


            return self.sessions[conversation_id]
    
    def _is_bot_mentioned(self, event: Event) -> bool:
        # 检查消息中是否 @ 了机器人
        if event.event_type != 'message.group.normal':
            return False
        
        if not event.content: # 检查 content 是否为 None 或空列表
            self.logger.debug(f"[_is_bot_mentioned] Event content is empty for event {event.event_id}")
            return False

        for seg in event.content: # 应该直接访问 event.content
            if seg.type == 'at':
                at_user_id = seg.data.get('user_id') # 应该用 'user_id' 而不是 'qq'
                self.logger.debug(f"[_is_bot_mentioned] Found @ segment. at_user_id: {at_user_id} (type: {type(at_user_id)}), self.bot_id: {self.bot_id} (type: {type(self.bot_id)})")
                # 确保比较时双方都是字符串
                if at_user_id is not None and str(at_user_id) == str(self.bot_id):
                    self.logger.info(f"[_is_bot_mentioned] Bot (ID: {self.bot_id}) was mentioned in event {event.event_id}.")
                    return True
        
        self.logger.debug(f"[_is_bot_mentioned] Bot (ID: {self.bot_id}) was NOT mentioned in event {event.event_id}.")
        return False

    async def handle_incoming_message(self, event: Event):
        """
        处理来自消息处理器的消息事件。
        """
        conv_id = self._get_conversation_id(event)
        platform = event.platform
        conv_type = event.conversation_info.type if event.conversation_info else "unknown" # 默认为unknown，但应尽量从事件获取

        session = await self.get_or_create_session(
            conversation_id=conv_id, 
            platform=platform, 
            conversation_type=conv_type
        )

        # 激活逻辑：如果被@，则激活会话
        if self._is_bot_mentioned(event):
            session.activate()

        # 将事件交给会话处理 (会话内部会判断 is_active)
        await session.process_event(event)
        
    async def run_periodic_deactivation_check(self):
        """
        后台任务，定期检查并停用不活跃的会话。
        """
        while True:
            await asyncio.sleep(self.config.deactivation_check_interval_seconds)
            
            async with self.lock:
                inactive_sessions = []
                current_time = time.time()
                for conv_id, session in self.sessions.items():
                    if session.is_active and (current_time - session.last_active_time) > self.config.session_timeout_seconds:
                        inactive_sessions.append(session)
                
                for session in inactive_sessions:
                    session.deactivate()
