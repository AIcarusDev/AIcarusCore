# AIcarusCore/src/main.py
import asyncio
import threading
import os # 用于环境变量和路径操作
import json # 用于解析环境变量中的JSON字符串
from urllib.parse import urlparse # 用于解析代理URL
from typing import Any, Callable, Awaitable, Optional, Dict, List, Tuple # 确保导入所有需要的类型

# 核心组件导入
from src.action.action_handler import ActionHandler
from src.common.custom_logging.logger_manager import get_logger
from src.config.global_config import get_global_config, AlcarusRootConfig
from src.config.alcarus_configs import ModelParams # 从 alcarus_configs 导入 ModelParams
from src.core_communication.core_ws_server import CoreWebsocketServer, AdapterEventCallback
from src.core_logic.consciousness_flow import CoreLogic as CoreLogicFlow
from src.core_logic.intrusive_thoughts import IntrusiveThoughtsGenerator
from src.llmrequest.llm_processor import Client as ProcessorClient
from src.message_processing.default_message_processor import DefaultMessageProcessor

# 新的数据库服务层导入
from src.database.core.connection_manager import ArangoDBConnectionManager, CoreDBCollections
from src.database.services.event_storage_service import EventStorageService
from src.database.services.conversation_storage_service import ConversationStorageService
from src.database.services.thought_storage_service import ThoughtStorageService

# 通信协议导入
from aicarus_protocols import Event as ProtocolEvent # 明确这是协议层的Event
from websockets.server import WebSocketServerProtocol # WebSocket服务器协议类型

logger = get_logger("AIcarusCore.MainInitializer") # 主初始化器的日志记录器

class CoreSystemInitializer:
    """
    AIcarus Core 系统的核心初始化器。
    负责有序地配置、创建和连接所有系统组件。
    """

    def __init__(self):
        """初始化 CoreSystemInitializer 的各个组件为 None。"""
        self.logger = get_logger("AIcarusCore.MainInitializer") 
        self.root_cfg: Optional[AlcarusRootConfig] = None # 全局配置实例

        # 数据库相关组件
        self.conn_manager: Optional[ArangoDBConnectionManager] = None # 新的数据库连接管理器
        self.event_storage_service: Optional[EventStorageService] = None
        self.conversation_storage_service: Optional[ConversationStorageService] = None
        self.thought_storage_service: Optional[ThoughtStorageService] = None

        # LLM 客户端实例
        self.main_consciousness_llm_client: Optional[ProcessorClient] = None
        self.action_llm_client: Optional[ProcessorClient] = None
        self.summary_llm_client: Optional[ProcessorClient] = None
        self.intrusive_thoughts_llm_client: Optional[ProcessorClient] = None
        self.embedding_llm_client: Optional[ProcessorClient] = None # 用于嵌入模型的客户端

        # 通信和消息处理组件
        self.core_comm_layer: Optional[CoreWebsocketServer] = None
        self.message_processor: Optional[DefaultMessageProcessor] = None

        # 核心逻辑和功能模块实例
        self.action_handler_instance: Optional[ActionHandler] = None
        self.intrusive_generator_instance: Optional[IntrusiveThoughtsGenerator] = None
        self.core_logic_instance: Optional[CoreLogicFlow] = None

        # 控制后台任务的事件和线程
        self.intrusive_thread: Optional[threading.Thread] = None
        self.stop_event: threading.Event = threading.Event() # 用于优雅地停止所有后台循环
        self.immediate_thought_trigger: asyncio.Event = asyncio.Event()

        logger.info("CoreSystemInitializer 实例已创建，准备进行初始化。")

    async def _initialize_llm_clients(self) -> None:
        """根据全局配置，初始化所有需要的LLM客户端。
        新逻辑：从 self.root_cfg.llm_models 中读取每个模型用途的配置，
        并根据其内部指定的 'provider' 字段来创建客户端。
        """
        if not self.root_cfg:
            logger.critical("全局配置 (root_cfg) 未加载，无法初始化LLM客户端。")
            raise RuntimeError("Root config not loaded. Cannot initialize LLM clients.")

        logger.info("开始根据新的扁平化配置结构初始化所有LLM客户端...")
        general_llm_settings_obj = self.root_cfg.llm_client_settings
        proxy_settings_obj = self.root_cfg.proxy
        final_proxy_host: Optional[str] = None
        final_proxy_port: Optional[int] = None

        # 解析代理设置 (与之前逻辑相同)
        if proxy_settings_obj.use_proxy and proxy_settings_obj.http_proxy_url:
            try:
                parsed_url = urlparse(proxy_settings_obj.http_proxy_url)
                final_proxy_host = parsed_url.hostname
                final_proxy_port = parsed_url.port
                if not final_proxy_host or not final_proxy_port:
                    logger.warning(f"代理URL '{proxy_settings_obj.http_proxy_url}' 解析不完整，将不使用代理。")
                    final_proxy_host, final_proxy_port = None, None
            except Exception as e_parse_proxy:
                logger.warning(f"解析代理URL '{proxy_settings_obj.http_proxy_url}' 失败: {e_parse_proxy}。将不使用代理。")
                final_proxy_host, final_proxy_port = None, None

        # 解析废弃的API密钥配置 (与之前逻辑相同)
        resolved_abandoned_keys: Optional[List[str]] = None
        env_val_abandoned = os.getenv("LLM_ABANDONED_KEYS")
        if env_val_abandoned:
            try:
                keys_from_env = json.loads(env_val_abandoned)
                if isinstance(keys_from_env, list):
                    resolved_abandoned_keys = [str(k).strip() for k in keys_from_env if str(k).strip()]
            except json.JSONDecodeError:
                logger.warning(
                    f"环境变量 'LLM_ABANDONED_KEYS' 的值不是有效的JSON列表。尝试按逗号分割。值 (前50字符): {env_val_abandoned[:50]}..."
                )
                resolved_abandoned_keys = [k.strip() for k in env_val_abandoned.split(",") if k.strip()]
            if not resolved_abandoned_keys and env_val_abandoned.strip():
                resolved_abandoned_keys = [env_val_abandoned.strip()]
        
        # 内部辅助函数，用于根据 ModelParams 创建单个 ProcessorClient 实例 (与之前版本相同)
        def _create_client_from_model_params(
            model_params_cfg: ModelParams, 
            purpose_key_for_log: str
        ) -> Optional[ProcessorClient]:
            try:
                actual_provider_name_str: str = model_params_cfg.provider
                actual_model_api_name: str = model_params_cfg.model_name

                if not actual_provider_name_str or not actual_model_api_name:
                    logger.error(
                        f"配置错误：用途为 '{purpose_key_for_log}' 的模型 "
                        f"未明确指定 'provider' 或 'model_name' 字段。"
                    )
                    return None
                
                model_for_client_constructor: Dict[str, str] = {
                    "provider": actual_provider_name_str.upper(),
                    "name": actual_model_api_name,
                }
                model_specific_kwargs: Dict[str, Any] = {}
                if model_params_cfg.temperature is not None:
                    model_specific_kwargs["temperature"] = model_params_cfg.temperature
                if model_params_cfg.max_output_tokens is not None:
                    model_specific_kwargs["maxOutputTokens"] = model_params_cfg.max_output_tokens
                if model_params_cfg.top_p is not None:
                    model_specific_kwargs["top_p"] = model_params_cfg.top_p
                if model_params_cfg.top_k is not None:
                    model_specific_kwargs["top_k"] = model_params_cfg.top_k
                
                processor_constructor_args: Dict[str, Any] = {
                    "model": model_for_client_constructor,
                    "image_placeholder_tag": general_llm_settings_obj.image_placeholder_tag,
                    "stream_chunk_delay_seconds": general_llm_settings_obj.stream_chunk_delay_seconds,
                    "enable_image_compression": general_llm_settings_obj.enable_image_compression,
                    "image_compression_target_bytes": general_llm_settings_obj.image_compression_target_bytes,
                    "rate_limit_disable_duration_seconds": general_llm_settings_obj.rate_limit_disable_duration_seconds,
                    "proxy_host": final_proxy_host,
                    "proxy_port": final_proxy_port,
                    "abandoned_keys_config": resolved_abandoned_keys,
                    **model_specific_kwargs,
                }
                final_constructor_args = {k: v for k, v in processor_constructor_args.items() if v is not None}
                
                client_instance = ProcessorClient(**final_constructor_args)
                logger.info(
                    f"为用途 '{purpose_key_for_log}' 成功创建 ProcessorClient 实例 "
                    f"(模型: {client_instance.llm_client.model_name}, "
                    f"提供商: {client_instance.llm_client.provider})."
                )
                return client_instance
            except Exception as e:
                logger.error(
                    f"为用途 '{purpose_key_for_log}' (提供商 '{model_params_cfg.provider if model_params_cfg else '未知'}') 创建LLM客户端时发生未知错误: {e}。",
                    exc_info=True
                )
                return None

        # 开始初始化各个LLM客户端
        try:
            # 从 self.root_cfg.llm_models 获取 AllModelPurposesConfig 实例
            if not self.root_cfg.llm_models:
                logger.error("配置错误：[llm_models] 配置块缺失，无法初始化任何LLM客户端。")
                raise RuntimeError("[llm_models] 配置块缺失。")

            all_model_configs = self.root_cfg.llm_models

            model_purpose_map = {
                "main_consciousness": "main_consciousness_llm_client",
                "action_decision": "action_llm_client",
                "information_summary": "summary_llm_client",
                "embedding_default": "embedding_llm_client",
                "intrusive_thoughts": "intrusive_thoughts_llm_client"
            }

            for purpose_key, client_attr_name in model_purpose_map.items():
                model_params_cfg = getattr(all_model_configs, purpose_key, None)
                
                if model_params_cfg and isinstance(model_params_cfg, ModelParams):
                    if purpose_key == "intrusive_thoughts" and \
                       (not self.root_cfg.intrusive_thoughts_module_settings or \
                        not self.root_cfg.intrusive_thoughts_module_settings.enabled):
                        logger.info(f"侵入性思维模块未启用，跳过 '{purpose_key}' LLM客户端的创建。")
                        setattr(self, client_attr_name, None)
                        continue

                    client_instance = _create_client_from_model_params(model_params_cfg, purpose_key)
                    setattr(self, client_attr_name, client_instance)
                    
                    if not client_instance:
                        if purpose_key == "main_consciousness":
                            raise RuntimeError(f"核心组件 '{purpose_key}' 的LLM客户端初始化失败。")
                        else:
                            logger.warning(f"可选组件 '{purpose_key}' 的LLM客户端未能初始化。相关功能可能受限。")
                elif model_params_cfg is None:
                     logger.info(f"在 [llm_models] 中未找到用途为 '{purpose_key}' 的模型配置，跳过其客户端创建。")
                     setattr(self, client_attr_name, None)
                else:
                    logger.error(
                        f"配置错误：用途为 '{purpose_key}' 的模型配置类型不正确 (期望 ModelParams)，得到 {type(model_params_cfg)}。"
                        "将跳过此客户端的创建。"
                    )
                    setattr(self, client_attr_name, None)

            if not self.main_consciousness_llm_client:
                logger.critical("主意识LLM客户端未能成功初始化，这是一个核心依赖。系统可能无法正常运行。")

            logger.info("所有根据新的扁平化配置结构定义的LLM客户端已尝试初始化完毕。")

        except Exception as e_init_all_llms:
            logger.critical(f"在新的LLM客户端初始化过程中发生未预期的严重错误: {e_init_all_llms}", exc_info=True)
            raise RuntimeError(f"新的LLM客户端初始化因意外错误而失败: {e_init_all_llms}") from e_init_all_llms

    async def _initialize_database_and_services(self) -> None:
        """
        初始化数据库连接管理器 (ArangoDBConnectionManager) 及其管理的核心集合和索引，
        然后基于此连接管理器初始化所有核心的数据存储服务。
        """
        if not self.root_cfg: # 再次检查以防万一
            logger.critical("全局配置 (root_cfg) 未加载，无法初始化数据库连接和存储服务。")
            raise RuntimeError("Root config not loaded. Cannot initialize Database and Services.")
            
        # 从全局配置中获取数据库特定配置部分 (例如 AlcarusRootConfig.database)
        db_config_from_root = getattr(self.root_cfg, 'database', None)

        # 获取所有核心集合及其索引定义的配置
        all_core_collection_configs = CoreDBCollections.get_all_core_collection_configs()

        # 创建 ArangoDBConnectionManager 实例
        # 它会优先使用 db_config_from_root 中的配置，如果缺失则回退到环境变量
        self.conn_manager = await ArangoDBConnectionManager.create_from_config(
            db_config_from_root if db_config_from_root else object(), # 传递一个空对象如果配置不存在，让其完全依赖环境变量
            core_collection_configs=all_core_collection_configs
        )

        if not self.conn_manager or not self.conn_manager.db: # 检查连接管理器和内部数据库连接是否成功建立
            raise RuntimeError("ArangoDBConnectionManager 或其内部数据库连接未能成功初始化。")
        
        logger.info(f"数据库连接管理器已为数据库 '{self.conn_manager.db.name}' 初始化。")
        logger.info("核心集合及索引结构已由连接管理器在初始化时保障。")

        # 初始化各个存储服务，并注入连接管理器
        self.event_storage_service = EventStorageService(conn_manager=self.conn_manager)
        # 服务类内部的 initialize_infrastructure 方法用于确保特定于该服务的更细致的索引或结构（如果需要）
        # ConnectionManager 已经确保了集合的存在和来自 CoreDBCollections 的基础索引。
        if hasattr(self.event_storage_service, 'initialize_infrastructure') and \
           callable(self.event_storage_service.initialize_infrastructure):
            await self.event_storage_service.initialize_infrastructure()
        logger.info("EventStorageService 已初始化。")

        self.conversation_storage_service = ConversationStorageService(conn_manager=self.conn_manager)
        if hasattr(self.conversation_storage_service, 'initialize_infrastructure') and \
           callable(self.conversation_storage_service.initialize_infrastructure):
            await self.conversation_storage_service.initialize_infrastructure()
        logger.info("ConversationStorageService 已初始化。")

        self.thought_storage_service = ThoughtStorageService(conn_manager=self.conn_manager)
        if hasattr(self.thought_storage_service, 'initialize_infrastructure') and \
           callable(self.thought_storage_service.initialize_infrastructure):
            await self.thought_storage_service.initialize_infrastructure()
        logger.info("ThoughtStorageService 已初始化。")

        logger.info("所有核心数据存储服务均已初始化。")


    async def initialize(self) -> None:
        """
        初始化 AIcarus Core 系统中的所有核心组件。
        这是一个有序的过程，确保各组件在启动前都已正确配置和连接。
        """
        logger.info("=== AIcarus Core 系统开始核心组件初始化流程... ===")
        try:
            # 1. 加载全局配置
            self.root_cfg = get_global_config()
            if not self.root_cfg: # 再次确认配置加载成功
                 logger.critical("全局配置未能成功加载！系统无法继续初始化。")
                 raise RuntimeError("全局配置 (AlcarusRootConfig) 加载失败。")
            logger.info("全局配置已成功加载。")

            # 2. 初始化所有LLM客户端
            await self._initialize_llm_clients()

            # 3. 初始化数据库连接管理器和所有存储服务
            await self._initialize_database_and_services()

            # 4. 初始化消息处理器 (DefaultMessageProcessor)，并注入新的存储服务
            if not self.event_storage_service or not self.conversation_storage_service: # 检查依赖的服务是否已就绪
                logger.critical("核心存储服务 (EventStorageService或ConversationStorageService) 未初始化，无法创建消息处理器。")
                raise RuntimeError("核心存储服务未初始化，无法创建DefaultMessageProcessor。")
            self.message_processor = DefaultMessageProcessor(
                event_service=self.event_storage_service,
                conversation_service=self.conversation_storage_service,
                core_websocket_server=None # WebSocket服务器将在下一步创建并回填此引用
            )
            self.message_processor.core_initializer_ref = self # <-- 加上这行代码！
            self.logger.info("已将 CoreSystemInitializer 实例的引用注入到 DefaultMessageProcessor，这下可以触发思考了。")

            logger.info("DefaultMessageProcessor 已初始化并成功注入了新的存储服务。")

            # 5. 创建并设置 WebSocket 通信层 (CoreWebsocketServer)
            event_handler_for_ws: AdapterEventCallback # 类型提示
            if self.message_processor and \
               hasattr(self.message_processor, 'process_event') and \
               callable(self.message_processor.process_event):
                event_handler_for_ws = self.message_processor.process_event # 获取事件处理回调
            else: # 如果消息处理器或其方法无效
                logger.critical("DefaultMessageProcessor 或其 'process_event' 方法无效，无法设置WebSocket回调！")
                raise RuntimeError("DefaultMessageProcessor 或其 'process_event' 方法无效。")

            # 从配置中获取WebSocket服务器的host和port
            ws_host = self.root_cfg.server.host
            ws_port = self.root_cfg.server.port
            self.core_comm_layer = CoreWebsocketServer(
                host=ws_host,
                port=ws_port,
                event_handler_callback=event_handler_for_ws, # 设置回调
                db_instance=self.conn_manager.db if self.conn_manager else None # (可选)传递数据库实例给通信层
            )
            logger.info(f"核心 WebSocket 通信层 (CoreWebsocketServer) 准备在 ws://{ws_host}:{ws_port} 上监听。")
            
            # 将 WebSocket 服务器实例回填到消息处理器中，使其可以主动发送消息
            if self.message_processor: # 再次检查，确保 message_processor 实例存在
                self.message_processor.core_comm_layer = self.core_comm_layer
                logger.info("CoreWebsocketServer 实例已成功设置回 DefaultMessageProcessor，使其具备发送能力。")

            # 6. 初始化动作处理器 (ActionHandler)
            self.action_handler_instance = ActionHandler() 
            if self.action_handler_instance:
                # 确保所有需要的服务都已初始化
                if not self.thought_storage_service or not self.event_storage_service: # 检查新服务是否就位
                    logger.critical("ThoughtStorageService 或 EventStorageService 未初始化，无法正确设置 ActionHandler 依赖。小猫咪要闹情绪了！")
                    raise RuntimeError("核心存储服务未初始化，无法设置 ActionHandler 依赖。")
                
                # 亲爱的，看这里！ActionHandler现在不需要直接喂食event_service了哦！
                self.action_handler_instance.set_dependencies(
                    thought_service=self.thought_storage_service, # 这是 ThoughtStorageService
                    comm_layer=self.core_comm_layer
                    # event_service 参数已移除
                )
                logger.info(
                    "ActionHandler 已初始化并成功注入了新的存储服务 (ThoughtStorageService, EventStorageService) 和通信层。"
                    " 其LLM客户端将按需加载，准备好大干一场了！"
                )

            # 7. 初始化侵入性思维生成器 (IntrusiveThoughtsGenerator)
            intrusive_settings = self.root_cfg.intrusive_thoughts_module_settings
            persona_settings = self.root_cfg.persona
            if intrusive_settings.enabled: # 仅当模块在配置中启用时才初始化
                if self.intrusive_thoughts_llm_client and self.thought_storage_service: # 检查依赖是否就绪
                    self.intrusive_generator_instance = IntrusiveThoughtsGenerator(
                        llm_client=self.intrusive_thoughts_llm_client,
                        thought_storage_service=self.thought_storage_service, # 注入新的ThoughtStorageService
                        persona_cfg=persona_settings,
                        module_settings=intrusive_settings,
                        stop_event=self.stop_event, # 传递全局停止事件
                    )
                    logger.info("侵入性思维生成器 (IntrusiveThoughtsGenerator) 已成功初始化。")
                else: # 如果依赖未满足
                    missing_deps_itg = []
                    if not self.intrusive_thoughts_llm_client: missing_deps_itg.append("侵入性思维LLM客户端")
                    if not self.thought_storage_service: missing_deps_itg.append("ThoughtStorageService")
                    logger.warning(
                        f"侵入性思维模块已在配置中启用，但其核心依赖 ({', '.join(missing_deps_itg)}) 未能成功初始化。"
                        f"该模块将无法正常工作。"
                    )
            else: # 如果模块在配置中未启用
                self.intrusive_generator_instance = None # 明确设置为None
                logger.info("侵入性思维模块在配置中未启用，跳过其初始化。")

            # 8. 初始化核心逻辑流 (CoreLogicFlow)
            # 检查 CoreLogicFlow 所需的所有核心服务和配置是否都已成功初始化
            if not all([
                self.root_cfg,
                self.main_consciousness_llm_client,
                self.core_comm_layer,
                self.action_handler_instance, # 即使其DB部分待重构，实例本身应存在
                self.event_storage_service,     # CoreLogicFlow 需要它来获取上下文聊天记录
                self.conversation_storage_service, # CoreLogicFlow 可能需要它来获取会话的注意力档案
                self.thought_storage_service   # CoreLogicFlow 需要它来保存/读取思考，并更新动作状态
            ]):
                # 构建缺失依赖的列表，用于清晰地报错
                missing_core_logic_deps = [
                    item_name for item_name, status in {
                        "全局配置 (RootConfig)": self.root_cfg,
                        "主意识LLM客户端": self.main_consciousness_llm_client,
                        "核心通信层": self.core_comm_layer,
                        "动作处理器": self.action_handler_instance,
                        "事件存储服务": self.event_storage_service,
                        "会话存储服务": self.conversation_storage_service,
                        "思考存储服务": self.thought_storage_service
                    }.items() if not status
                ]
                error_message = f"核心逻辑流 (CoreLogicFlow) 初始化失败：核心依赖缺失 - {', '.join(missing_core_logic_deps)}。"
                logger.critical(error_message)
                raise RuntimeError(error_message)

            self.core_logic_instance = CoreLogicFlow(
                root_cfg=self.root_cfg,
                event_storage_service=self.event_storage_service,
                conversation_storage_service=self.conversation_storage_service,
                thought_storage_service=self.thought_storage_service,
                main_consciousness_llm_client=self.main_consciousness_llm_client,
                intrusive_thoughts_llm_client=self.intrusive_thoughts_llm_client, # 可能为None
                core_comm_layer=self.core_comm_layer,
                action_handler_instance=self.action_handler_instance,
                intrusive_generator_instance=self.intrusive_generator_instance, # 可能为None
                stop_event=self.stop_event, # 传递全局停止事件
                immediate_thought_trigger=self.immediate_thought_trigger
            )
            if self.action_handler_instance:
                self.action_handler_instance.set_thought_trigger(self.immediate_thought_trigger)
                logger.info("已尝试为 ActionHandler 设置主思维触发器。") # 添加日志方便确认

            logger.info("核心逻辑流 (CoreLogicFlow) 已成功初始化并注入了新的存储服务和触发器。")

            logger.info("=== AIcarus Core 系统所有核心组件初始化完毕！ ===")

        except Exception as e: # 捕获初始化过程中发生的任何其他异常
            logger.critical(f"AIcarus Core 系统初始化过程中发生严重错误: {e}", exc_info=True)
            await self.shutdown() # 尝试在初始化失败时也执行关闭清理流程
            raise # 将异常向上抛出，以便主程序 (run_core_logic.py) 可以捕获并记录

    async def start(self) -> None:
        """启动核心系统的所有后台服务和主循环。"""
        if not self.core_logic_instance or not self.core_comm_layer:
            logger.critical("核心组件 (CoreLogic 或 CoreCommLayer) 未完全初始化，系统无法启动。")
            return

        server_task: Optional[asyncio.Task] = None        # WebSocket 服务器任务
        thinking_loop_task: Optional[asyncio.Task] = None # 主意识思考循环任务

        try:
            # 启动侵入性思维的后台生成线程 (如果已启用且初始化成功)
            if self.intrusive_generator_instance and \
               self.root_cfg and self.root_cfg.intrusive_thoughts_module_settings.enabled:
                # start_background_generation 方法返回一个 threading.Thread 实例
                self.intrusive_thread = self.intrusive_generator_instance.start_background_generation()
                if self.intrusive_thread:
                    logger.info("侵入性思维后台生成线程已启动。")
                else:
                    # 如果 start_background_generation 返回 None (例如因依赖未满足而未启动)
                    logger.warning("侵入性思维后台生成线程未能启动 (可能由于其内部依赖未满足或模块已在配置中禁用)。")
            
            # 启动核心 WebSocket 服务器的异步任务
            if self.core_comm_layer: # 确保实例存在
                server_task = asyncio.create_task(self.core_comm_layer.start(), name="CoreWebSocketServerTask")
                logger.info("核心 WebSocket 服务器的异步任务已启动，开始监听连接。")
            
            # 启动核心逻辑大脑的思考循环异步任务
            if self.core_logic_instance: # 确保实例存在
                thinking_loop_task = await self.core_logic_instance.start_thinking_loop()
                # asyncio.Task 对象在创建时可以通过 name 参数命名，这里是返回后，故不能用 set_name
                logger.info("核心逻辑大脑的思考循环异步任务已启动。")

            # 收集需要等待的核心异步任务
            tasks_to_wait = [t for t in [server_task, thinking_loop_task] if t is not None]
            if not tasks_to_wait: # 如果没有核心异步任务在运行
                logger.warning("没有核心异步任务 (WebSocket服务器或思考循环) 被成功启动。系统可能不会执行其主要功能。")
                return # 如果没有任务可等待，则提前返回，否则 asyncio.wait 会报错

            # 等待任一关键任务首先完成 (通常意味着异常终止或正常停止信号)
            done, pending = await asyncio.wait(
                tasks_to_wait,
                return_when=asyncio.FIRST_COMPLETED # 当任何一个任务结束时即返回
            )

            # 处理已完成的任务（通常是检查是否有异常）
            for task in done:
                task_name = task.get_name() if hasattr(task, 'get_name') and task.get_name() else "一个已完成的关键任务"
                if task.cancelled():
                    logger.info(f"任务 '{task_name}' 被取消。")
                elif task.exception(): # 如果任务是因异常而结束
                    exc = task.exception()
                    logger.critical(
                        f"关键任务 '{task_name}' 因未捕获的异常而意外终止: {exc!r}", exc_info=exc # 记录完整异常信息
                    )
                    if exc: raise exc # 将异常传播出去，以便主程序 (run_core_logic.py) 可以捕获
                else: # 如果任务正常结束 (例如，服务器被明确停止)
                    logger.info(f"任务 '{task_name}' 已正常结束。")
            
            # 在一个关键任务结束后，请求取消其他仍在运行的挂起任务，以实现优雅关闭
            for task in pending:
                task_name = task.get_name() if hasattr(task, 'get_name') and task.get_name() else "一个挂起的关键任务"
                self.logger.info(f"一个关键任务已结束，正在请求取消其他仍在运行的挂起任务 '{task_name}'...")
                if not task.done(): # 再次检查，确保任务在等待期间没有自行结束
                    task.cancel() # 发送取消请求
                    try:
                        await task # 等待任务实际响应取消并结束
                    except asyncio.CancelledError: # 这是预期的异常，表明任务成功取消
                        logger.info(f"挂起的任务 '{task_name}' 已成功响应取消请求并结束。")
                    except Exception as e_cancel: # 捕获取消过程中可能发生的其他异常
                        logger.error(f"尝试取消挂起任务 '{task_name}' 时发生意外错误: {e_cancel}", exc_info=True)

        except asyncio.CancelledError: # 如果 start() 方法本身被取消
            logger.info("AIcarus Core 主启动流程 (start 方法) 被外部取消。")
        except Exception as e: # 捕获在 start 方法执行过程中发生的其他所有未预期错误
            logger.critical(f"AIcarus Core 系统在启动或运行期间发生未处理的严重错误: {e}", exc_info=True)
            raise # 将异常传播出去，以便顶层捕获
        finally:
            # 无论 start 方法如何结束（正常、异常或取消），都执行关闭流程
            logger.info("--- AIcarus Core 系统正在进入关闭流程 (从 start 方法的 finally 块触发)... ---")
            await self.shutdown() # 确保在任何情况下都尝试进行优雅关闭

    async def shutdown(self) -> None:
        """优雅地关闭所有已初始化的核心组件和服务。"""
        logger.info("--- 正在执行 AIcarus Core 系统的关闭流程 ---")
        self.stop_event.set() # 设置全局停止事件，通知所有后台循环和线程应准备退出

        # 1. 停止核心逻辑的思考循环 (它应响应 stop_event)
        if self.core_logic_instance:
            logger.info("正在请求停止核心逻辑大脑的思考循环...")
            await self.core_logic_instance.stop() # stop 方法内部应处理其异步任务的取消和等待
            logger.info("核心逻辑大脑的思考循环已处理停止请求。")

        # 2. 停止侵入性思维的后台线程 (它应响应 stop_event)
        if self.intrusive_thread is not None and self.intrusive_thread.is_alive():
            logger.info("正在等待侵入性思维后台生成线程结束 (超时设置10秒)...")
            self.intrusive_thread.join(timeout=10.0) # 等待线程自然结束，设置超时
            if self.intrusive_thread.is_alive(): # 如果超时后线程仍在运行
                logger.warning("警告：侵入性思维后台线程在10秒超时后仍未结束。可能需要强制处理或检查其循环逻辑。")
            else:
                logger.info("侵入性思维后台生成线程已成功结束。")
        
        # 3. 停止 WebSocket 通信层 (它应响应 stop_event 并关闭所有连接)
        if self.core_comm_layer:
            logger.info("正在请求停止核心 WebSocket 通信层...")
            await self.core_comm_layer.stop() # stop 方法内部应处理其服务器的关闭和连接的断开
            logger.info("核心 WebSocket 通信层已处理停止请求。")
        
        # 4. 关闭 ActionHandler (如果它内部有需要显式清理的异步资源或任务)
        # 目前 ActionHandler 主要是按需调用，没有常驻的异步循环，但如果未来添加，可以在此处理。
        # if self.action_handler_instance and hasattr(self.action_handler_instance, 'stop_async_resources'):
        #     logger.info("正在请求停止动作处理器的异步资源...")
        #     await self.action_handler_instance.stop_async_resources() # 假设有这样一个异步的清理方法
        #     logger.info("动作处理器的异步资源已处理停止请求。")

        # 5. 关闭数据库连接管理器 (它会关闭底层的 ArangoClient)
        if self.conn_manager: # 使用新的数据库连接管理器实例
            logger.info("正在关闭数据库连接管理器...")
            await self.conn_manager.close_client() # 调用其关闭客户端连接的方法
            logger.info("数据库连接管理器及其底层连接已关闭。")
        
        # 6. 关闭 LLM 客户端 (如果它们内部持有一些需要显式异步关闭的资源，例如 aiohttp.ClientSession)
        # ProcessorClient 和其内部的 UnderlyingLLMClient 目前主要依赖 aiohttp.ClientSession 的上下文管理
        # 或在 UnderlyingLLMClient 的 __del__ 中尝试关闭。如果未来添加显式的异步 close 方法，应在此处调用。
        llm_clients_to_close: List[Optional[ProcessorClient]] = [ # 类型提示
            self.main_consciousness_llm_client,
            self.action_llm_client,
            self.summary_llm_client,
            self.intrusive_thoughts_llm_client,
            self.embedding_llm_client
        ]
        for llm_client_wrapper in llm_clients_to_close:
            if llm_client_wrapper and \
               hasattr(llm_client_wrapper.llm_client, '_close_session_if_any') and \
               callable(llm_client_wrapper.llm_client._close_session_if_any): # 检查底层客户端是否有清理方法
                try:
                    logger.info(
                        f"尝试关闭 LLM 客户端 "
                        f"({llm_client_wrapper.llm_client.provider} - {llm_client_wrapper.llm_client.model_name}) "
                        f"的底层 aiohttp 会话 (如果存在)..."
                    )
                    # _close_session_if_any 应该是一个异步方法
                    await llm_client_wrapper.llm_client._close_session_if_any() # type: ignore
                except Exception as e_llm_close:
                    logger.warning(f"关闭 LLM 客户端的底层会话时出错: {e_llm_close}")

        logger.info("AIcarus Core 系统所有组件的关闭流程已执行完毕。")

# 主启动函数 start_core_system 和 __main__ 部分保持不变，以便 run_core_logic.py 可以正确调用
async def start_core_system() -> None:
    """主异步函数，用于初始化并启动 AIcarus Core 系统。"""
    initializer = CoreSystemInitializer() # 创建初始化器实例
    try:
        await initializer.initialize() # 初始化所有核心组件
        await initializer.start()      # 启动所有核心服务和后台循环
    except Exception as e: # 捕获在初始化或启动过程中发生的任何严重错误
        logger.critical(f"AIcarus Core 系统启动或运行过程中遭遇致命错误: {e}", exc_info=True)
        # 即使在 initialize 或 start 中发生错误并已调用过 shutdown，
        # 这里的再次调用 shutdown 应该是安全的（幂等的）。
        await initializer.shutdown() # 确保在任何严重错误后都尝试进行优雅关闭
    # finally 块移到调用方 (例如 run_core_logic.py 中的 asyncio.run 之外)，
    # 以便更好地处理 KeyboardInterrupt 等顶层退出信号。

if __name__ == "__main__":
    # 为 Windows 系统配置事件循环策略，以避免一些常见问题
    if os.name == 'nt': # 如果是 Windows 操作系统
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    try:
        asyncio.run(start_core_system()) # 运行主异步函数
    except KeyboardInterrupt: # 捕获用户通过 Ctrl+C 发送的中断信号
        logger.info("AIcarus Core (main.py __main__): 检测到用户中断 (KeyboardInterrupt)，程序正在准备退出...")
        # asyncio.run() 在捕获 KeyboardInterrupt 后会自动尝试取消所有正在运行的任务。
        # 我们的 shutdown 逻辑应该会在任务被取消时或在 start_core_system 的 finally 块中被触发。
    except Exception as main_execution_exc: # 捕获其他所有未在 start_core_system 中处理的顶层异常
        logger.critical(
            f"AIcarus Core (main.py __main__): 顶层执行过程中发生未捕获的严重异常: {main_execution_exc}",
            exc_info=True
        )
    finally:
        # 这个 finally 块确保无论如何，程序结束时都会打印这条信息。
        # 实际的资源清理应在 start_core_system -> initializer.shutdown() 中完成。
        logger.info("AIcarus Core (main.py __main__): 程序最终执行流程结束。")