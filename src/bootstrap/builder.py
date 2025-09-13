# 文件路径: src/bootstrap/builder.py

import json
import os
from typing import Protocol, runtime_checkable

from src.bootstrap.container import ServiceContainer
from src.common.custom_logging.logging_config import get_logger
from src.common.intelligent_interrupt_system.iis_main import IISBuilder
from src.common.intelligent_interrupt_system.intelligent_interrupter import IntelligentInterrupter
from src.common.intelligent_interrupt_system.models import (
    AsyncSemanticModelProxy,
)
from src.common.interruption_broker import InterruptionEventBroker
from src.common.narrative_vectorizer.narrative_vectorizer import NarrativeVectorizer
from src.config import config
from src.config.aicarus_configs import ModelParams
from src.mind.abilities.information_retrieval_service import InformationRetrievalService
from src.mind.consciousness_flow import CoreLogic
from src.mind.goal_manager import GoalManager
from src.mind.state_manager import AIStateManager
from src.mind.thought_generator import ThoughtGenerator
from src.mind.thought_persistor import ThoughtPersistor
from src.os import apps
from src.os.application_manager import ApplicationManager
from src.os.apps.qq.sticker_service import QQStickerService
from src.os.communication.action_sender import ActionSender
from src.os.communication.core_ws_server import CoreWebsocketServer
from src.os.communication.event_receiver import EventReceiver
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

logger = get_logger(__name__)


@runtime_checkable
class Initializable(Protocol):
    """定义了一个可初始化的协议，要求实现类提供初始化基础设施的方法."""

    async def initialize_infrastructure(self) -> None:
        """Initialize the required infrastructure for the implementing service."""
        ...


class ServiceBuilder:
    """服务构建器，用于创建和配置核心服务容器."""

    def __init__(self, interruption_message: str = "") -> None:
        self.interruption_message = interruption_message

    async def build_container(self) -> ServiceContainer:
        """构建并配置服务容器，包括初始化LLM客户端、数据库服务等."""
        # 初始化应用管理器并加载所有应用
        application_manager = ApplicationManager()
        application_manager.discover_and_load_apps(apps)
        # 初始化 LLM 客户端
        llm_clients = self._initialize_llm_clients()
        db_services = await self._initialize_typedb_and_services()

        # 1. 创建唯一的 SemanticModel 代理实例
        # 这个操作是瞬间完成的，真正的模型加载在后台进行
        semantic_model_proxy = AsyncSemanticModelProxy()
        logger.info("SemanticModelProxy已创建，后台加载任务已启动。")

        # 创建新的能力/服务实例
        filesystem_service = FileSystemService()
        info_retrieval_service = InformationRetrievalService(
            web_search_agent_client=llm_clients["web_search_agent_client"],
            url_context_agent_client=llm_clients["url_context_agent_client"],
        )
        goal_manager = GoalManager(db_services["goal_storage_service"])

        # OS 层服务
        window_manager = WindowManager()

        # 基础设施服务
        action_sender = ActionSender()
        # 实例化 QQStickerService
        qq_sticker_service = QQStickerService(
            sticker_storage_service=db_services["sticker_storage_service"],
            event_storage_service=db_services["event_storage_service"],
        )
        image_analysis_service = ImageAnalysisService(
            db_services["conn_manager"],
            db_services["media_cache_service"],
            config.feature_flags,
        )

        # ActionHandler 的初始化
        action_handler = ActionHandler(
            filesystem_service=filesystem_service,
            info_retrieval_service=info_retrieval_service,
            thought_storage_service=db_services["thought_storage_service"],
            event_storage_service=db_services["event_storage_service"],
            action_log_service=db_services["action_log_service"],
            action_sender=action_sender,
            entity_service=db_services["entity_graph_service"],
        )
        action_handler.set_application_manager(application_manager)

        # Mind 层服务
        state_manager = AIStateManager(
            thought_service=db_services["thought_storage_service"],
            action_log_service=db_services["action_log_service"],
            goal_manager=goal_manager,
        )

        # Prompting 层服务
        aicos_state_generator = AICOSStateGenerator(
            window_manager=window_manager,
            application_manager=application_manager,
            entity_service=db_services["entity_graph_service"],
            event_service=db_services["event_storage_service"],
            media_cache_service=db_services["media_cache_service"],
            action_handler=action_handler,
        )

        action_handler.set_state_generator(aicos_state_generator)

        prompt_builder = ThoughtPromptBuilder(
            aicos_state_generator=aicos_state_generator,
            window_manager=window_manager,
            application_manager=application_manager,
            state_manager=state_manager,
            thought_storage_service=db_services["thought_storage_service"],
            entity_graph_service=db_services["entity_graph_service"],
            filesystem_service=filesystem_service,
            info_retrieval_service=info_retrieval_service,
            goal_manager=goal_manager,
            qq_sticker_service=qq_sticker_service,
        )

        # 2. 将代理注入到 NarrativeVectorizer
        narrative_vectorizer = NarrativeVectorizer(
            entity_service=db_services["entity_graph_service"],
            image_analysis_service=image_analysis_service,
            # 传入代理，而不是 await 真实模型
            semantic_model=semantic_model_proxy,
        )
        interruption_broker = InterruptionEventBroker()
        await interruption_broker.start()

        # 3. 将代理注入到 DefaultMessageProcessor
        message_processor = DefaultMessageProcessor(
            event_service=db_services["event_storage_service"],
            entity_service=db_services["entity_graph_service"],
            action_log_service=db_services["action_log_service"],
            image_analysis_service=image_analysis_service,
            semantic_model=semantic_model_proxy,
            media_cache_service=db_services["media_cache_service"],
            interruption_broker=interruption_broker,
            narrative_vectorizer=narrative_vectorizer,
            window_manager=window_manager,
            action_handler=action_handler,
        )

        thought_generator = ThoughtGenerator(
            llm_client=llm_clients["main_consciousness_llm_client"],
            action_handler=action_handler,
            media_cache_service=db_services["media_cache_service"],
        )

        container = ServiceContainer(
            main_consciousness_llm_client=llm_clients["main_consciousness_llm_client"],
            web_search_agent_client=llm_clients["web_search_agent_client"],
            url_context_agent_client=llm_clients["url_context_agent_client"],
            thought_generator=thought_generator,
            config=config,
            conn_manager=db_services["conn_manager"],
            event_storage_service=db_services["event_storage_service"],
            thought_storage_service=db_services["thought_storage_service"],
            action_log_service=db_services["action_log_service"],
            entity_graph_service=db_services["entity_graph_service"],
            image_analysis_service=image_analysis_service,
            sticker_storage_service=db_services["sticker_storage_service"],
            goal_storage_service=db_services["goal_storage_service"],
            media_cache_service=db_services["media_cache_service"],
            action_handler=action_handler,
            qq_sticker_service=qq_sticker_service,
            # 4. 将代理注入到中断模型
            intelligent_interrupter=await self._initialize_interrupt_model(
                db_services["event_storage_service"], semantic_model_proxy
            ),
            message_processor=message_processor,
            prompt_builder=prompt_builder,
            state_manager=state_manager,
            thought_persistor=ThoughtPersistor(db_services["thought_storage_service"]),
            interruption_broker=interruption_broker,
            narrative_vectorizer=narrative_vectorizer,
            core_comm_layer=None,  # 稍后填充
            core_logic=None,  # 稍后填充
            window_manager=window_manager,
            application_manager=application_manager,
            aicos_state_generator=aicos_state_generator,
            filesystem_service=filesystem_service,
            info_retrieval_service=info_retrieval_service,
            goal_manager=goal_manager,
        )

        # 接收完整的容器实例
        event_receiver = EventReceiver(
            mind_event_callback=message_processor.process_event,
            action_handler_instance=action_handler,
            service_container=container,
        )

        core_comm_layer = CoreWebsocketServer(
            container=container,
            host=config.server.host,
            port=config.server.port,
            event_receiver=event_receiver,
            action_sender=action_sender,
            event_storage_service=db_services["event_storage_service"],
            action_handler_instance=action_handler,
            entity_service=db_services["entity_graph_service"],
        )

        core_logic = CoreLogic(
            thought_generator=container.thought_generator,
            thought_persistor=container.thought_persistor,
            prompt_builder=prompt_builder,
        )

        # 填充容器中之前留空的服务
        container.core_comm_layer = core_comm_layer
        container.core_logic = core_logic

        return container

    def _initialize_llm_clients(self) -> dict:
        """初始化所有配置的LLM客户端."""
        logger.info("开始初始化LLM客户端...")
        general_llm_settings_obj = config.llm_client_settings
        resolved_abandoned_keys: list[str] | None = None
        if env_val_abandoned := os.getenv("LLM_ABANDONED_KEYS"):
            try:
                keys_from_env = json.loads(env_val_abandoned)
                if isinstance(keys_from_env, list):
                    resolved_abandoned_keys = [
                        str(k).strip() for k in keys_from_env if str(k).strip()
                    ]
            except json.JSONDecodeError:
                resolved_abandoned_keys = [
                    k.strip() for k in env_val_abandoned.split(",") if k.strip()
                ]

        def _create_client(cfg: ModelParams, purpose: str) -> ProcessorClient | None:
            if not cfg or not cfg.provider or not cfg.model_name:
                return None
            try:
                # 1. 明确分离出仅供 UnderlyingLLMClient 内部使用的参数。
                #    这些参数不应该被当作 API 的 generationConfig 发送出去。
                internal_client_params = {
                    "image_placeholder_tag",
                    "stream_chunk_delay_seconds",
                    "enable_image_compression",
                    "image_compression_target_bytes",
                    "rate_limit_disable_duration_seconds",
                }

                # 2. 从通用设置中筛选出合法的生成参数 (GenerationParams)。
                #    这样可以确保只有 API 认识的字段才会进入 **kwargs。
                valid_generation_params = {
                    k: v
                    for k, v in vars(general_llm_settings_obj).items()
                    if k not in internal_client_params
                }

                # 3. 构建构造函数参数字典，现在它更干净、更安全了。
                args = {
                    "model": {"provider": cfg.provider.upper(), "name": cfg.model_name},
                    # 仅传递合法的生成参数
                    **valid_generation_params,
                    # 传递模型专属的参数
                    **{
                        k: v
                        for k, v in vars(cfg).items()
                        if v is not None and k not in ["provider", "model_name"]
                    },
                    # 显式传递那些内部使用的参数，而不是通过 **kwargs
                    "stream_chunk_delay_seconds": general_llm_settings_obj.stream_chunk_delay_seconds,  # noqa: E501
                    "enable_image_compression": general_llm_settings_obj.enable_image_compression,
                    "image_compression_target_bytes": general_llm_settings_obj.image_compression_target_bytes,  # noqa: E501
                    "rate_limit_disable_duration_seconds": general_llm_settings_obj.rate_limit_disable_duration_seconds,  # noqa: E501
                }

                if resolved_abandoned_keys:
                    args["abandoned_keys_config"] = resolved_abandoned_keys

                # 移除值为 None 的项，防止覆盖 ProcessorClient 中的默认值
                final_args = {k: v for k, v in args.items() if v is not None}
                # 创建 ProcessorClient 实例
                client = ProcessorClient(**final_args)
                logger.info(
                    f"为用途 '{purpose}' 创建 ProcessorClient 成功 "
                    f"(模型: {client.llm_client.model_name})。"
                )
                return client
            except Exception as e:
                logger.error(f"为用途 '{purpose}' 创建LLM客户端失败: {e}", exc_info=True)
                return None

        if not (models := config.llm_models):
            raise RuntimeError("[llm_models] 配置块缺失。")
        clients = {
            "main_consciousness_llm_client": _create_client(
                models.main_consciousness, "main_consciousness"
            ),
            "web_search_agent_client": _create_client(models.web_search_agent, "web_search_agent"),
            "url_context_agent_client": _create_client(
                models.url_context_agent, "url_context_agent"
            ),
        }
        if not clients["main_consciousness_llm_client"]:
            raise RuntimeError("主意识LLM客户端初始化失败。")
        logger.info("LLM客户端初始化完毕。")
        return clients

    async def _initialize_typedb_and_services(self) -> dict:
        """初始化 TypeDB 连接和所有核心数据服务."""
        # 从环境变量中读取数据库配置
        db_config_dict = {
            "host": os.getenv("TYPEDB_HOST", "localhost:1729"),
            "database_name": os.getenv("TYPEDB_DATABASE", "aicarus_core_db"),
            "username": os.getenv("TYPEDB_USER", "admin"),
            "password": os.getenv("TYPEDB_PASSWORD", "password"),
        }
        conn_manager = await TypeDBConnectionManager.get_instance(db_config_dict)

        if not conn_manager or not conn_manager.get_driver():
            raise RuntimeError("TypeDB 连接管理器初始化失败。")

        # 1. 先创建没有额外依赖或作为别人依赖的服务
        event_storage_service = EventStorageService(conn_manager=conn_manager)

        # 2. 创建依赖于其他服务的服务，并手动注入
        entity_graph_service = EntityGraphService(
            conn_manager=conn_manager,
            event_storage_service=event_storage_service,
        )
        event_storage_service.set_entity_graph_service(entity_graph_service)

        # 3. 创建剩余的服务
        services_to_create = {
            "thought_storage_service": ThoughtStorageService,
            "action_log_service": ActionLogStorageService,
            "media_cache_service": MediaCacheService,
            "sticker_storage_service": StickerStorageService,
            "goal_storage_service": GoalStorageService,
        }

        initialized_services = {
            "conn_manager": conn_manager,
            "event_storage_service": event_storage_service,
            "entity_graph_service": entity_graph_service,
        }

        for instance_name, service_class in services_to_create.items():
            instance = service_class(conn_manager=conn_manager)
            if isinstance(instance, Initializable) and hasattr(
                instance, "initialize_infrastructure"
            ):
                await instance.initialize_infrastructure()
            initialized_services[instance_name] = instance

        logger.info("所有核心 TypeDB 数据存储服务均已初始化。")
        return initialized_services

    # 5. 修改 _initialize_interrupt_model 接收代理
    async def _initialize_interrupt_model(
        self,
        event_storage_service: EventStorageService,
        semantic_model_proxy: AsyncSemanticModelProxy,
    ) -> IntelligentInterrupter:
        """初始化中断判断模型."""
        logger.info("=== 开始初始化中断判断模型... ===")
        iis_builder = IISBuilder(
            event_storage=event_storage_service, semantic_model_proxy=semantic_model_proxy
        )
        semantic_markov_model = await iis_builder.get_or_create_model()
        interrupt_config = config.interrupt_model
        speaker_weights = {entry.id: entry.weight for entry in interrupt_config.speaker_weights}
        if "default" not in speaker_weights:
            speaker_weights["default"] = 1.0

        interrupt_model = IntelligentInterrupter(
            speaker_weights=speaker_weights,
            objective_keywords=interrupt_config.objective_keywords,
            core_importance_concepts=interrupt_config.core_importance_concepts,
            semantic_markov_model=semantic_markov_model,
        )
        logger.info("=== 中断判断模型已成功初始化！ ===")
        return interrupt_model
