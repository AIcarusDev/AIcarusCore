# 文件路径: src/bootstrap/container.py

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

# 避免在类型提示时出现循环导入问题
if TYPE_CHECKING:
    from src.common.intelligent_interrupt_system.intelligent_interrupter import (
        IntelligentInterrupter,
    )
    from src.common.interruption_broker import InterruptionEventBroker
    from src.common.narrative_vectorizer.narrative_vectorizer import NarrativeVectorizer
    from src.config import AlcarusRootConfig
    from src.mind.abilities.deliberation_service import DeliberationService
    from src.mind.abilities.information_retrieval_service import InformationRetrievalService
    from src.mind.consciousness_flow import CoreLogic
    from src.mind.goal_manager import GoalManager
    from src.mind.state_manager import AIStateManager
    from src.mind.thought_generator import ThoughtGenerator
    from src.mind.thought_persistor import ThoughtPersistor
    from src.os.application_manager import ApplicationManager
    from src.os.apps.qq.sticker_service import QQStickerService
    from src.os.communication.core_ws_server import CoreWebsocketServer
    from src.os.services.filesystem_service import FileSystemService
    from src.os.state_generator import AICOSStateGenerator
    from src.os.window_manager import WindowManager
    from src.prompting.orchestrator import ThoughtPromptBuilder
    from src.services.action.action_handler import ActionHandler
    from src.services.database.core.connection_manager import TypeDBConnectionManager
    from src.services.database.services import (
        ActionLogStorageService,
        EntityGraphService,
        EventStorageService,
        GoalStorageService,
        MediaCacheService,
        StickerStorageService,
        ThoughtStorageService,
    )
    from src.services.llmrequest.llm_processor import Client as ProcessorClient
    from src.services.perception.default_message_processor import DefaultMessageProcessor
    from src.services.perception.image_analysis_service import ImageAnalysisService


@dataclass
class ServiceContainer:
    """一个存放所有核心服务实例的容器，定义了系统的基本组件结构."""

    # LLM 客户端
    main_consciousness_llm_client: ProcessorClient
    web_search_agent_client: ProcessorClient | None
    url_context_agent_client: ProcessorClient | None
    deliberation_llm_client: ProcessorClient | None
    config: AlcarusRootConfig

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
    media_cache_service: MediaCacheService

    # 业务逻辑与功能模块
    action_handler: ActionHandler
    qq_sticker_service: QQStickerService
    intelligent_interrupter: IntelligentInterrupter
    message_processor: DefaultMessageProcessor
    prompt_builder: ThoughtPromptBuilder
    state_manager: AIStateManager
    thought_generator: ThoughtGenerator
    thought_persistor: ThoughtPersistor
    interruption_broker: InterruptionEventBroker
    narrative_vectorizer: NarrativeVectorizer

    # 通信与核心循环
    core_comm_layer: CoreWebsocketServer
    core_logic: CoreLogic

    # [AIC-OS] 服务
    window_manager: WindowManager
    application_manager: ApplicationManager
    aicos_state_generator: AICOSStateGenerator
    window_manager: WindowManager

    # 能力与服务
    filesystem_service: FileSystemService
    info_retrieval_service: InformationRetrievalService
    deliberation_service: DeliberationService
    goal_manager: GoalManager
