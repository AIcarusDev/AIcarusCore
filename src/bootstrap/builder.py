# src/bootstrap/builder.py
import json
import os
from asyncio import Event as AsyncioEvent
from threading import Event as ThreadingEvent
from typing import Protocol, runtime_checkable

from src import platform_builders
from src.action.action_handler import ActionHandler
from src.action.services.sticker_service import StickerService
from src.aicos.application_manager import ApplicationManager
from src.aicos.state_generator import AICOSStateGenerator
from src.aicos.window_manager import WindowManager
from src.bootstrap.container import ServiceContainer
from src.common.custom_logging.logging_config import get_logger
from src.common.intelligent_interrupt_system.iis_main import IISBuilder
from src.common.intelligent_interrupt_system.intelligent_interrupter import IntelligentInterrupter
from src.common.intelligent_interrupt_system.models import SemanticModel
from src.common.interruption_broker import InterruptionEventBroker
from src.common.narrative_vectorizer.narrative_vectorizer import NarrativeVectorizer
from src.common.unread_info_service.unread_info_service import UnreadInfoService
from src.config import config
from src.config.aicarus_configs import ModelParams
from src.core_communication.action_sender import ActionSender
from src.core_communication.core_ws_server import CoreWebsocketServer
from src.core_communication.event_receiver import EventReceiver
from src.core_logic.consciousness_flow import CoreLogic
from src.core_logic.internal_info_builder import InternalInfoBuilder
from src.core_logic.intrusive_thoughts import IntrusiveThoughtsGenerator
from src.core_logic.state_manager import AIStateManager
from src.core_logic.thought_generator import ThoughtGenerator
from src.core_logic.thought_persistor import ThoughtPersistor
from src.database.core.connection_manager import TypeDBConnectionManager
from src.database.services.action_log_storage_service import ActionLogStorageService
from src.database.services.entity_graph_service import EntityGraphService
from src.database.services.event_storage_service import EventStorageService
from src.database.services.goal_storage_service import GoalStorageService
from src.database.services.image_analysis_cache_service import ImageAnalysisCacheService
from src.database.services.sticker_storage_service import StickerStorageService
from src.database.services.thought_storage_service import ThoughtStorageService
from src.llmrequest.llm_processor import Client as ProcessorClient
from src.message_processing.default_message_processor import DefaultMessageProcessor
from src.message_processing.image_analysis_service import ImageAnalysisService
from src.platform_builders.registry import platform_builder_registry
from src.prompt_builder.orchestrator import ThoughtPromptBuilder

logger = get_logger(__name__)


@runtime_checkable
class Initializable(Protocol):
    """一个协议，定义了初始化基础设施的方法."""

    async def initialize_infrastructure(self) -> None:
        """初始化基础设施的方法."""
        ...


class ServiceBuilder:
    """服务构建器，用于创建和配置核心服务容器."""

    async def build_container(self) -> ServiceContainer:
        """构建并返回一个服务容器，包含所有核心服务和组件."""
        platform_builder_registry.discover_and_register_builders(platform_builders)
        llm_clients = self._initialize_llm_clients()

        db_services = await self._initialize_typedb_and_services()

        # --- [第 1 步: 创建 AIC-OS 核心服务实例] ---
        window_manager = WindowManager()
        application_manager = ApplicationManager()

        sticker_service = StickerService(
            sticker_storage_service=db_services["sticker_storage_service"],
            event_storage_service=db_services["event_storage_service"],
        )

        image_analysis_service = ImageAnalysisService(
            db_services["conn_manager"],  # ImageAnalysisService 仍然可以使用新的conn_manager
            db_services["image_analysis_cache_service"],
        )
        interrupt_model = await self._initialize_interrupt_model(
            db_services["event_storage_service"]
        )

        action_handler = ActionHandler()
        state_manager = AIStateManager(
            thought_service=db_services["thought_storage_service"],
            action_log_service=db_services["action_log_service"],
            goal_storage_service=db_services["goal_storage_service"],
        )

        # --- [第 2 步: 将 AIC-OS 服务注入到新的依赖者中] ---

        # 2a. 创建 AICOSStateGenerator 并注入其依赖
        aicos_state_generator = AICOSStateGenerator(
            window_manager=window_manager,
            application_manager=application_manager,
            entity_service=db_services["entity_graph_service"],
        )

        unread_info_service = UnreadInfoService(
            db_services["event_storage_service"], db_services["entity_graph_service"]
        )

        internal_info_builder = InternalInfoBuilder(db_services["thought_storage_service"])

        # 2b. 创建 ThoughtPromptBuilder 并注入其依赖
        prompt_builder = ThoughtPromptBuilder(
            # 新增依赖
            aicos_state_generator=aicos_state_generator,
            window_manager=window_manager,
            application_manager=application_manager,
            # 旧依赖
            internal_info_builder=internal_info_builder,
            state_manager=state_manager,
            thought_storage_service=db_services["thought_storage_service"],
            entity_graph_service=db_services["entity_graph_service"],
            chat_session_manager=None, # 动态注入
            core_ws_server=None, # 动态注入
        )

        internal_info_builder.prompt_builder = prompt_builder
        # summary_llm = (
        #     llm_clients["summary_llm_client"] or llm_clients["main_consciousness_llm_client"]
        # )
        semantic_model = await self._get_semantic_model(db_services["event_storage_service"])

        narrative_vectorizer = NarrativeVectorizer(
            entity_service=db_services["entity_graph_service"],
            image_analysis_service=image_analysis_service,
            semantic_model=semantic_model,
        )

        interruption_broker = InterruptionEventBroker()
        await interruption_broker.start()

        message_processor = DefaultMessageProcessor(
            event_service=db_services["event_storage_service"],
            entity_service=db_services["entity_graph_service"],
            action_log_service=db_services["action_log_service"],
            image_analysis_service=image_analysis_service,
            semantic_model=semantic_model,
            interruption_broker=interruption_broker,
            narrative_vectorizer=narrative_vectorizer,
            qq_chat_session_manager=None,
        )

        action_sender = ActionSender()
        action_handler.web_search_agent_client = llm_clients["web_search_agent_client"]
        action_handler.url_context_agent_client = llm_clients["url_context_agent_client"]
        event_receiver = EventReceiver(
            event_handler_callback=message_processor.process_event,
            action_handler_instance=action_handler,
            adapter_clients_info=action_sender.adapter_clients_info,
        )
        core_comm_layer = CoreWebsocketServer(
            host=config.server.host,
            port=config.server.port,
            event_receiver=event_receiver,
            action_sender=action_sender,
            event_storage_service=db_services["event_storage_service"],
            action_handler_instance=action_handler,
            entity_service=db_services["entity_graph_service"],
            unread_info_service=unread_info_service,
        )
        prompt_builder.core_ws_server = core_comm_layer

        stop_event = ThreadingEvent()
        intrusive_generator = None
        if (
            config.intrusive_thoughts_module_settings.enabled
            and llm_clients["intrusive_thoughts_llm_client"]
        ):
            intrusive_generator = IntrusiveThoughtsGenerator(
                llm_clients["intrusive_thoughts_llm_client"], stop_event
            )

        thought_generator = ThoughtGenerator(llm_clients["main_consciousness_llm_client"])
        thought_persistor = ThoughtPersistor(db_services["thought_storage_service"])
        immediate_thought_trigger = AsyncioEvent()

        core_logic = CoreLogic(
            window_manager=window_manager,
            application_manager=application_manager,
            core_comm_layer=core_comm_layer,
            action_handler_instance=action_handler,
            state_manager=state_manager,
            chat_session_manager=None,
            thought_storage_service=db_services["thought_storage_service"],
            entity_graph_service=db_services["entity_graph_service"],
            thought_generator=thought_generator,
            thought_persistor=thought_persistor,
            prompt_builder=prompt_builder,
            stop_event=stop_event,
            immediate_thought_trigger=immediate_thought_trigger,
            intrusive_generator_instance=intrusive_generator,
            interruption_broker=interruption_broker,
        )

        return ServiceContainer(
            window_manager=window_manager,
            application_manager=application_manager,
            aicos_state_generator=aicos_state_generator,
            schema_builder=prompt_builder.schema_builder,
            main_consciousness_llm_client=llm_clients["main_consciousness_llm_client"],
            summary_llm_client=llm_clients["summary_llm_client"],
            intrusive_thoughts_llm_client=llm_clients["intrusive_thoughts_llm_client"],
            focused_chat_llm_client=llm_clients["focused_chat_llm_client"],
            web_search_agent_client=llm_clients["web_search_agent_client"],
            url_context_agent_client=llm_clients["url_context_agent_client"],
            deliberation_llm_client=llm_clients["deliberation_llm_client"],
            conn_manager=db_services["conn_manager"],
            event_storage_service=db_services["event_storage_service"],
            thought_storage_service=db_services["thought_storage_service"],
            action_log_service=db_services["action_log_service"],
            image_analysis_service=image_analysis_service,
            entity_graph_service=db_services["entity_graph_service"],
            action_handler=action_handler,
            intelligent_interrupter=interrupt_model,
            internal_info_builder=internal_info_builder,
            intrusive_generator=intrusive_generator,
            message_processor=message_processor,
            prompt_builder=prompt_builder,
            state_manager=state_manager,
            interruption_broker=interruption_broker,
            thought_generator=thought_generator,
            thought_persistor=thought_persistor,
            unread_info_service=unread_info_service,
            core_comm_layer=core_comm_layer,
            core_logic=core_logic,
            sticker_storage_service=db_services["sticker_storage_service"],
            sticker_service=sticker_service,
            narrative_vectorizer=narrative_vectorizer,
            chat_session_manager=None,
            config=config,
            goal_storage_service=db_services["goal_storage_service"],
        )

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
            "summary_llm_client": _create_client(models.information_summary, "information_summary"),
            "web_search_agent_client": _create_client(models.web_search_agent, "web_search_agent"),
            "url_context_agent_client": _create_client(
                models.url_context_agent, "url_context_agent"
            ),
            "deliberation_llm_client": _create_client(models.deliberation, "deliberation"),
            "intrusive_thoughts_llm_client": _create_client(
                models.intrusive_thoughts, "intrusive_thoughts"
            )
            if config.intrusive_thoughts_module_settings.enabled
            else None,
            "focused_chat_llm_client": _create_client(models.focused_chat, "focused_chat")
            if config.focus_chat_mode.enabled
            else None,
        }
        if not clients["main_consciousness_llm_client"]:
            raise RuntimeError("主意识LLM客户端初始化失败。")
        if config.focus_chat_mode.enabled and not clients["focused_chat_llm_client"]:
            raise RuntimeError("专注聊天LLM客户端已启用但初始化失败。")
        logger.info("LLM客户端初始化完毕。")
        return clients

    async def _initialize_typedb_and_services(self) -> dict:
        """[心脏移植完成] 初始化 TypeDB 连接和所有核心数据服务."""
        # 从环境变量中读取数据库配置
        db_config_dict = {
            "host": os.getenv("TYPEDB_HOST", "localhost:1729"),
            "database_name": os.getenv("TYPEDB_DATABASE", "aicarus_core_db"),
            "username": os.getenv("TYPEDB_USER", "admin"),
            "password": os.getenv("TYPEDB_PASSWORD", "password"),
        }
        # 现在从 src/database/core/connection_manager.py 导入
        conn_manager = await TypeDBConnectionManager.get_instance(db_config_dict)

        if not conn_manager or not conn_manager.get_driver():
            raise RuntimeError("TypeDB 连接管理器初始化失败。")

        # 1. 先创建没有额外依赖或作为别人依赖的服务
        event_storage_service = EventStorageService(conn_manager=conn_manager)

        # 2. 创建依赖于其他服务的服务，并手动注入
        entity_graph_service = EntityGraphService(
            conn_manager=conn_manager,
            event_storage_service=event_storage_service,  # <-- 在这里注入！
        )
        event_storage_service.set_entity_graph_service(entity_graph_service)

        # 3. 创建剩余的服务
        services_to_create = {
            "thought_storage_service": ThoughtStorageService,
            "action_log_service": ActionLogStorageService,
            "image_analysis_cache_service": ImageAnalysisCacheService,
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

    async def _initialize_interrupt_model(
        self, event_storage_service: EventStorageService
    ) -> IntelligentInterrupter:
        """初始化中断判断模型."""
        logger.info("=== 开始初始化中断判断模型（小色猫）... ===")
        iis_builder = IISBuilder(event_storage=event_storage_service)
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

    async def _get_semantic_model(
        self, event_storage_service: EventStorageService
    ) -> SemanticModel:
        """获取基础语义模型."""
        iis_builder = IISBuilder(event_storage=event_storage_service)
        await iis_builder.get_or_create_model()
        return iis_builder.base_semantic_model
