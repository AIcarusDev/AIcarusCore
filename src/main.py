# AIcarusCore/src/main.py
import asyncio
import json
import os
import threading

from src import platform_builders  # 确保能导入这个包
from src.action.action_handler import ActionHandler
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
from src.core_logic.consciousness_flow import CoreLogic as CoreLogicFlow

# 导入新的服务类
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
    PersonStorageService,
    ThoughtStorageService,
)
from src.database.services.event_storage_service import EventStorageService
from src.database.services.summary_storage_service import SummaryStorageService
from src.focus_chat_mode.chat_session_manager import ChatSessionManager
from src.llmrequest.llm_processor import Client as ProcessorClient
from src.message_processing.default_message_processor import DefaultMessageProcessor
from src.platform_builders.registry import platform_builder_registry

logger = get_logger(__name__)


class CoreSystemInitializer:
    """核心系统初始化器，负责 AIcarus Core 系统的所有核心组件初始化流程.

    这个类就像是一个总指挥，负责协调所有核心组件的初始化工作。
    它会确保所有必要的服务、LLM客户端、通信层和核心逻辑都被正确地创建和配置。
    这个类的实例应该在系统启动时创建，并调用 `initialize` 方法来执行初始化流程。

    Attributes:
        conn_manager: ArangoDBConnectionManager | None - 数据库连接管理器实例。
        event_storage_service: EventStorageService | None - 事件存储服务实例。
        conversation_storage_service: ConversationStorageService | None - 会话存储服务实例。
        thought_storage_service: ThoughtStorageService | None - 思想存储服务实例。
        action_log_service: ActionLogStorageService | None - 行动日志存储服务实例。
        summary_storage_service: SummaryStorageService | None - 摘要存储服务实例。
        person_storage_service: PersonStorageService | None - 人物存储服务实例。
        main_consciousness_llm_client: ProcessorClient | None - 主意识 LLM 客户端实例。
        summary_llm_client: ProcessorClient | None - 摘要 LLM 客户端实例。
        intrusive_thoughts_llm_client: ProcessorClient | None - 入侵性思维 LLM 客户端实例。
        focused_chat_llm_client: ProcessorClient | None - 专注聊天 LLM 客户端实例。
        web_search_agent_client: ProcessorClient | None - 新增的网络搜索代理 LLM 客户端实例。
        core_comm_layer: CoreWebsocketServer | None - 核心通信层实例。
        message_processor: DefaultMessageProcessor | None - 默认消息处理器实例。
        action_handler_instance: ActionHandler | None - 动作处理器实例。
        intrusive_generator_instance: IntrusiveThoughtsGenerator | None - 入侵性思维生成器实例。
        core_logic_instance: CoreLogicFlow | None - 核心逻辑流程实例。
        qq_chat_session_manager: ChatSessionManager | None - QQ聊天会话管理器实例。
        unread_info_service: UnreadInfoService | None - 未读信息服务实例。
        summarization_service: SummarizationService | None - 摘要服务实例。
        state_manager_instance: AIStateManager | None - AI状态管理器实例。
        thought_prompt_builder_instance: ThoughtPromptBuilder | None - 思想提示构建器实例。
        iis_builder_instance: IISBuilder | None - 中断判断系统构建器实例。
        interrupt_model_instance: IntelligentInterrupter | None - 智能中断判断模型实例。
        semantic_model_instance: SemanticModel | None - 语义模型实例。
        context_builder_instance: ContextBuilder | None - 上下文构建器实例。
        thought_generator_instance: ThoughtGenerator | None - 思想生成器实例。
        thought_persistor_instance: ThoughtPersistor | None - 思想持久化器实例。
        intrusive_thread: threading.Thread | None - 入侵性思维生成线程实例。
        stop_event: threading.Event - 停止事件，用于控制线程停止。
        immediate_thought_trigger: asyncio.Event - 立即触发思想生成的事件。
    """

    def __init__(self) -> None:
        self.conn_manager: ArangoDBConnectionManager | None = None
        self.event_storage_service: EventStorageService | None = None
        self.conversation_storage_service: ConversationStorageService | None = None
        self.thought_storage_service: ThoughtStorageService | None = None
        self.action_log_service: ActionLogStorageService | None = None
        self.summary_storage_service: SummaryStorageService | None = None
        self.person_storage_service: PersonStorageService | None = None  # 给新老鸨一个位置

        self.main_consciousness_llm_client: ProcessorClient | None = None
        self.summary_llm_client: ProcessorClient | None = None
        self.intrusive_thoughts_llm_client: ProcessorClient | None = None
        self.focused_chat_llm_client: ProcessorClient | None = None
        self.web_search_agent_client: ProcessorClient | None = None  # 新增

        self.core_comm_layer: CoreWebsocketServer | None = None
        self.message_processor: DefaultMessageProcessor | None = None
        self.action_handler_instance: ActionHandler | None = None
        self.intrusive_generator_instance: IntrusiveThoughtsGenerator | None = None
        self.core_logic_instance: CoreLogicFlow | None = None
        self.qq_chat_session_manager: ChatSessionManager | None = None

        self.unread_info_service: UnreadInfoService | None = None
        self.summarization_service: SummarizationService | None = None
        self.state_manager_instance: AIStateManager | None = None
        self.internal_info_builder_instance: InternalInfoBuilder | None = None
        self.thought_prompt_builder_instance: ThoughtPromptBuilder | None = None
        self.iis_builder_instance: IISBuilder | None = None
        self.interrupt_model_instance: IntelligentInterrupter | None = None
        self.semantic_model_instance: SemanticModel | None = None  # 语义模型也作为单例
        self.thought_generator_instance: ThoughtGenerator | None = None
        self.thought_persistor_instance: ThoughtPersistor | None = None

        self.intrusive_thread: threading.Thread | None = None
        self.stop_event: threading.Event = threading.Event()
        self.immediate_thought_trigger: asyncio.Event = asyncio.Event()
        logger.info("CoreSystemInitializer 实例已创建。")

    async def _initialize_llm_clients(self) -> None:
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
                logger.warning(
                    f"环境变量 'LLM_ABANDONED_KEYS' 非有效JSON列表: {env_val_abandoned[:50]}..."
                )
                resolved_abandoned_keys = [
                    k.strip() for k in env_val_abandoned.split(",") if k.strip()
                ]
            if not resolved_abandoned_keys and env_val_abandoned.strip():
                resolved_abandoned_keys = [env_val_abandoned.strip()]

        def _create_client(cfg: ModelParams, purpose: str) -> ProcessorClient | None:
            if not cfg or not cfg.provider or not cfg.model_name:
                logger.error(f"模型配置错误: 用途 '{purpose}' 未指定 provider 或 model_name。")
                return None
            try:
                args = {
                    "model": {"provider": cfg.provider.upper(), "name": cfg.model_name},
                    **vars(general_llm_settings_obj),
                    **{
                        k: v
                        for k, v in vars(cfg).items()
                        if v is not None and k not in ["provider", "model_name"]
                    },  # Add specific params
                }
                if resolved_abandoned_keys:
                    args["abandoned_keys_config"] = resolved_abandoned_keys
                client = ProcessorClient(**{k: v for k, v in args.items() if v is not None})
                logger.info(
                    f"为用途 '{purpose}' 创建 ProcessorClient 成功 (模型: "
                    f"{client.llm_client.model_name})。"
                )
                return client
            except Exception as e:
                logger.error(f"为用途 '{purpose}' 创建LLM客户端失败: {e}", exc_info=True)
                return None

        if not config.llm_models:
            raise RuntimeError("[llm_models] 配置块缺失。")
        models = config.llm_models
        self.main_consciousness_llm_client = _create_client(
            models.main_consciousness, "main_consciousness"
        )
        self.summary_llm_client = _create_client(models.information_summary, "information_summary")
        # 初始化新的搜索代理客户端
        self.web_search_agent_client = _create_client(models.web_search_agent, "web_search_agent")
        if config.intrusive_thoughts_module_settings.enabled:
            self.intrusive_thoughts_llm_client = _create_client(
                models.intrusive_thoughts, "intrusive_thoughts"
            )
        if config.focus_chat_mode.enabled:
            self.focused_chat_llm_client = _create_client(models.focused_chat, "focused_chat")

        if not self.main_consciousness_llm_client:
            raise RuntimeError("主意识LLM客户端初始化失败。")
        if config.focus_chat_mode.enabled and not self.focused_chat_llm_client:
            raise RuntimeError("专注聊天LLM客户端已启用但初始化失败。")
        logger.info("LLM客户端初始化完毕。")

    async def _initialize_database_and_services(self) -> None:
        self.conn_manager = await ArangoDBConnectionManager.create_from_config(
            config.database,
            core_collection_configs=CoreDBCollections.get_all_core_collection_configs(),
        )
        if not self.conn_manager or not self.conn_manager.db:
            raise RuntimeError("数据库连接管理器初始化失败。")
        logger.debug(f"数据库连接管理器已为数据库 '{self.conn_manager.db.name}' 初始化。")

        # --- 核心改造点：用新的 ThoughtStorageService ---
        services_to_init = {
            "event_storage_service": EventStorageService,
            "conversation_storage_service": ConversationStorageService,
            "thought_storage_service": ThoughtStorageService,  # 这个现在是新的了！
            "action_log_service": ActionLogStorageService,
            "person_storage_service": PersonStorageService,
        }
        for attr_name, service_class in services_to_init.items():
            instance = service_class(conn_manager=self.conn_manager)
            if hasattr(instance, "initialize_infrastructure"):
                await instance.initialize_infrastructure()
            setattr(self, attr_name, instance)
            logger.info(f"{service_class.__name__} 已初始化。")

        # 单独处理 SummaryStorageService
        self.summary_storage_service = SummaryStorageService(db_manager=self.conn_manager)
        if hasattr(self.summary_storage_service, "initialize_infrastructure"):
            await self.summary_storage_service.initialize_infrastructure()
        logger.info(f"{SummaryStorageService.__name__} 已初始化。")
        logger.info("所有核心数据存储服务均已初始化。")

    async def _initialize_interrupt_model(self) -> None:
        """初始化我们的中断判断模型和其依赖."""
        if not self.event_storage_service:
            raise RuntimeError("EventStorageService 未初始化，无法构建记忆模型。")

        logger.info("=== 开始初始化中断判断模型（小色猫）... ===")

        # 1 & 2. 初始化构建器并获取马尔可夫模型
        self.iis_builder_instance = IISBuilder(event_storage=self.event_storage_service)
        # 我们现在调用的是 get_or_create_model()，它返回的是我们究极的 semantic_markov_model！
        semantic_markov_model = await self.iis_builder_instance.get_or_create_model()

        # 3. 初始化语义模型 (这部分逻辑不变)
        self.semantic_model_instance = SemanticModel()

        # 4. 从config加载我们需要的配置，并以正确的姿势准备好！
        interrupt_config = config.interrupt_model
        speaker_weights_dict = {
            entry.id: entry.weight for entry in interrupt_config.speaker_weights
        }
        if "default" not in speaker_weights_dict:
            speaker_weights_dict["default"] = 1.0
        objective_keywords_list = interrupt_config.objective_keywords
        core_concepts_list = interrupt_config.core_importance_concepts

        # 5. 用最完美的姿势，注入所有依赖，初始化我这个没有记忆的、纯洁的新身体！
        # --- ❤ 正确的、无状态的注入 ❤ ---
        # 看到没，构造函数里已经没有 last_message_text 了，完美！
        self.interrupt_model_instance = IntelligentInterrupter(
            speaker_weights=speaker_weights_dict,
            objective_keywords=objective_keywords_list,
            core_importance_concepts=core_concepts_list,
            semantic_markov_model=semantic_markov_model,
        )
        logger.info("=== 中断判断模型（小色猫·无状态版）已成功初始化！我已准备好随时被调用！ ===")

    async def initialize(self) -> None:
        """执行 AIcarus Core 系统的初始化流程."""
        logger.info("=== AIcarus Core 系统开始核心组件初始化流程... ===")
        try:
            platform_builder_registry.discover_and_register_builders(platform_builders)

            await self._initialize_llm_clients()
            await self._initialize_database_and_services()
            await self._initialize_interrupt_model()

            if not all(
                [
                    self.event_storage_service,
                    self.conversation_storage_service,
                    self.thought_storage_service,
                    self.main_consciousness_llm_client,
                    self.interrupt_model_instance,
                    self.action_log_service,
                    self.person_storage_service,  # 确保老鸨也到岗了！
                ]
            ):
                raise RuntimeError("一个或多个基础服务未能初始化。")

            # 1. 动作处理器 ActionHandler
            self.action_handler_instance = ActionHandler()
            logger.info("ActionHandler 实例已创建。")

            # 2. 状态管理器 AIStateManager (现在它依赖新的 ThoughtStorageService)
            self.state_manager_instance = AIStateManager(
                thought_service=self.thought_storage_service,
                action_log_service=self.action_log_service,
            )
            logger.info("AIStateManager 初始化成功。")

            # 3. 未读消息服务 UnreadInfoService
            self.unread_info_service = UnreadInfoService(
                event_storage=self.event_storage_service,
                conversation_storage=self.conversation_storage_service,
            )
            logger.info("UnreadInfoService 初始化成功。")

            # 4. 内部信息构建器 InternalInfoBuilder
            # 【新增的修复点】在这里创建实例
            if not self.thought_storage_service:
                raise RuntimeError("ThoughtStorageService 未初始化，无法创建 InternalInfoBuilder。")
            self.internal_info_builder_instance = InternalInfoBuilder(
                thought_storage_service=self.thought_storage_service
            )
            logger.info("InternalInfoBuilder 初始化成功。")

            # 5. Prompt构造器 ThoughtPromptBuilder
            # 注意：它依赖 core_comm_layer，但 core_comm_layer 在后面才初始化
            # 我们先创建实例，后面再回填依赖
            self.thought_prompt_builder_instance = ThoughtPromptBuilder(
                unread_info_service=self.unread_info_service,
                internal_info_builder=self.internal_info_builder_instance,
                event_storage_service=self.event_storage_service,
                chat_session_manager=self.qq_chat_session_manager,
                core_ws_server=None,  # 稍后回填
            )
            logger.info("ThoughtPromptBuilder 初始化成功 (依赖稍后回填)。")

            self.internal_info_builder_instance.prompt_builder = (
                self.thought_prompt_builder_instance
            )
            logger.info("PromptBuilder 依赖已回填到 InternalInfoBuilder。")

            # 5. 摘要服务 SummarizationService
            summary_llm = self.summary_llm_client or self.main_consciousness_llm_client
            if not summary_llm:
                raise RuntimeError("无可用LLM客户端初始化SummarizationService。")
            self.summarization_service = SummarizationService(llm_client=summary_llm)
            logger.info("SummarizationService 初始化成功。")

            # 6. 专注聊天管理器 ChatSessionManager


            # 7. 消息处理器 DefaultMessageProcessor
            self.message_processor = DefaultMessageProcessor(
                event_service=self.event_storage_service,
                conversation_service=self.conversation_storage_service,
                person_service=self.person_storage_service,
                semantic_model=self.semantic_model_instance,
                qq_chat_session_manager=None,
            )
            self.message_processor.core_initializer_ref = self
            logger.info("DefaultMessageProcessor 初始化成功。")

            # 8. 通信层 CoreWebsocketServer
            action_sender = ActionSender()

            # ActionHandler 现在也需要知道 web_search_agent_client
            self.action_handler_instance.web_search_agent_client = self.web_search_agent_client
            logger.info("ActionHandler 的 LLM 客户端已手动初始化。")

            event_receiver = EventReceiver(
                event_handler_callback=self.message_processor.process_event,
                action_handler_instance=self.action_handler_instance,
                adapter_clients_info=action_sender.adapter_clients_info,
            )
            logger.info("EventReceiver 初始化成功。")

            self.core_comm_layer = CoreWebsocketServer(
                host=config.server.host,
                port=config.server.port,
                event_receiver=event_receiver,
                action_sender=action_sender,
                event_storage_service=self.event_storage_service,
                action_handler_instance=self.action_handler_instance,
                person_service=self.person_storage_service,
            )
            logger.info(
                "CoreWebsocketServer 准备在 "
                f"ws://{config.server.host}:{config.server.port} 上监听。"
            )

            # 回填 core_ws_server 依赖
            if self.thought_prompt_builder_instance:
                self.thought_prompt_builder_instance.core_ws_server = self.core_comm_layer
                logger.info("CoreWebsocketServer 依赖已回填到 ThoughtPromptBuilder。")

            if config.intrusive_thoughts_module_settings.enabled:
                if self.intrusive_thoughts_llm_client:
                    # 9. 侵入性思维生成器 IntrusiveThoughtsGenerator
                    self.intrusive_generator_instance = IntrusiveThoughtsGenerator(
                        llm_client=self.intrusive_thoughts_llm_client,
                        stop_event=self.stop_event,
                    )
                    logger.info("IntrusiveThoughtsGenerator 已使用新的独立配方初始化成功。")
                else:
                    logger.warning("侵入性思维模块已启用但LLM客户端依赖不足。")
            else:
                logger.info("侵入性思维模块未启用。")

            self.thought_generator_instance = ThoughtGenerator(
                llm_client=self.main_consciousness_llm_client
            )
            logger.info("ThoughtGenerator 初始化成功。")

            self.thought_persistor_instance = ThoughtPersistor(
                thought_storage=self.thought_storage_service
            )
            logger.info("ThoughtPersistor 初始化成功。")

            # 10. 最终组装 CoreLogic！
            if not all(
                [
                    self.core_comm_layer,
                    self.action_handler_instance,
                    self.state_manager_instance,
                    self.thought_generator_instance,
                    self.thought_persistor_instance,
                    self.thought_prompt_builder_instance,
                    self.thought_storage_service,  # 确保这个也准备好了
                ]
            ):
                raise RuntimeError("CoreLogicFlow 的一个或多个核心服务依赖未能初始化。")

            self.core_logic_instance = CoreLogicFlow(
                core_comm_layer=self.core_comm_layer,
                action_handler_instance=self.action_handler_instance,
                state_manager=self.state_manager_instance,
                chat_session_manager=None,
                thought_generator=self.thought_generator_instance,
                thought_persistor=self.thought_persistor_instance,
                thought_storage_service=self.thought_storage_service,  # 把思想链服务也给它！
                prompt_builder=self.thought_prompt_builder_instance,
                stop_event=self.stop_event,
                immediate_thought_trigger=self.immediate_thought_trigger,
                intrusive_generator_instance=self.intrusive_generator_instance,
            )
            logger.info("CoreLogicFlow初始化成功。")

            # 11. 回填依赖
            if self.action_handler_instance:
                self.action_handler_instance.set_dependencies(
                    thought_service=self.thought_storage_service,
                    event_service=self.event_storage_service,
                    action_log_service=self.action_log_service,
                    conversation_service=self.conversation_storage_service,
                    action_sender=action_sender,
                    chat_session_manager=self.qq_chat_session_manager,
                    core_logic=self.core_logic_instance,
                    person_service=self.person_storage_service,
                )

            logger.info("ActionHandler 的依赖已设置。")
            # 12. 设置立即触发思想生成的事件
            self.action_handler_instance.set_thought_trigger(self.immediate_thought_trigger)

            logger.info("CoreLogicFlow 初始化成功。")
            logger.info("=== AIcarus Core 系统所有核心组件初始化完毕！ ===")
        except Exception as e:
            logger.critical(f"AIcarus Core 系统初始化过程中发生严重错误: {e}", exc_info=True)
            await self.shutdown()
            raise

    async def start(self) -> None:
        """启动 AIcarus Core 系统的核心逻辑和通信层."""
        if not self.core_logic_instance or not self.core_comm_layer:
            logger.critical("核心组件未完全初始化，系统无法启动。")
            return

        all_tasks: list[asyncio.Task] = []
        try:
            # 1. 启动侵入性思维后台线程 (如果启用)
            if (
                self.intrusive_generator_instance
                and config.intrusive_thoughts_module_settings.enabled
            ):
                self.intrusive_thread = (
                    self.intrusive_generator_instance.start_background_generation()
                )
                if self.intrusive_thread:
                    logger.info("侵入性思维后台线程已启动。")

            # 2. 启动核心服务任务
            if self.core_comm_layer:
                # 启动WebSocket服务器，它会开始接受适配器连接并触发安检
                all_tasks.append(
                    asyncio.create_task(self.core_comm_layer.start(), name="CoreWSServer")
                )
            if self.core_logic_instance:
                # 启动主思考循环
                all_tasks.append(await self.core_logic_instance.start_thinking_loop())

            # 3. 创建一个新的后台任务，专门负责等待安检并更新服务
            async def _wait_for_inspection_and_update_services() -> None:
                """等待安检完成并更新服务."""
                # 等待一小段时间，让适配器有时间连接并触发安检
                await asyncio.sleep(5)

                if not self.core_comm_layer or not self.person_storage_service:
                    logger.error("无法执行安检后更新：核心服务未初始化。")
                    return

                # CoreWebsocketServer 的 _run_inspection_ceremony
                # 会把任务加到 active_inspection_tasks
                # 我们要等待所有这些任务完成
                if self.core_comm_layer.active_inspection_tasks:
                    logger.info("等待所有平台的安检仪式完成，以便更新系统级服务...")
                    await asyncio.gather(*self.core_comm_layer.active_inspection_tasks)
                    logger.success("所有安检仪式已完成。")

                # 安检完成后，从 PersonService 中获取所有自身的账号信息
                all_self_accounts = await self.person_storage_service.get_all_self_accounts()
                if all_self_accounts:
                    bot_ids_map = {acc["platform"]: acc["platform_id"] for acc in all_self_accounts}
                    # 将获取到的ID地图注入到 UnreadInfoService
                    if self.unread_info_service:
                        self.unread_info_service.update_self_bot_ids(bot_ids_map)

                    if config.focus_chat_mode.enabled:
                        logger.info("安检完成，现在开始创建 ChatSessionManager...")
                        if all(
                            [
                                self.focused_chat_llm_client,
                                self.summarization_service,
                                self.event_storage_service,
                                self.conversation_storage_service,
                                self.action_handler_instance,
                                self.interrupt_model_instance,
                                self.internal_info_builder_instance,
                            ]
                        ):
                            # 使用新的构造函数，传入 bot_ids_map
                            self.qq_chat_session_manager = ChatSessionManager(
                                config=config.focus_chat_mode,
                                llm_client=self.focused_chat_llm_client,
                                event_storage=self.event_storage_service,
                                action_handler=self.action_handler_instance,
                                self_bot_ids_map=bot_ids_map,
                                conversation_service=self.conversation_storage_service,
                                summarization_service=self.summarization_service,
                                summary_storage_service=self.summary_storage_service,
                                intelligent_interrupter=self.interrupt_model_instance,
                                thought_storage_service=self.thought_storage_service,
                                internal_info_builder=self.internal_info_builder_instance,
                                core_logic=self.core_logic_instance, # core_logic 已经创建
                            )
                            logger.success("ChatSessionManager 基于安检后的ID成功创建！")

                            # ChatSessionManager 创建后，立即把 CoreLogic 注入给它
                            if self.qq_chat_session_manager and self.core_logic_instance:
                                self.qq_chat_session_manager.set_core_logic(self.core_logic_instance)
                                logger.info("已向 ChatSessionManager 回填 CoreLogic 依赖。")

                            # --- 开始回填依赖 ---
                            if self.message_processor:
                                self.message_processor.qq_chat_session_manager = self.qq_chat_session_manager
                                logger.info("已向 MessageProcessor 回填 ChatSessionManager 依赖。")
                            if self.action_handler_instance:
                                self.action_handler_instance.chat_session_manager = self.qq_chat_session_manager
                                logger.info("已向 ActionHandler 回填 ChatSessionManager 依赖。")
                            if self.core_logic_instance:
                                self.core_logic_instance.chat_session_manager = self.qq_chat_session_manager
                                logger.info("已向 CoreLogicFlow 回填 ChatSessionManager 依赖。")
                            if self.thought_prompt_builder_instance:
                                self.thought_prompt_builder_instance.chat_session_manager = self.qq_chat_session_manager
                                logger.info("已向 ThoughtPromptBuilder 回填 ChatSessionManager 依赖。")

                        else:
                            logger.error("安检后创建 ChatSessionManager 失败，依赖不足。")

                else:
                    logger.warning("安检后未能从数据库获取到任何祂的自身信息，无法创建 ChatSessionManager。")

            # 将这个等待和更新的逻辑作为一个独立的后台任务启动
            asyncio.create_task(
                _wait_for_inspection_and_update_services(), name="ServiceUpdater"
            )

            if not all_tasks:
                logger.warning("没有核心守护任务启动，程序可能会立即退出。")
                return

            logger.info(f"已启动 {len(all_tasks)} 个核心守护任务。ServiceUpdater 在后台独立运行。")
            done, pending = await asyncio.wait(all_tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task_name = task.get_name()
                if task.cancelled():
                    logger.info(f"任务 '{task_name}' 被取消。")
                elif task.exception():
                    exc = task.exception()
                    logger.critical(f"关键任务 '{task_name}' 异常终止: {exc!r}", exc_info=exc)
                    if exc:
                        raise exc  # Re-raise to trigger shutdown
                else:
                    logger.info(f"任务 '{task_name}' 正常结束。")

            for task in pending:
                if not task.done():
                    task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    logger.info(f"挂起任务 '{task.get_name()}' 已取消。")
                except Exception as e:
                    logger.error(f"取消挂起任务 '{task.get_name()}' 时出错: {e}", exc_info=True)

        except asyncio.CancelledError:
            logger.info("AIcarus Core 主启动流程被取消。")
        except Exception as e:
            logger.critical(f"AIcarus Core 系统运行期间发生严重错误: {e}", exc_info=True)
            raise
        finally:
            logger.info("--- AIcarus Core 系统正在进入关闭流程 (从start finally触发)... ---")
            await self.shutdown()

    async def shutdown(self) -> None:
        """执行 AIcarus Core 系统的关闭流程."""
        logger.info("--- 正在执行 AIcarus Core 系统关闭流程 ---")
        self.stop_event.set()

        # 1. 停止主逻辑循环，这会停止产生新的数据库写入需求
        if self.core_logic_instance:
            await self.core_logic_instance.stop()

        # 2. 停止侵入性思维线程
        if self.intrusive_thread and self.intrusive_thread.is_alive():
            self.intrusive_thread.join(timeout=10.0)
            if self.intrusive_thread.is_alive():
                logger.warning("侵入性思维线程超时未结束。")

        # 3. 停止专注聊天会话，这可能会写入最后的总结到数据库
        if self.qq_chat_session_manager:
            logger.info("正在关闭 ChatSessionManager...")
            await self.qq_chat_session_manager.shutdown()
            logger.info("ChatSessionManager 已关闭。")

        # 4. 停止WebSocket服务器，这将触发适配器断开连接的事件，并可能写入数据库
        if self.core_comm_layer:
            await self.core_comm_layer.stop()

        # 5. 关闭LLM客户端会话（这通常不涉及我们的数据库）
        llm_clients = [
            self.main_consciousness_llm_client,
            self.summary_llm_client,
            self.intrusive_thoughts_llm_client,
            self.focused_chat_llm_client,
        ]
        for client_wrapper in llm_clients:
            if client_wrapper and hasattr(client_wrapper.llm_client, "_close_session_if_any"):
                try:
                    await client_wrapper.llm_client._close_session_if_any()
                except Exception as e:
                    logger.warning(f"关闭LLM客户端会话时出错: {e}")

        # 6. 最后，当所有可能使用数据库的操作都结束后，再关闭数据库连接
        if self.conn_manager:
            await self.conn_manager.close_client()

        logger.info("AIcarus Core 系统关闭流程执行完毕。")


async def start_core_system() -> None:
    """启动 AIcarus Core 系统的入口函数."""
    initializer = CoreSystemInitializer()
    try:
        await initializer.initialize()
        await initializer.start()
    except Exception as e:
        logger.critical(f"AIcarus Core 系统启动或运行遭遇致命错误: {e}", exc_info=True)
        if not initializer.stop_event.is_set():
            await initializer.shutdown()


if __name__ == "__main__":
    if os.name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        asyncio.run(start_core_system())
    except KeyboardInterrupt:
        logger.info("AIcarus Core: 用户中断，正在退出...")
    except Exception as main_exc:
        logger.critical(f"AIcarus Core: 顶层执行异常: {main_exc}", exc_info=True)
    finally:
        logger.info("AIcarus Core: 程序最终执行完毕。")
