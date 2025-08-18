# src/bootstrap/container.py
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

# 避免在类型提示时出现循环导入问题
if TYPE_CHECKING:
    from src.action.action_handler import ActionHandler
    from src.action.services.sticker_service import StickerService
    from src.common.intelligent_interrupt_system.intelligent_interrupter import (
        IntelligentInterrupter,
    )
    from src.common.interruption_broker import InterruptionEventBroker
    from src.common.narrative_vectorizer.narrative_vectorizer import NarrativeVectorizer
    from src.common.unread_info_service.unread_info_service import UnreadInfoService
    from src.core_communication.core_ws_server import CoreWebsocketServer
    from src.core_logic.consciousness_flow import CoreLogic
    from src.core_logic.internal_info_builder import InternalInfoBuilder
    from src.core_logic.intrusive_thoughts import IntrusiveThoughtsGenerator
    from src.core_logic.prompt_builder import ThoughtPromptBuilder
    from src.core_logic.state_manager import AIStateManager
    from src.core_logic.thought_generator import ThoughtGenerator
    from src.core_logic.thought_persistor import ThoughtPersistor
    from src.database.core.connection_manager import TypeDBConnectionManager
    from src.database.services import (
        ActionLogStorageService,
        EntityGraphService,
        EventStorageService,
        GoalStorageService,
        StickerStorageService,
        ThoughtStorageService,
    )
    from src.focus_chat_mode.chat_session_manager import ChatSessionManager
    from src.llmrequest.llm_processor import Client as ProcessorClient
    from src.message_processing.default_message_processor import DefaultMessageProcessor
    from src.message_processing.image_analysis_service import ImageAnalysisService


@dataclass
class ServiceContainer:
    """一个存放所有核心服务实例的容器，定义了系统的基本组件结构."""

    # LLM 客户端
    main_consciousness_llm_client: ProcessorClient
    summary_llm_client: ProcessorClient | None
    intrusive_thoughts_llm_client: ProcessorClient | None
    focused_chat_llm_client: ProcessorClient | None
    web_search_agent_client: ProcessorClient | None
    url_context_agent_client: ProcessorClient | None
    deliberation_llm_client: ProcessorClient | None

    # 连接管理器
    conn_manager: TypeDBConnectionManager

    # 核心数据存储服务
    event_storage_service: EventStorageService
    thought_storage_service: ThoughtStorageService
    action_log_service: ActionLogStorageService
    entity_graph_service: EntityGraphService
    image_analysis_service: ImageAnalysisService
    sticker_storage_service: StickerStorageService
    goal_storage_service: GoalStorageService

    # 业务逻辑与功能模块
    action_handler: ActionHandler
    sticker_service: StickerService
    intelligent_interrupter: IntelligentInterrupter
    internal_info_builder: InternalInfoBuilder
    intrusive_generator: IntrusiveThoughtsGenerator | None
    message_processor: DefaultMessageProcessor
    prompt_builder: ThoughtPromptBuilder
    state_manager: AIStateManager
    thought_generator: ThoughtGenerator
    thought_persistor: ThoughtPersistor
    unread_info_service: UnreadInfoService
    interruption_broker: InterruptionEventBroker
    narrative_vectorizer: NarrativeVectorizer

    # 通信与核心循环
    core_comm_layer: CoreWebsocketServer
    core_logic: CoreLogic

    # 专注聊天管理器 (特殊处理，因为它依赖安检)
    chat_session_manager: ChatSessionManager | None
