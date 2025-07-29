# 文件: src/bootstrap/builder.py (最终修正版 V1.1)
import json
import os
from asyncio import Event as AsyncioEvent
from threading import Event as ThreadingEvent
from typing import Protocol, runtime_checkable

from src import platform_builders
from src.action.action_handler import ActionHandler
from src.bootstrap.container import ServiceContainer
from src.common.custom_logging.logging_config import get_logger
from src.common.intelligent_interrupt_system.iis_main import IISBuilder
from src.common.intelligent_interrupt_system.intelligent_interrupter import IntelligentInterrupter
from src.common.intelligent_interrupt_system.models import SemanticModel
from src.common.summarization_observation.summarization_service import SummarizationService
from src.common.unread_info_service.unread_info_service import UnreadInfoService
from src.config import config
from src.config.aicarus_configs import ModelParams
from src.core_communication.action_sender import ActionSender
from src.core_communication.core_ws_server import CoreWebsocketServer
from src.core_communication.event_receiver import EventReceiver
from src.core_logic.consciousness_flow import CoreLogic
from src.core_logic.internal_info_builder import InternalInfoBuilder
from src.core_logic.intrusive_thoughts import IntrusiveThoughtsGenerator
from src.core_logic.prompt_builder import ThoughtPromptBuilder
from src.core_logic.state_manager import AIStateManager
from src.core_logic.thought_generator import ThoughtGenerator
from src.core_logic.thought_persistor import ThoughtPersistor
from src.database import (
    ActionLogStorageService,
    ArangoDBConnectionManager,
    ConversationStorageService,
    CoreDBCollections,
    EventStorageService,
    PersonStorageService,
    SummaryStorageService,
    ThoughtStorageService,
)
from src.llmrequest.llm_processor import Client as ProcessorClient
from src.message_processing.default_message_processor import DefaultMessageProcessor
from src.platform_builders.registry import platform_builder_registry

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
        db_services = await self._initialize_database_and_services()
        interrupt_model = await self._initialize_interrupt_model(
            db_services["event_storage_service"]
        )

        action_handler = ActionHandler()
        state_manager = AIStateManager(
            db_services["thought_storage_service"], db_services["action_log_service"]
        )
        unread_info_service = UnreadInfoService(
            db_services["event_storage_service"], db_services["conversation_storage_service"]
        )
        internal_info_builder = InternalInfoBuilder(db_services["thought_storage_service"])

        prompt_builder = ThoughtPromptBuilder(
            unread_info_service,
            internal_info_builder,
            db_services["event_storage_service"],
            db_services["thought_storage_service"],  # <-- 新增的 thought_storage_service
            db_services["conversation_storage_service"],
            None,  # chat_session_manager 是可选的，后面注入
            None,  # core_ws_server 也是可选的，后面注入
        )
        # ======================================================================

        internal_info_builder.prompt_builder = prompt_builder
        summary_llm = (
            llm_clients["summary_llm_client"] or llm_clients["main_consciousness_llm_client"]
        )
        summarization_service = SummarizationService(summary_llm)
        semantic_model = await self._get_semantic_model(db_services["event_storage_service"])

        message_processor = DefaultMessageProcessor(
            event_service=db_services["event_storage_service"],
            conversation_service=db_services["conversation_storage_service"],
            person_service=db_services["person_storage_service"],
            action_log_service=db_services["action_log_service"],
            semantic_model=semantic_model,
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
            person_service=db_services["person_storage_service"],
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
            core_comm_layer=core_comm_layer,
            action_handler_instance=action_handler,
            state_manager=state_manager,
            chat_session_manager=None,
            thought_storage_service=db_services["thought_storage_service"],
            thought_generator=thought_generator,
            thought_persistor=thought_persistor,
            prompt_builder=prompt_builder,
            stop_event=stop_event,
            immediate_thought_trigger=immediate_thought_trigger,
            intrusive_generator_instance=intrusive_generator,
        )

        return ServiceContainer(
            main_consciousness_llm_client=llm_clients["main_consciousness_llm_client"],
            summary_llm_client=llm_clients["summary_llm_client"],
            intrusive_thoughts_llm_client=llm_clients["intrusive_thoughts_llm_client"],
            focused_chat_llm_client=llm_clients["focused_chat_llm_client"],
            web_search_agent_client=llm_clients["web_search_agent_client"],
            url_context_agent_client=llm_clients["url_context_agent_client"],
            conn_manager=db_services["conn_manager"],
            event_storage_service=db_services["event_storage_service"],
            conversation_storage_service=db_services["conversation_storage_service"],
            thought_storage_service=db_services["thought_storage_service"],
            action_log_service=db_services["action_log_service"],
            summary_storage_service=db_services["summary_storage_service"],
            person_storage_service=db_services["person_storage_service"],
            action_handler=action_handler,
            intelligent_interrupter=interrupt_model,
            internal_info_builder=internal_info_builder,
            intrusive_generator=intrusive_generator,
            message_processor=message_processor,
            prompt_builder=prompt_builder,
            state_manager=state_manager,
            summarization_service=summarization_service,
            thought_generator=thought_generator,
            thought_persistor=thought_persistor,
            unread_info_service=unread_info_service,
            core_comm_layer=core_comm_layer,
            core_logic=core_logic,
            chat_session_manager=None,
        )

    def _initialize_llm_clients(self) -> dict:
        logger.info("开始初始化LLM客户端...")
        general_llm_settings_obj = config.llm_client_settings
        resolved_abandoned_keys: list[str] | None = None
        env_val_abandoned = os.getenv("LLM_ABANDONED_KEYS")
        if env_val_abandoned:
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
            if not resolved_abandoned_keys and env_val_abandoned.strip():
                resolved_abandoned_keys = [env_val_abandoned.strip()]

        def _create_client(cfg: ModelParams, purpose: str) -> ProcessorClient | None:
            if not cfg or not cfg.provider or not cfg.model_name:
                return None
            try:
                args = {
                    "model": {"provider": cfg.provider.upper(), "name": cfg.model_name},
                    **vars(general_llm_settings_obj),
                    **{
                        k: v
                        for k, v in vars(cfg).items()
                        if v is not None and k not in ["provider", "model_name"]
                    },
                }
                if resolved_abandoned_keys:
                    args["abandoned_keys_config"] = resolved_abandoned_keys
                client = ProcessorClient(**{k: v for k, v in args.items() if v is not None})
                logger.info(
                    f"为用途 '{purpose}' 创建 ProcessorClient 成功 "
                    f"(模型: {client.llm_client.model_name})。"
                )
                return client
            except Exception as e:
                logger.error(f"为用途 '{purpose}' 创建LLM客户端失败: {e}", exc_info=True)
                return None

        if not config.llm_models:
            raise RuntimeError("[llm_models] 配置块缺失。")
        models = config.llm_models
        clients = {
            "main_consciousness_llm_client": _create_client(
                models.main_consciousness, "main_consciousness"
            ),
            "summary_llm_client": _create_client(models.information_summary, "information_summary"),
            "web_search_agent_client": _create_client(models.web_search_agent, "web_search_agent"),
            "url_context_agent_client": _create_client(
                models.url_context_agent,
                "url_context_agent"
            ),
            "intrusive_thoughts_llm_client": None,
            "focused_chat_llm_client": None,
        }
        if config.intrusive_thoughts_module_settings.enabled:
            clients["intrusive_thoughts_llm_client"] = _create_client(
                models.intrusive_thoughts, "intrusive_thoughts"
            )
        if config.focus_chat_mode.enabled:
            clients["focused_chat_llm_client"] = _create_client(models.focused_chat, "focused_chat")
        if not clients["main_consciousness_llm_client"]:
            raise RuntimeError("主意识LLM客户端初始化失败。")
        if config.focus_chat_mode.enabled and not clients["focused_chat_llm_client"]:
            raise RuntimeError("专注聊天LLM客户端已启用但初始化失败。")
        logger.info("LLM客户端初始化完毕。")
        return clients

    async def _initialize_database_and_services(self) -> dict:
        conn_manager = await ArangoDBConnectionManager.create_from_config(
            config.database,
            core_collection_configs=CoreDBCollections.get_all_core_collection_configs(),
        )
        if not conn_manager or not conn_manager.db:
            raise RuntimeError("数据库连接管理器初始化失败。")
        services_to_create = {
            "event_storage_service": EventStorageService,
            "conversation_storage_service": ConversationStorageService,
            "thought_storage_service": ThoughtStorageService,
            "action_log_service": ActionLogStorageService,
            "person_storage_service": PersonStorageService,
            "summary_storage_service": SummaryStorageService,
        }
        initialized_services = {"conn_manager": conn_manager}
        for instance_name, service_class in services_to_create.items():
            instance = (
                service_class(db_manager=conn_manager)
                if service_class is SummaryStorageService
                else service_class(conn_manager=conn_manager)
            )
            if isinstance(instance, Initializable):
                await instance.initialize_infrastructure()
            initialized_services[instance_name] = instance
        logger.info("所有核心数据存储服务均已初始化。")
        return initialized_services

    async def _initialize_interrupt_model(
        self, event_storage_service: EventStorageService
    ) -> IntelligentInterrupter:
        logger.info("=== 开始初始化中断判断模型（小色猫）... ===")
        iis_builder_instance = IISBuilder(event_storage=event_storage_service)
        semantic_markov_model = await iis_builder_instance.get_or_create_model()
        interrupt_config = config.interrupt_model
        speaker_weights_dict = {
            entry.id: entry.weight for entry in interrupt_config.speaker_weights
        }
        if "default" not in speaker_weights_dict:
            speaker_weights_dict["default"] = 1.0
        interrupt_model_instance = IntelligentInterrupter(
            speaker_weights=speaker_weights_dict,
            objective_keywords=interrupt_config.objective_keywords,
            core_importance_concepts=interrupt_config.core_importance_concepts,
            semantic_markov_model=semantic_markov_model,
        )
        logger.info("=== 中断判断模型（小色猫·无状态版）已成功初始化！ ===")
        return interrupt_model_instance

    async def _get_semantic_model(
        self, event_storage_service: EventStorageService
    ) -> SemanticModel:
        iis_builder_instance = IISBuilder(event_storage=event_storage_service)
        await iis_builder_instance.get_or_create_model()
        return iis_builder_instance.base_semantic_model
