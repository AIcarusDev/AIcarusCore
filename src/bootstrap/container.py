# src/bootstrap/container.py
from __future__ import annotations
from dataclasses import dataclass
from typing import TYPE_CHECKING

# 避免在类型提示时出现循环导入问题
if TYPE_CHECKING:
    from src.action.action_handler import ActionHandler
    from src.common.intelligent_interrupt_system.intelligent_interrupter import IntelligentInterrupter
    from src.common.summarization_observation.summarization_service import SummarizationService
    from src.common.unread_info_service.unread_info_service import UnreadInfoService
    from src.core_communication.core_ws_server import CoreWebsocketServer
    from src.core_logic.consciousness_flow import CoreLogic
    from src.core_logic.internal_info_builder import InternalInfoBuilder
    from src.core_logic.intrusive_thoughts import IntrusiveThoughtsGenerator
    from src.core_logic.prompt_builder import ThoughtPromptBuilder
    from src.core_logic.state_manager import AIStateManager
    from src.core_logic.thought_generator import ThoughtGenerator
    from src.core_logic.thought_persistor import ThoughtPersistor
    from src.database.core.connection_manager import ArangoDBConnectionManager
    from src.database.services.action_log_storage_service import ActionLogStorageService
    from src.database.services.conversation_storage_service import ConversationStorageService
    from src.database.services.event_storage_service import EventStorageService
    from src.database.services.person_storage_service import PersonStorageService
    from src.database.services.summary_storage_service import SummaryStorageService
    from src.focus_chat_mode.chat_session_manager import ChatSessionManager
    from src.llmrequest.llm_processor import Client as ProcessorClient
    from src.message_processing.default_message_processor import DefaultMessageProcessor
    from src.database import (
    ActionLogStorageService,
    ArangoDBConnectionManager,
    ConversationStorageService,
    PersonStorageService,
    ThoughtStorageService,
    )

@dataclass
class ServiceContainer:
    """一个存放所有核心服务实例的容器."""
    # LLM 客户端
    main_consciousness_llm_client: ProcessorClient
    summary_llm_client: ProcessorClient
    intrusive_thoughts_llm_client: ProcessorClient | None
    focused_chat_llm_client: ProcessorClient | None
    web_search_agent_client: ProcessorClient | None

    # 数据库与核心服务
    conn_manager: ArangoDBConnectionManager
    event_storage_service: EventStorageService
    conversation_storage_service: ConversationStorageService
    thought_storage_service: ThoughtStorageService
    action_log_service: ActionLogStorageService
    summary_storage_service: SummaryStorageService
    person_storage_service: PersonStorageService

    # 业务逻辑与功能模块
    action_handler: ActionHandler
    intelligent_interrupter: IntelligentInterrupter
    internal_info_builder: InternalInfoBuilder
    intrusive_generator: IntrusiveThoughtsGenerator | None
    message_processor: DefaultMessageProcessor
    prompt_builder: ThoughtPromptBuilder
    state_manager: AIStateManager
    summarization_service: SummarizationService
    thought_generator: ThoughtGenerator
    thought_persistor: ThoughtPersistor
    unread_info_service: UnreadInfoService

    # 通信与核心循环
    core_comm_layer: CoreWebsocketServer
    core_logic: CoreLogic

    # 专注聊天管理器 (特殊处理，因为它依赖安检)
    chat_session_manager: ChatSessionManager | None