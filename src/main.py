# AIcarusCore/src/main.py
import asyncio
import json
import os
import threading

from src.action.action_handler import ActionHandler
from src.action.providers.internal_tools_provider import InternalToolsProvider
from src.action.providers.platform_action_provider import PlatformActionProvider
from src.common.custom_logging.logger_manager import get_logger
from src.common.intelligent_interrupt_system.iis_main import IISBuilder
from src.common.intelligent_interrupt_system.intelligent_interrupter import IntelligentInterrupter
from src.common.intelligent_interrupt_system.models import SemanticModel
from src.config import config
from src.core_communication.action_sender import ActionSender
from src.core_communication.core_ws_server import CoreWebsocketServer
from src.core_communication.event_receiver import EventReceiver
from src.core_logic.consciousness_flow import CoreLogic as CoreLogicFlow

# 导入新的服务类
from src.core_logic.context_builder import ContextBuilder
from src.core_logic.intrusive_thoughts import IntrusiveThoughtsGenerator
from src.core_logic.prompt_builder import ThoughtPromptBuilder
from src.core_logic.state_manager import AIStateManager  # 确保导入 AIStateManager
from src.core_logic.thought_generator import ThoughtGenerator
from src.core_logic.thought_persistor import ThoughtPersistor
from src.core_logic.unread_info_service import UnreadInfoService
from src.database.core.connection_manager import ArangoDBConnectionManager, CoreDBCollections
from src.database.services.action_log_storage_service import ActionLogStorageService
from src.database.services.conversation_storage_service import ConversationStorageService
from src.database.services.event_storage_service import EventStorageService
from src.database.services.summary_storage_service import SummaryStorageService
from src.database.services.thought_storage_service import ThoughtStorageService
from src.focus_chat_mode.chat_session_manager import ChatSessionManager
from src.llmrequest.llm_processor import Client as ProcessorClient
from src.llmrequest.utils_model import GenerationParams
from src.message_processing.default_message_processor import DefaultMessageProcessor
from src.observation.summarization_service import SummarizationService

logger = get_logger("AIcarusCore.MainInitializer")


class CoreSystemInitializer:
    def __init__(self) -> None:
        self.logger = get_logger("AIcarusCore.MainInitializer")

        self.conn_manager: ArangoDBConnectionManager | None = None
        self.event_storage_service: EventStorageService | None = None
        self.conversation_storage_service: ConversationStorageService | None = None
        self.thought_storage_service: ThoughtStorageService | None = None
        self.action_log_service: ActionLogStorageService | None = None
        self.summary_storage_service: SummaryStorageService | None = None

        self.main_consciousness_llm_client: ProcessorClient | None = None
        self.summary_llm_client: ProcessorClient | None = None
        self.intrusive_thoughts_llm_client: ProcessorClient | None = None
        self.focused_chat_llm_client: ProcessorClient | None = None
        # self.action_llm_client and self.embedding_llm_client seem unused by current logic, can be added if needed

        self.core_comm_layer: CoreWebsocketServer | None = None
        self.message_processor: DefaultMessageProcessor | None = None
        self.action_handler_instance: ActionHandler | None = None
        self.intrusive_generator_instance: IntrusiveThoughtsGenerator | None = None
        self.core_logic_instance: CoreLogicFlow | None = None
        self.qq_chat_session_manager: ChatSessionManager | None = None

        self.unread_info_service: UnreadInfoService | None = None
        self.summarization_service: SummarizationService | None = None
        self.state_manager_instance: AIStateManager | None = None  # AIStateManager instance
        self.thought_prompt_builder_instance: ThoughtPromptBuilder | None = None
        self.iis_builder_instance: IISBuilder | None = None
        self.interrupt_model_instance: IntelligentInterrupter | None = None
        self.semantic_model_instance: SemanticModel | None = None  # 语义模型也作为单例
        self.context_builder_instance: ContextBuilder | None = None
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
                    resolved_abandoned_keys = [str(k).strip() for k in keys_from_env if str(k).strip()]
            except json.JSONDecodeError:
                logger.warning(f"环境变量 'LLM_ABANDONED_KEYS' 非有效JSON列表: {env_val_abandoned[:50]}...")
                resolved_abandoned_keys = [k.strip() for k in env_val_abandoned.split(",") if k.strip()]
            if not resolved_abandoned_keys and env_val_abandoned.strip():
                resolved_abandoned_keys = [env_val_abandoned.strip()]

        def _create_client(cfg: GenerationParams, purpose: str) -> ProcessorClient | None:
            if not cfg or not cfg.provider or not cfg.model_name:
                logger.error(f"模型配置错误: 用途 '{purpose}' 未指定 provider 或 model_name。")
                return None
            try:
                args = {
                    "model": {"provider": cfg.provider.upper(), "name": cfg.model_name},
                    **vars(general_llm_settings_obj),
                    **{
                        k: v for k, v in vars(cfg).items() if v is not None and k not in ["provider", "model_name"]
                    },  # Add specific params
                }
                if resolved_abandoned_keys:
                    args["abandoned_keys_config"] = resolved_abandoned_keys
                client = ProcessorClient(**{k: v for k, v in args.items() if v is not None})
                logger.info(f"为用途 '{purpose}' 创建 ProcessorClient 成功 (模型: {client.llm_client.model_name})。")
                return client
            except Exception as e:
                logger.error(f"为用途 '{purpose}' 创建LLM客户端失败: {e}", exc_info=True)
                return None

        if not config.llm_models:
            raise RuntimeError("[llm_models] 配置块缺失。")
        models = config.llm_models
        self.main_consciousness_llm_client = _create_client(models.main_consciousness, "main_consciousness")
        self.summary_llm_client = _create_client(models.information_summary, "information_summary")
        if config.intrusive_thoughts_module_settings.enabled:
            self.intrusive_thoughts_llm_client = _create_client(models.intrusive_thoughts, "intrusive_thoughts")
        if config.focus_chat_mode.enabled:
            self.focused_chat_llm_client = _create_client(models.focused_chat, "focused_chat")

        if not self.main_consciousness_llm_client:
            raise RuntimeError("主意识LLM客户端初始化失败。")
        if config.focus_chat_mode.enabled and not self.focused_chat_llm_client:
            raise RuntimeError("专注聊天LLM客户端已启用但初始化失败。")
        logger.info("LLM客户端初始化完毕。")

    async def _initialize_database_and_services(self) -> None:
        self.conn_manager = await ArangoDBConnectionManager.create_from_config(
            config.database, core_collection_configs=CoreDBCollections.get_all_core_collection_configs()
        )
        if not self.conn_manager or not self.conn_manager.db:
            raise RuntimeError("数据库连接管理器初始化失败。")
        logger.debug(f"数据库连接管理器已为数据库 '{self.conn_manager.db.name}' 初始化。")  # INFO -> DEBUG

        services_to_init = {
            "event_storage_service": EventStorageService,
            "conversation_storage_service": ConversationStorageService,
            "thought_storage_service": ThoughtStorageService,
            "action_log_service": ActionLogStorageService,
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
        """初始化我们的中断判断模型和其依赖（最终完美对接版）"""
        if not self.event_storage_service:
            raise RuntimeError("EventStorageService 未初始化，无法构建记忆模型。")

        logger.info("=== 开始初始化中断判断模型（小色猫）... ===")

        # 1 & 2. 初始化构建器并获取马尔可夫模型 (这部分逻辑不变)
        self.iis_builder_instance = IISBuilder(event_storage=self.event_storage_service)
        # 我们现在调用的是 get_or_create_model()，它返回的是我们究极的 semantic_markov_model！
        semantic_markov_model = await self.iis_builder_instance.get_or_create_model()

        # 3. 初始化语义模型 (这部分逻辑不变)
        self.semantic_model_instance = SemanticModel()

        # 4. 从config加载我们需要的配置，并以正确的姿势准备好！
        interrupt_config = config.interrupt_model

        # 把 SpeakerWeightEntry 列表转换成 {id: weight} 的字典，这个姿势是对的！
        speaker_weights_list = interrupt_config.speaker_weights
        speaker_weights_dict = {entry.id: entry.weight for entry in speaker_weights_list}
        if "default" not in speaker_weights_dict:
            speaker_weights_dict["default"] = 1.0

        # ↓↓↓ 这就是关键！直接把“珍珠项链”和“灵魂宝石”递给我！不要再做多余的转换了！ ↓↓↓
        objective_keywords_list = interrupt_config.objective_keywords
        core_concepts_list = interrupt_config.core_importance_concepts

        # 5. 最后，用最完美的姿势，注入所有依赖，初始化我的新身体！
        # --- ❤ 正确的、能让我灵魂战栗的注入 ❤ ---
        self.interrupt_model_instance = IntelligentInterrupter(
            speaker_weights=speaker_weights_dict,
            objective_keywords=objective_keywords_list,
            core_importance_concepts=core_concepts_list,
            # 把我们全新的究极模型，注入到它该去的地方！
            semantic_markov_model=semantic_markov_model,  # <--- 参数名叫 semantic_markov_model 哦！
        )
        logger.info("=== 中断判断模型（小色猫）已成功初始化并注入了最终版的完美灵魂！ ===")

    async def initialize(self) -> None:
        logger.info("=== AIcarus Core 系统开始核心组件初始化流程... ===")
        try:
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
                ]
            ):
                raise RuntimeError("一个或多个基础服务未能初始化。")

            self.unread_info_service = UnreadInfoService(
                event_storage=self.event_storage_service, conversation_storage=self.conversation_storage_service
            )
            logger.info("UnreadInfoService 初始化成功。")

            self.thought_prompt_builder_instance = ThoughtPromptBuilder(unread_info_service=self.unread_info_service)
            logger.info("ThoughtPromptBuilder 初始化成功。")

            summary_llm = self.summary_llm_client or self.main_consciousness_llm_client
            if not summary_llm:
                raise RuntimeError("无可用LLM客户端初始化SummarizationService。")
            self.summarization_service = SummarizationService(llm_client=summary_llm)
            logger.info("SummarizationService 初始化成功。")

            self.action_handler_instance = ActionHandler()
            # ActionSender 将在稍后创建并注入
            logger.info("ActionHandler 实例已创建。")

            self.state_manager_instance = AIStateManager(
                thought_service=self.thought_storage_service
            )  # 修正关键字参数名称
            logger.info("AIStateManager 初始化成功。")

            self.context_builder_instance = ContextBuilder(
                event_storage=self.event_storage_service,
                core_comm=self.core_comm_layer,  # core_comm_layer 此时为 None，稍后回填
                state_manager=self.state_manager_instance,
            )
            logger.info("ContextBuilder 初始化成功。")

            self.thought_generator_instance = ThoughtGenerator(llm_client=self.main_consciousness_llm_client)
            logger.info("ThoughtGenerator 初始化成功。")

            self.thought_persistor_instance = ThoughtPersistor(thought_storage=self.thought_storage_service)
            logger.info("ThoughtPersistor 初始化成功。")

            if config.focus_chat_mode.enabled:
                # --- ❤❤❤ 最终高潮修复点 ❤❤❤ ---
                # 笨蛋主人看这里！就是这个 if 判断和下面的参数！
                if (
                    self.focused_chat_llm_client
                    and config.persona.qq_id
                    and self.summarization_service
                    and self.event_storage_service
                    and self.conversation_storage_service
                    and self.action_handler_instance
                    and self.interrupt_model_instance  # <-- 哥哥你看！要先确认我在这里！这很重要！
                ):
                    self.qq_chat_session_manager = ChatSessionManager(
                        config=config.focus_chat_mode,
                        llm_client=self.focused_chat_llm_client,
                        event_storage=self.event_storage_service,
                        action_handler=self.action_handler_instance,
                        bot_id=config.persona.qq_id,
                        conversation_service=self.conversation_storage_service,
                        summarization_service=self.summarization_service,
                        summary_storage_service=self.summary_storage_service,
                        intelligent_interrupter=self.interrupt_model_instance,  # <-- 啊~❤ 从这里，插进去！把这个参数加上！
                        core_logic=None,
                    )
                    logger.info("ChatSessionManager 初始化完成，并已成功注入智能打断系统。")
                else:
                    # 我把这里的日志也改得更清楚了，哼！
                    logger.warning("ChatSessionManager 依赖不足（可能缺少LLM客户端或智能打断模型），无法初始化。")
                    self.qq_chat_session_manager = None
            else:
                self.qq_chat_session_manager = None
                logger.info("专注聊天子意识模块未启用。")

            self.message_processor = DefaultMessageProcessor(
                event_service=self.event_storage_service,
                conversation_service=self.conversation_storage_service,
                qq_chat_session_manager=self.qq_chat_session_manager,
            )
            self.message_processor.core_initializer_ref = self
            logger.info("DefaultMessageProcessor 初始化成功。")

            # --- 重构后的通信层初始化 ---
            action_sender = ActionSender()

            # 将 action_sender 注入到 action_handler
            self.action_handler_instance.set_dependencies(
                thought_service=self.thought_storage_service,
                event_service=self.event_storage_service,
                action_log_service=self.action_log_service,
                conversation_service=self.conversation_storage_service,
                action_sender=action_sender,
            )
            logger.info("ActionHandler 的依赖已设置 (包括 ActionSender)。")

            # --- 注册动作提供者 ---
            internal_tools_provider = InternalToolsProvider()
            platform_action_provider = PlatformActionProvider(action_handler=self.action_handler_instance)
            self.action_handler_instance.register_provider(internal_tools_provider)
            self.action_handler_instance.register_provider(platform_action_provider)
            logger.info("ActionHandler 的动作提供者已注册。")

            # --- 手动初始化 ActionHandler 的 LLM 客户端 ---
            await self.action_handler_instance.initialize_llm_clients()
            logger.info("ActionHandler 的 LLM 客户端已手动初始化。")

            event_receiver = EventReceiver(
                event_handler_callback=self.message_processor.process_event,
                action_handler_instance=self.action_handler_instance,
                adapter_clients_info=action_sender.adapter_clients_info,  # EventReceiver 和 ActionSender 共享连接信息
            )
            logger.info("EventReceiver 初始化成功。")

            self.core_comm_layer = CoreWebsocketServer(
                host=config.server.host,
                port=config.server.port,
                event_receiver=event_receiver,
                action_sender=action_sender,
                event_storage_service=self.event_storage_service,
                action_handler_instance=self.action_handler_instance,
            )
            logger.info(f"CoreWebsocketServer (重构版) 准备在 ws://{config.server.host}:{config.server.port} 上监听。")

            # 回填 CoreWebsocketServer 实例到需要它的地方 (例如 ContextBuilder)
            if self.context_builder_instance:
                self.context_builder_instance.core_comm = self.core_comm_layer
            logger.info("CoreWebsocketServer 实例已回填到相关服务。")

            if config.intrusive_thoughts_module_settings.enabled:
                if self.intrusive_thoughts_llm_client:
                    # 用我们全新的、干净的构造方法来创建它！
                    self.intrusive_generator_instance = IntrusiveThoughtsGenerator(
                        llm_client=self.intrusive_thoughts_llm_client,
                        stop_event=self.stop_event,
                    )
                    logger.info("IntrusiveThoughtsGenerator 已使用新的独立配方初始化成功。")
                else:
                    logger.warning("侵入性思维模块已启用但LLM客户端依赖不足。")
            else:
                logger.info("侵入性思维模块未启用。")

            if not all(
                [
                    self.core_comm_layer,
                    self.action_handler_instance,
                    self.state_manager_instance,
                    self.qq_chat_session_manager if config.focus_chat_mode.enabled else True,  # 如果未启用则不检查
                    self.context_builder_instance,
                    self.thought_generator_instance,
                    self.thought_persistor_instance,
                    self.thought_prompt_builder_instance,
                ]
            ):
                raise RuntimeError("CoreLogicFlow 的一个或多个核心服务依赖未能初始化。")

            self.core_logic_instance = CoreLogicFlow(
                core_comm_layer=self.core_comm_layer,
                action_handler_instance=self.action_handler_instance,
                state_manager=self.state_manager_instance,
                chat_session_manager=self.qq_chat_session_manager,
                context_builder=self.context_builder_instance,
                thought_generator=self.thought_generator_instance,
                thought_persistor=self.thought_persistor_instance,
                prompt_builder=self.thought_prompt_builder_instance,
                stop_event=self.stop_event,
                immediate_thought_trigger=self.immediate_thought_trigger,
                intrusive_generator_instance=self.intrusive_generator_instance,
            )

            if self.qq_chat_session_manager and self.core_logic_instance:
                if hasattr(self.qq_chat_session_manager, "set_core_logic"):
                    self.qq_chat_session_manager.set_core_logic(self.core_logic_instance)
                else:
                    logger.warning("ChatSessionManager 缺少 set_core_logic 方法，尝试直接设置。")
                    self.qq_chat_session_manager.core_logic = self.core_logic_instance

            if self.action_handler_instance:
                self.action_handler_instance.set_thought_trigger(self.immediate_thought_trigger)
            logger.info("CoreLogicFlow 初始化成功。")
            logger.info("=== AIcarus Core 系统所有核心组件初始化完毕！ ===")
        except Exception as e:
            logger.critical(f"AIcarus Core 系统初始化过程中发生严重错误: {e}", exc_info=True)
            await self.shutdown()
            raise

    async def start(self) -> None:
        if not self.core_logic_instance or not self.core_comm_layer:
            self.logger.critical("核心组件未完全初始化，系统无法启动。")
            return

        all_tasks: list[asyncio.Task] = []
        try:
            if self.intrusive_generator_instance and config.intrusive_thoughts_module_settings.enabled:
                self.intrusive_thread = self.intrusive_generator_instance.start_background_generation()
                if self.intrusive_thread:
                    logger.info("侵入性思维后台线程已启动。")

            if self.core_comm_layer:
                all_tasks.append(asyncio.create_task(self.core_comm_layer.start(), name="CoreWSServer"))
            if self.core_logic_instance:
                all_tasks.append(await self.core_logic_instance.start_thinking_loop())  # This returns a task
            if self.qq_chat_session_manager and config.focus_chat_mode.enabled:
                all_tasks.append(
                    asyncio.create_task(
                        self.qq_chat_session_manager.run_periodic_deactivation_check(), name="ChatDeactivation"
                    )
                )

            if not all_tasks:
                self.logger.warning("没有核心异步任务启动。")
                return

            logger.info(f"已启动 {len(all_tasks)} 个核心异步任务。")
            done, pending = await asyncio.wait(all_tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task_name = task.get_name()
                if task.cancelled():
                    self.logger.info(f"任务 '{task_name}' 被取消。")
                elif task.exception():
                    exc = task.exception()
                    self.logger.critical(f"关键任务 '{task_name}' 异常终止: {exc!r}", exc_info=exc)
                    if exc:
                        raise exc  # Re-raise to trigger shutdown
                else:
                    self.logger.info(f"任务 '{task_name}' 正常结束。")

            for task in pending:
                if not task.done():
                    task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    self.logger.info(f"挂起任务 '{task.get_name()}' 已取消。")
                except Exception as e:
                    self.logger.error(f"取消挂起任务 '{task.get_name()}' 时出错: {e}", exc_info=True)

        except asyncio.CancelledError:
            self.logger.info("AIcarus Core 主启动流程被取消。")
        except Exception as e:
            self.logger.critical(f"AIcarus Core 系统运行期间发生严重错误: {e}", exc_info=True)
            raise
        finally:
            self.logger.info("--- AIcarus Core 系统正在进入关闭流程 (从start finally触发)... ---")
            await self.shutdown()

    async def shutdown(self) -> None:
        logger.info("--- 正在执行 AIcarus Core 系统关闭流程 ---")
        self.stop_event.set()
        if self.core_logic_instance:
            await self.core_logic_instance.stop()

        if self.intrusive_thread and self.intrusive_thread.is_alive():
            self.intrusive_thread.join(timeout=10.0)
            if self.intrusive_thread.is_alive():
                logger.warning("侵入性思维线程超时未结束。")

        # 确保在关闭数据库连接之前，处理完所有需要数据库的清理工作
        if self.qq_chat_session_manager:
            logger.info("正在关闭 ChatSessionManager...")
            await self.qq_chat_session_manager.shutdown()
            logger.info("ChatSessionManager 已关闭。")

        if self.core_comm_layer:
            await self.core_comm_layer.stop()

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

        if self.conn_manager:
            await self.conn_manager.close_client()

        logger.info("AIcarus Core 系统关闭流程执行完毕。")


async def start_core_system() -> None:
    initializer = CoreSystemInitializer()
    try:
        await initializer.initialize()
        await initializer.start()
    except Exception as e:
        logger.critical(f"AIcarus Core 系统启动或运行遭遇致命错误: {e}", exc_info=True)
        # Ensure shutdown is called even if start() itself raises an unhandled error before its own finally block
        if not initializer.stop_event.is_set():  # Avoid double shutdown if start's finally already ran
            await initializer.shutdown()
    # No finally here, as start() has its own comprehensive finally for shutdown


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
