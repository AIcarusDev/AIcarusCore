# AIcarusCore/src/main.py
import asyncio
import json
import os
import threading
from typing import Any, Optional, TYPE_CHECKING

from src.action.action_handler import ActionHandler
from src.sub_consciousness.chat_session_manager import ChatSessionManager
from src.common.custom_logging.logger_manager import get_logger
from src.config import config
from src.core_communication.core_ws_server import AdapterEventCallback, CoreWebsocketServer
from src.core_logic.consciousness_flow import CoreLogic as CoreLogicFlow
from src.core_logic.intrusive_thoughts import IntrusiveThoughtsGenerator
from src.core_logic.unread_info_service import UnreadInfoService
from src.core_logic.summarization_service import SummarizationService
from src.core_logic.prompt_builder import ThoughtPromptBuilder
from src.core_logic.state_manager import AIStateManager # 确保导入 AIStateManager
# 导入新的服务类
from src.core_logic.context_builder import ContextBuilder
from src.core_logic.thought_generator import ThoughtGenerator
from src.core_logic.thought_persistor import ThoughtPersistor


from src.database.core.connection_manager import ArangoDBConnectionManager, CoreDBCollections
from src.database.services.action_log_storage_service import ActionLogStorageService
from src.database.services.conversation_storage_service import ConversationStorageService
from src.database.services.event_storage_service import EventStorageService
from src.database.services.thought_storage_service import ThoughtStorageService
from src.llmrequest.llm_processor import Client as ProcessorClient
from src.llmrequest.utils_model import GenerationParams
from src.message_processing.default_message_processor import DefaultMessageProcessor

if TYPE_CHECKING:
    pass

logger = get_logger("AIcarusCore.MainInitializer")

class CoreSystemInitializer:
    def __init__(self) -> None:
        self.logger = get_logger("AIcarusCore.MainInitializer")
        
        self.conn_manager: Optional[ArangoDBConnectionManager] = None
        self.event_storage_service: Optional[EventStorageService] = None
        self.conversation_storage_service: Optional[ConversationStorageService] = None
        self.thought_storage_service: Optional[ThoughtStorageService] = None
        self.action_log_service: Optional[ActionLogStorageService] = None

        self.main_consciousness_llm_client: Optional[ProcessorClient] = None
        self.summary_llm_client: Optional[ProcessorClient] = None
        self.intrusive_thoughts_llm_client: Optional[ProcessorClient] = None
        self.focused_chat_llm_client: Optional[ProcessorClient] = None
        # self.action_llm_client and self.embedding_llm_client seem unused by current logic, can be added if needed

        self.core_comm_layer: Optional[CoreWebsocketServer] = None
        self.message_processor: Optional[DefaultMessageProcessor] = None
        self.action_handler_instance: Optional[ActionHandler] = None
        self.intrusive_generator_instance: Optional[IntrusiveThoughtsGenerator] = None
        self.core_logic_instance: Optional[CoreLogicFlow] = None
        self.qq_chat_session_manager: Optional[ChatSessionManager] = None
        
        self.unread_info_service: Optional[UnreadInfoService] = None
        self.summarization_service: Optional[SummarizationService] = None
        self.state_manager_instance: Optional[AIStateManager] = None # AIStateManager instance
        self.thought_prompt_builder_instance: Optional[ThoughtPromptBuilder] = None
        self.context_builder_instance: Optional[ContextBuilder] = None
        self.thought_generator_instance: Optional[ThoughtGenerator] = None
        self.thought_persistor_instance: Optional[ThoughtPersistor] = None

        self.intrusive_thread: Optional[threading.Thread] = None
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
                if isinstance(keys_from_env, list): resolved_abandoned_keys = [str(k).strip() for k in keys_from_env if str(k).strip()]
            except json.JSONDecodeError:
                logger.warning(f"环境变量 'LLM_ABANDONED_KEYS' 非有效JSON列表: {env_val_abandoned[:50]}...")
                resolved_abandoned_keys = [k.strip() for k in env_val_abandoned.split(",") if k.strip()]
            if not resolved_abandoned_keys and env_val_abandoned.strip(): resolved_abandoned_keys = [env_val_abandoned.strip()]

        def _create_client(cfg: GenerationParams, purpose: str) -> Optional[ProcessorClient]:
            if not cfg or not cfg.provider or not cfg.model_name:
                logger.error(f"模型配置错误: 用途 '{purpose}' 未指定 provider 或 model_name。")
                return None
            try:
                args = {
                    "model": {"provider": cfg.provider.upper(), "name": cfg.model_name},
                    **vars(general_llm_settings_obj), 
                    **{k: v for k, v in vars(cfg).items() if v is not None and k not in ['provider', 'model_name']} # Add specific params
                }
                if resolved_abandoned_keys: args["abandoned_keys_config"] = resolved_abandoned_keys
                client = ProcessorClient(**{k:v for k,v in args.items() if v is not None})
                logger.info(f"为用途 '{purpose}' 创建 ProcessorClient 成功 (模型: {client.llm_client.model_name})。")
                return client
            except Exception as e:
                logger.error(f"为用途 '{purpose}' 创建LLM客户端失败: {e}", exc_info=True)
                return None

        if not config.llm_models: raise RuntimeError("[llm_models] 配置块缺失。")
        models = config.llm_models
        self.main_consciousness_llm_client = _create_client(models.main_consciousness, "main_consciousness")
        self.summary_llm_client = _create_client(models.information_summary, "information_summary")
        if config.intrusive_thoughts_module_settings.enabled:
            self.intrusive_thoughts_llm_client = _create_client(models.intrusive_thoughts, "intrusive_thoughts")
        if config.sub_consciousness.enabled:
            self.focused_chat_llm_client = _create_client(models.focused_chat, "focused_chat")
        
        if not self.main_consciousness_llm_client: raise RuntimeError("主意识LLM客户端初始化失败。")
        if config.sub_consciousness.enabled and not self.focused_chat_llm_client:
            raise RuntimeError("专注聊天LLM客户端已启用但初始化失败。")
        logger.info("LLM客户端初始化完毕。")


    async def _initialize_database_and_services(self) -> None:
        self.conn_manager = await ArangoDBConnectionManager.create_from_config(object(), core_collection_configs=CoreDBCollections.get_all_core_collection_configs())
        if not self.conn_manager or not self.conn_manager.db: raise RuntimeError("数据库连接管理器初始化失败。")
        logger.info(f"数据库连接管理器已为数据库 '{self.conn_manager.db.name}' 初始化。")

        services_to_init = {
            "event_storage_service": EventStorageService,
            "conversation_storage_service": ConversationStorageService,
            "thought_storage_service": ThoughtStorageService,
            "action_log_service": ActionLogStorageService,
        }
        for attr_name, service_class in services_to_init.items():
            instance = service_class(conn_manager=self.conn_manager)
            if hasattr(instance, "initialize_infrastructure"): await instance.initialize_infrastructure()
            setattr(self, attr_name, instance)
            logger.info(f"{service_class.__name__} 已初始化。")
        logger.info("所有核心数据存储服务均已初始化。")

    async def initialize(self) -> None:
        logger.info("=== AIcarus Core 系统开始核心组件初始化流程... ===")
        try:
            await self._initialize_llm_clients()
            await self._initialize_database_and_services()

            if not all([self.event_storage_service, self.conversation_storage_service, self.thought_storage_service, self.main_consciousness_llm_client]):
                raise RuntimeError("一个或多个基础服务未能初始化。")

            self.unread_info_service = UnreadInfoService(
                event_storage=self.event_storage_service,
                conversation_storage=self.conversation_storage_service
            )
            logger.info("UnreadInfoService 初始化成功。")

            self.thought_prompt_builder_instance = ThoughtPromptBuilder(unread_info_service=self.unread_info_service)
            logger.info("ThoughtPromptBuilder 初始化成功。")
            
            summary_llm = self.summary_llm_client or self.main_consciousness_llm_client
            if not summary_llm: raise RuntimeError("无可用LLM客户端初始化SummarizationService。")
            self.summarization_service = SummarizationService(llm_client=summary_llm)
            logger.info("SummarizationService 初始化成功。")
            
            self.action_handler_instance = ActionHandler()
            self.action_handler_instance.set_dependencies(
                thought_service=self.thought_storage_service,
                event_service=self.event_storage_service,
                action_log_service=self.action_log_service,
            )
            logger.info("ActionHandler 初始化并注入存储服务成功。")

            self.state_manager_instance = AIStateManager(thought_service=self.thought_storage_service) # 修正关键字参数名称
            logger.info("AIStateManager 初始化成功。")

            self.context_builder_instance = ContextBuilder(
                event_storage=self.event_storage_service,
                core_comm=self.core_comm_layer, # core_comm_layer 此时为 None，稍后回填
                state_manager=self.state_manager_instance 
            )
            logger.info("ContextBuilder 初始化成功。")

            self.thought_generator_instance = ThoughtGenerator(llm_client=self.main_consciousness_llm_client)
            logger.info("ThoughtGenerator 初始化成功。")

            self.thought_persistor_instance = ThoughtPersistor(thought_storage=self.thought_storage_service)
            logger.info("ThoughtPersistor 初始化成功。")

            if config.sub_consciousness.enabled:
                if self.focused_chat_llm_client and config.persona.qq_id and self.summarization_service and self.event_storage_service and self.action_handler_instance:
                    self.qq_chat_session_manager = ChatSessionManager( 
                        config=config.sub_consciousness,
                        llm_client=self.focused_chat_llm_client,
                        event_storage=self.event_storage_service,
                        action_handler=self.action_handler_instance,
                        bot_id=config.persona.qq_id,
                        summarization_service=self.summarization_service,
                        core_logic=None 
                    )
                    logger.info("ChatSessionManager 初始化完成。")
                else:
                    logger.warning("ChatSessionManager 依赖不足，无法初始化。")
                    self.qq_chat_session_manager = None
            else:
                self.qq_chat_session_manager = None
                logger.info("专注聊天子意识模块未启用。")
            
            self.message_processor = DefaultMessageProcessor(
                event_service=self.event_storage_service,
                conversation_service=self.conversation_storage_service,
                core_websocket_server=None, 
                qq_chat_session_manager=self.qq_chat_session_manager 
            )
            self.message_processor.core_initializer_ref = self
            logger.info("DefaultMessageProcessor 初始化成功。")

            self.core_comm_layer = CoreWebsocketServer(
                host=config.server.host, port=config.server.port,
                event_handler_callback=self.message_processor.process_event,
                event_storage_service=self.event_storage_service,
                action_handler_instance=self.action_handler_instance,
                db_instance=self.conn_manager.db if self.conn_manager else None,
            )
            logger.info(f"CoreWebsocketServer 准备在 ws://{config.server.host}:{config.server.port} 上监听。")
            
            if self.context_builder_instance: self.context_builder_instance.core_comm = self.core_comm_layer
            if self.message_processor: self.message_processor.core_comm_layer = self.core_comm_layer
            if self.action_handler_instance: self.action_handler_instance.core_communication_layer = self.core_comm_layer
            logger.info("CoreWebsocketServer 实例已回填到相关服务。")

            if config.intrusive_thoughts_module_settings.enabled:
                if self.intrusive_thoughts_llm_client and self.thought_storage_service:
                    self.intrusive_generator_instance = IntrusiveThoughtsGenerator(
                        llm_client=self.intrusive_thoughts_llm_client,
                        thought_storage_service=self.thought_storage_service,
                        stop_event=self.stop_event,
                    )
                    logger.info("IntrusiveThoughtsGenerator 初始化成功。")
                else: logger.warning("侵入性思维模块已启用但依赖不足。")
            else: logger.info("侵入性思维模块未启用。")

            if not all([
                self.core_comm_layer, self.action_handler_instance, self.state_manager_instance,
                self.qq_chat_session_manager if config.sub_consciousness.enabled else True, # 如果未启用则不检查
                self.context_builder_instance, self.thought_generator_instance, 
                self.thought_persistor_instance, self.thought_prompt_builder_instance
            ]):
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
                intrusive_generator_instance=self.intrusive_generator_instance
            )
            
            if self.qq_chat_session_manager and self.core_logic_instance:
                if hasattr(self.qq_chat_session_manager, 'set_core_logic'):
                    self.qq_chat_session_manager.set_core_logic(self.core_logic_instance)
                else: 
                    logger.warning("ChatSessionManager 缺少 set_core_logic 方法，尝试直接设置。")
                    self.qq_chat_session_manager.core_logic = self.core_logic_instance
            
            if self.action_handler_instance: self.action_handler_instance.set_thought_trigger(self.immediate_thought_trigger)
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
                if self.intrusive_thread: logger.info("侵入性思维后台线程已启动。")
            
            if self.core_comm_layer: all_tasks.append(asyncio.create_task(self.core_comm_layer.start(), name="CoreWSServer"))
            if self.core_logic_instance: all_tasks.append(await self.core_logic_instance.start_thinking_loop()) # This returns a task
            if self.qq_chat_session_manager and config.sub_consciousness.enabled: 
                all_tasks.append(asyncio.create_task(self.qq_chat_session_manager.run_periodic_deactivation_check(), name="ChatDeactivation"))

            if not all_tasks:
                self.logger.warning("没有核心异步任务启动。")
                return
            
            logger.info(f"已启动 {len(all_tasks)} 个核心异步任务。")
            done, pending = await asyncio.wait(all_tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task_name = task.get_name()
                if task.cancelled(): self.logger.info(f"任务 '{task_name}' 被取消。")
                elif task.exception():
                    exc = task.exception()
                    self.logger.critical(f"关键任务 '{task_name}' 异常终止: {exc!r}", exc_info=exc)
                    if exc: raise exc # Re-raise to trigger shutdown
                else: self.logger.info(f"任务 '{task_name}' 正常结束。")
            
            for task in pending:
                if not task.done(): task.cancel()
                try: await task
                except asyncio.CancelledError: self.logger.info(f"挂起任务 '{task.get_name()}' 已取消。")
                except Exception as e: self.logger.error(f"取消挂起任务 '{task.get_name()}' 时出错: {e}", exc_info=True)
                        
        except asyncio.CancelledError: self.logger.info("AIcarus Core 主启动流程被取消。")
        except Exception as e:
            self.logger.critical(f"AIcarus Core 系统运行期间发生严重错误: {e}", exc_info=True)
            raise
        finally:
            self.logger.info("--- AIcarus Core 系统正在进入关闭流程 (从start finally触发)... ---")
            await self.shutdown()

    async def shutdown(self) -> None:
        logger.info("--- 正在执行 AIcarus Core 系统关闭流程 ---")
        self.stop_event.set()
        if self.core_logic_instance: await self.core_logic_instance.stop()
        if self.intrusive_thread and self.intrusive_thread.is_alive():
            self.intrusive_thread.join(timeout=10.0)
            if self.intrusive_thread.is_alive(): logger.warning("侵入性思维线程超时未结束。")
        if self.core_comm_layer: await self.core_comm_layer.stop()
        if self.conn_manager: await self.conn_manager.close_client()
        
        llm_clients = [self.main_consciousness_llm_client, self.summary_llm_client, 
                       self.intrusive_thoughts_llm_client, self.focused_chat_llm_client]
        for client_wrapper in llm_clients:
            if client_wrapper and hasattr(client_wrapper.llm_client, "_close_session_if_any"):
                try: await client_wrapper.llm_client._close_session_if_any()
                except Exception as e: logger.warning(f"关闭LLM客户端会话时出错: {e}")
        logger.info("AIcarus Core 系统关闭流程执行完毕。")

async def start_core_system() -> None:
    initializer = CoreSystemInitializer()
    try:
        await initializer.initialize()
        await initializer.start()
    except Exception as e:
        logger.critical(f"AIcarus Core 系统启动或运行遭遇致命错误: {e}", exc_info=True)
        # Ensure shutdown is called even if start() itself raises an unhandled error before its own finally block
        if not initializer.stop_event.is_set(): # Avoid double shutdown if start's finally already ran
            await initializer.shutdown() 
    # No finally here, as start() has its own comprehensive finally for shutdown

if __name__ == "__main__":
    if os.name == "nt": asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try: asyncio.run(start_core_system())
    except KeyboardInterrupt: logger.info("AIcarus Core: 用户中断，正在退出...")
    except Exception as main_exc:
        logger.critical(f"AIcarus Core: 顶层执行异常: {main_exc}", exc_info=True)
    finally: logger.info("AIcarus Core: 程序最终执行完毕。")
