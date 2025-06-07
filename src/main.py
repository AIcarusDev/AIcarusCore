# AIcarusCore/src/main.py
import asyncio
import json  # 用于解析环境变量中的JSON字符串
import os  # 用于环境变量和路径操作
import threading
from typing import Any

# 通信协议导入
# 核心组件导入
from src.action.action_handler import ActionHandler
from src.common.custom_logging.logger_manager import get_logger
from src.config import config
from src.core_communication.core_ws_server import AdapterEventCallback, CoreWebsocketServer
from src.core_logic.consciousness_flow import CoreLogic as CoreLogicFlow
from src.core_logic.intrusive_thoughts import IntrusiveThoughtsGenerator

# 新的数据库服务层导入
from src.database.core.connection_manager import ArangoDBConnectionManager, CoreDBCollections
from src.database.services.action_log_storage_service import ActionLogStorageService  # 主人，把新的小女仆也请进来！
from src.database.services.conversation_storage_service import ConversationStorageService
from src.database.services.event_storage_service import EventStorageService
from src.database.services.thought_storage_service import ThoughtStorageService
from src.llmrequest.llm_processor import Client as ProcessorClient
from src.llmrequest.utils_model import GenerationParams
from src.message_processing.default_message_processor import DefaultMessageProcessor

logger = get_logger("AIcarusCore.MainInitializer")  # 主初始化器的日志记录器


class CoreSystemInitializer:
    """
    AIcarus Core 系统的核心初始化器。
    负责有序地配置、创建和连接所有系统组件。
    """

    def __init__(self) -> None:
        """初始化 CoreSystemInitializer 的各个组件为 None。"""
        self.logger = get_logger("AIcarusCore.MainInitializer")
        # 现在使用全局配置对象，不再需要实例变量

        # 数据库相关组件
        self.conn_manager: ArangoDBConnectionManager | None = None  # 新的数据库连接管理器
        self.event_storage_service: EventStorageService | None = None
        self.conversation_storage_service: ConversationStorageService | None = None
        self.thought_storage_service: ThoughtStorageService | None = None
        self.action_log_service: ActionLogStorageService | None = None  # 新增 ActionLog 服务

        # LLM 客户端实例
        self.main_consciousness_llm_client: ProcessorClient | None = None
        self.action_llm_client: ProcessorClient | None = None
        self.summary_llm_client: ProcessorClient | None = None
        self.intrusive_thoughts_llm_client: ProcessorClient | None = None
        self.embedding_llm_client: ProcessorClient | None = None  # 用于嵌入模型的客户端

        # 通信和消息处理组件
        self.core_comm_layer: CoreWebsocketServer | None = None
        self.message_processor: DefaultMessageProcessor | None = None

        # 核心逻辑和功能模块实例
        self.action_handler_instance: ActionHandler | None = None
        self.intrusive_generator_instance: IntrusiveThoughtsGenerator | None = None
        self.core_logic_instance: CoreLogicFlow | None = None

        # 控制后台任务的事件和线程
        self.intrusive_thread: threading.Thread | None = None
        self.stop_event: threading.Event = threading.Event()  # 用于优雅地停止所有后台循环
        self.immediate_thought_trigger: asyncio.Event = asyncio.Event()

        logger.info("CoreSystemInitializer 实例已创建，准备进行初始化。")

    async def _initialize_llm_clients(self) -> None:
        """根据全局配置，初始化所有需要的LLM客户端。"""
        logger.info("开始根据新的扁平化配置结构初始化所有LLM客户端...")
        general_llm_settings_obj = config.llm_client_settings
        resolved_abandoned_keys: list[str] | None = None
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

        def _create_client_from_model_params(
            model_params_cfg: GenerationParams, purpose_key_for_log: str
        ) -> ProcessorClient | None:
            try:
                actual_provider_name_str: str = model_params_cfg.provider
                actual_model_api_name: str = model_params_cfg.model_name
                if not actual_provider_name_str or not actual_model_api_name:
                    logger.error(
                        f"配置错误：用途为 '{purpose_key_for_log}' 的模型 未明确指定 'provider' 或 'model_name' 字段。"
                    )
                    return None
                model_for_client_constructor: dict[str, str] = {
                    "provider": actual_provider_name_str.upper(),
                    "name": actual_model_api_name,
                }
                model_specific_kwargs: dict[str, Any] = {}
                if model_params_cfg.temperature is not None:
                    model_specific_kwargs["temperature"] = model_params_cfg.temperature
                if model_params_cfg.max_output_tokens is not None:
                    model_specific_kwargs["maxOutputTokens"] = model_params_cfg.max_output_tokens
                if model_params_cfg.top_p is not None:
                    model_specific_kwargs["top_p"] = model_params_cfg.top_p
                if model_params_cfg.top_k is not None:
                    model_specific_kwargs["top_k"] = model_params_cfg.top_k
                processor_constructor_args: dict[str, Any] = {
                    "model": model_for_client_constructor,
                    "image_placeholder_tag": general_llm_settings_obj.image_placeholder_tag,
                    "stream_chunk_delay_seconds": general_llm_settings_obj.stream_chunk_delay_seconds,
                    "enable_image_compression": general_llm_settings_obj.enable_image_compression,
                    "image_compression_target_bytes": general_llm_settings_obj.image_compression_target_bytes,
                    "rate_limit_disable_duration_seconds": general_llm_settings_obj.rate_limit_disable_duration_seconds,
                    "abandoned_keys_config": resolved_abandoned_keys,
                    **model_specific_kwargs,
                }
                final_constructor_args = {k: v for k, v in processor_constructor_args.items() if v is not None}
                client_instance = ProcessorClient(**final_constructor_args)
                logger.info(
                    f"为用途 '{purpose_key_for_log}' 成功创建 ProcessorClient 实例 (模型: {client_instance.llm_client.model_name}, 提供商: {client_instance.llm_client.provider})."
                )
                return client_instance
            except Exception as e:
                logger.error(
                    f"为用途 '{purpose_key_for_log}' (提供商 '{model_params_cfg.provider if model_params_cfg else '未知'}') 创建LLM客户端时发生未知错误: {e}。",
                    exc_info=True,
                )
                return None

        try:
            if not config.llm_models:
                logger.error("配置错误：[llm_models] 配置块缺失，无法初始化任何LLM客户端。")
                raise RuntimeError("[llm_models] 配置块缺失。")
            all_model_configs = config.llm_models
            model_purpose_map = {
                "main_consciousness": "main_consciousness_llm_client",
                "action_decision": "action_llm_client",
                "information_summary": "summary_llm_client",
                "embedding_default": "embedding_llm_client",
                "intrusive_thoughts": "intrusive_thoughts_llm_client",
            }
            for purpose_key, client_attr_name in model_purpose_map.items():
                model_params_cfg = getattr(all_model_configs, purpose_key, None)
                if (
                    model_params_cfg
                    and hasattr(model_params_cfg, "provider")
                    and hasattr(model_params_cfg, "model_name")
                ):
                    if purpose_key == "intrusive_thoughts" and (
                        not config.intrusive_thoughts_module_settings
                        or not config.intrusive_thoughts_module_settings.enabled
                    ):
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
                        f"配置错误：用途为 '{purpose_key}' 的模型配置类型不正确 (期望有效的模型参数配置)，得到 {type(model_params_cfg)}。将跳过此客户端的创建。"
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
        all_core_collection_configs = CoreDBCollections.get_all_core_collection_configs()
        self.conn_manager = await ArangoDBConnectionManager.create_from_config(
            object(), core_collection_configs=all_core_collection_configs
        )
        if not self.conn_manager or not self.conn_manager.db:
            raise RuntimeError("ArangoDBConnectionManager 或其内部数据库连接未能成功初始化。")
        logger.info(f"数据库连接管理器已为数据库 '{self.conn_manager.db.name}' 初始化。")
        logger.info("核心集合及索引结构已由连接管理器在初始化时保障。")

        self.event_storage_service = EventStorageService(conn_manager=self.conn_manager)
        if hasattr(self.event_storage_service, "initialize_infrastructure") and callable(
            self.event_storage_service.initialize_infrastructure
        ):
            await self.event_storage_service.initialize_infrastructure()
        logger.info("EventStorageService 已初始化。")

        self.conversation_storage_service = ConversationStorageService(conn_manager=self.conn_manager)
        if hasattr(self.conversation_storage_service, "initialize_infrastructure") and callable(
            self.conversation_storage_service.initialize_infrastructure
        ):
            await self.conversation_storage_service.initialize_infrastructure()
        logger.info("ConversationStorageService 已初始化。")

        self.thought_storage_service = ThoughtStorageService(conn_manager=self.conn_manager)
        if hasattr(self.thought_storage_service, "initialize_infrastructure") and callable(
            self.thought_storage_service.initialize_infrastructure
        ):
            await self.thought_storage_service.initialize_infrastructure()
        logger.info("ThoughtStorageService 已初始化。")

        self.action_log_service = ActionLogStorageService(conn_manager=self.conn_manager)  # 初始化 ActionLog 服务
        if hasattr(self.action_log_service, "initialize_infrastructure") and callable(
            self.action_log_service.initialize_infrastructure
        ):
            # ActionLogStorageService 目前没有 initialize_infrastructure，但保留以备将来扩展
            await self.action_log_service.initialize_infrastructure()
        logger.info("ActionLogStorageService 已初始化。")

        logger.info("所有核心数据存储服务均已初始化。")

    async def initialize(self) -> None:
        """
        初始化 AIcarus Core 系统中的所有核心组件。
        这是一个有序的过程，确保各组件在启动前都已正确配置和连接。
        """
        logger.info("=== AIcarus Core 系统开始核心组件初始化流程... ===")
        try:
            logger.info("使用全局配置对象。")
            await self._initialize_llm_clients()
            await self._initialize_database_and_services()

            if not self.event_storage_service or not self.conversation_storage_service:
                logger.critical(
                    "核心存储服务 (EventStorageService或ConversationStorageService) 未初始化，无法创建消息处理器。"
                )
                raise RuntimeError("核心存储服务未初始化，无法创建DefaultMessageProcessor。")
            self.message_processor = DefaultMessageProcessor(
                event_service=self.event_storage_service,
                conversation_service=self.conversation_storage_service,
                core_websocket_server=None,
            )
            self.message_processor.core_initializer_ref = self
            self.logger.info(
                "已将 CoreSystemInitializer 实例的引用注入到 DefaultMessageProcessor，这下可以触发思考了。"
            )
            logger.info("DefaultMessageProcessor 已初始化并成功注入了新的存储服务。")

            self.action_handler_instance = ActionHandler()
            if not self.action_handler_instance:
                logger.critical("ActionHandler 实例未能创建！这不应该发生。")
                raise RuntimeError("ActionHandler 实例未能创建。")

            if (
                not self.thought_storage_service or not self.event_storage_service or not self.action_log_service
            ):  # 检查新增的 action_log_service
                missing_deps_msg = []
                if not self.thought_storage_service:
                    missing_deps_msg.append("ThoughtStorageService")
                if not self.event_storage_service:
                    missing_deps_msg.append("EventStorageService")
                if not self.action_log_service:
                    missing_deps_msg.append("ActionLogStorageService")
                logger.critical(
                    f"核心存储服务 ({', '.join(missing_deps_msg)}) 未初始化，无法正确设置 ActionHandler 依赖。小猫咪要闹情绪了！"
                )
                raise RuntimeError("核心存储服务未初始化，无法设置 ActionHandler 依赖。")

            self.action_handler_instance.set_dependencies(
                thought_service=self.thought_storage_service,
                event_service=self.event_storage_service,
                action_log_service=self.action_log_service,  # 注入 ActionLog 服务
                comm_layer=None,
            )
            logger.info(
                "ActionHandler 已初始化并成功注入了 ThoughtStorageService, EventStorageService 和 ActionLogStorageService。"
                " 其LLM客户端将按需加载，通信层将在 WebSocket 服务器启动后设置。"
            )

            event_handler_for_ws: AdapterEventCallback
            if (
                self.message_processor
                and hasattr(self.message_processor, "process_event")
                and callable(self.message_processor.process_event)
            ):
                event_handler_for_ws = self.message_processor.process_event
            else:
                logger.critical("DefaultMessageProcessor 或其 'process_event' 方法无效，无法设置WebSocket回调！")
                raise RuntimeError("DefaultMessageProcessor 或其 'process_event' 方法无效。")
            ws_host = config.server.host
            ws_port = config.server.port

            if not self.event_storage_service:
                logger.critical("EventStorageService 未初始化，无法创建 CoreWebsocketServer！这通常不应该发生。")
                raise RuntimeError("EventStorageService 未初始化，无法创建 CoreWebsocketServer。")

            self.core_comm_layer = CoreWebsocketServer(
                host=ws_host,
                port=ws_port,
                event_handler_callback=event_handler_for_ws,
                event_storage_service=self.event_storage_service,
                action_handler_instance=self.action_handler_instance,
                db_instance=self.conn_manager.db if self.conn_manager else None,
            )
            logger.info(
                f"核心 WebSocket 通信层 (CoreWebsocketServer) 准备在 ws://{ws_host}:{ws_port} 上监听，并已关联 ActionHandler。"
            )

            if self.message_processor:
                self.message_processor.core_comm_layer = self.core_comm_layer
                logger.info("CoreWebsocketServer 实例已成功设置回 DefaultMessageProcessor。")

            if self.action_handler_instance:
                self.action_handler_instance.core_communication_layer = self.core_comm_layer
                logger.info("CoreWebsocketServer 实例已成功设置回 ActionHandler 的通信层。")

            intrusive_settings = config.intrusive_thoughts_module_settings
            if intrusive_settings.enabled:
                if self.intrusive_thoughts_llm_client and self.thought_storage_service:
                    self.intrusive_generator_instance = IntrusiveThoughtsGenerator(
                        llm_client=self.intrusive_thoughts_llm_client,
                        thought_storage_service=self.thought_storage_service,
                        stop_event=self.stop_event,
                    )
                    logger.info("侵入性思维生成器 (IntrusiveThoughtsGenerator) 已成功初始化。")
                else:
                    missing_deps_itg = []
                    if not self.intrusive_thoughts_llm_client:
                        missing_deps_itg.append("侵入性思维LLM客户端")
                    if not self.thought_storage_service:
                        missing_deps_itg.append("ThoughtStorageService")
                    logger.warning(
                        f"侵入性思维模块已在配置中启用，但其核心依赖 ({', '.join(missing_deps_itg)}) 未能成功初始化。"
                        f"该模块将无法正常工作。"
                    )
            else:
                self.intrusive_generator_instance = None
                logger.info("侵入性思维模块在配置中未启用，跳过其初始化。")

            if not all(
                [
                    self.main_consciousness_llm_client,
                    self.core_comm_layer,
                    self.action_handler_instance,
                    self.event_storage_service,
                    self.conversation_storage_service,
                    self.thought_storage_service,
                    self.action_log_service,  # 确保 action_log_service 也被检查
                ]
            ):
                missing_core_logic_deps = [
                    item_name
                    for item_name, status in {
                        "主意识LLM客户端": self.main_consciousness_llm_client,
                        "核心通信层": self.core_comm_layer,
                        "动作处理器": self.action_handler_instance,
                        "事件存储服务": self.event_storage_service,
                        "会话存储服务": self.conversation_storage_service,
                        "思考存储服务": self.thought_storage_service,
                        "动作日志服务": self.action_log_service,
                    }.items()
                    if not status
                ]
                error_message = (
                    f"核心逻辑流 (CoreLogicFlow) 初始化失败：核心依赖缺失 - {', '.join(missing_core_logic_deps)}。"
                )
                logger.critical(error_message)
                raise RuntimeError(error_message)

            self.core_logic_instance = CoreLogicFlow(
                event_storage_service=self.event_storage_service,
                thought_storage_service=self.thought_storage_service,
                main_consciousness_llm_client=self.main_consciousness_llm_client,
                intrusive_thoughts_llm_client=self.intrusive_thoughts_llm_client,
                core_comm_layer=self.core_comm_layer,
                action_handler_instance=self.action_handler_instance,
                intrusive_generator_instance=self.intrusive_generator_instance,
                stop_event=self.stop_event,
                immediate_thought_trigger=self.immediate_thought_trigger,
            )
            if self.action_handler_instance:
                self.action_handler_instance.set_thought_trigger(self.immediate_thought_trigger)
                logger.info("已尝试为 ActionHandler 设置主思维触发器。")

            logger.info("核心逻辑流 (CoreLogicFlow) 已成功初始化并注入了新的存储服务和触发器。")
            logger.info("=== AIcarus Core 系统所有核心组件初始化完毕！ ===")
        except Exception as e:
            logger.critical(f"AIcarus Core 系统初始化过程中发生严重错误: {e}", exc_info=True)
            await self.shutdown()
            raise

    async def start(self) -> None:
        """启动核心系统的所有后台服务和主循环。"""
        if not self.core_logic_instance or not self.core_comm_layer:
            logger.critical("核心组件 (CoreLogic 或 CoreCommLayer) 未完全初始化，系统无法启动。")
            return
        server_task: asyncio.Task | None = None
        thinking_loop_task: asyncio.Task | None = None
        try:
            if self.intrusive_generator_instance and config.intrusive_thoughts_module_settings.enabled:
                self.intrusive_thread = self.intrusive_generator_instance.start_background_generation()
                if self.intrusive_thread:
                    logger.info("侵入性思维后台生成线程已启动。")
                else:
                    logger.warning("侵入性思维后台生成线程未能启动 (可能由于其内部依赖未满足或模块已在配置中禁用)。")
            if self.core_comm_layer:
                server_task = asyncio.create_task(self.core_comm_layer.start(), name="CoreWebSocketServerTask")
                logger.info("核心 WebSocket 服务器的异步任务已启动，开始监听连接。")
            if self.core_logic_instance:
                thinking_loop_task = await self.core_logic_instance.start_thinking_loop()
                logger.info("核心逻辑大脑的思考循环异步任务已启动。")
            tasks_to_wait = [t for t in [server_task, thinking_loop_task] if t is not None]
            if not tasks_to_wait:
                logger.warning("没有核心异步任务 (WebSocket服务器或思考循环) 被成功启动。系统可能不会执行其主要功能。")
                return
            done, pending = await asyncio.wait(tasks_to_wait, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task_name = task.get_name() if hasattr(task, "get_name") and task.get_name() else "一个已完成的关键任务"
                if task.cancelled():
                    logger.info(f"任务 '{task_name}' 被取消。")
                elif task.exception():
                    exc = task.exception()
                    logger.critical(f"关键任务 '{task_name}' 因未捕获的异常而意外终止: {exc!r}", exc_info=exc)
                    if exc:
                        raise exc
                else:
                    logger.info(f"任务 '{task_name}' 已正常结束。")
            for task in pending:
                task_name = task.get_name() if hasattr(task, "get_name") and task.get_name() else "一个挂起的关键任务"
                self.logger.info(f"一个关键任务已结束，正在请求取消其他仍在运行的挂起任务 '{task_name}'...")
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        logger.info(f"挂起的任务 '{task_name}' 已成功响应取消请求并结束。")
                    except Exception as e_cancel:
                        logger.error(f"尝试取消挂起任务 '{task_name}' 时发生意外错误: {e_cancel}", exc_info=True)
        except asyncio.CancelledError:
            logger.info("AIcarus Core 主启动流程 (start 方法) 被外部取消。")
        except Exception as e:
            logger.critical(f"AIcarus Core 系统在启动或运行期间发生未处理的严重错误: {e}", exc_info=True)
            raise
        finally:
            logger.info("--- AIcarus Core 系统正在进入关闭流程 (从 start 方法的 finally 块触发)... ---")
            await self.shutdown()

    async def shutdown(self) -> None:
        """优雅地关闭所有已初始化的核心组件和服务。"""
        logger.info("--- 正在执行 AIcarus Core 系统的关闭流程 ---")
        self.stop_event.set()
        if self.core_logic_instance:
            logger.info("正在请求停止核心逻辑大脑的思考循环...")
            await self.core_logic_instance.stop()
            logger.info("核心逻辑大脑的思考循环已处理停止请求。")
        if self.intrusive_thread is not None and self.intrusive_thread.is_alive():
            logger.info("正在等待侵入性思维后台生成线程结束 (超时设置10秒)...")
            self.intrusive_thread.join(timeout=10.0)
            if self.intrusive_thread.is_alive():
                logger.warning("警告：侵入性思维后台线程在10秒超时后仍未结束。可能需要强制处理或检查其循环逻辑。")
            else:
                logger.info("侵入性思维后台生成线程已成功结束。")
        if self.core_comm_layer:
            logger.info("正在请求停止核心 WebSocket 通信层...")
            await self.core_comm_layer.stop()
            logger.info("核心 WebSocket 通信层已处理停止请求。")
        if self.conn_manager:
            logger.info("正在关闭数据库连接管理器...")
            await self.conn_manager.close_client()
            logger.info("数据库连接管理器及其底层连接已关闭。")
        llm_clients_to_close: list[ProcessorClient | None] = [
            self.main_consciousness_llm_client,
            self.action_llm_client,
            self.summary_llm_client,
            self.intrusive_thoughts_llm_client,
            self.embedding_llm_client,
        ]
        for llm_client_wrapper in llm_clients_to_close:
            if (
                llm_client_wrapper
                and hasattr(llm_client_wrapper.llm_client, "_close_session_if_any")
                and callable(llm_client_wrapper.llm_client._close_session_if_any)
            ):
                try:
                    logger.info(
                        f"尝试关闭 LLM 客户端 ({llm_client_wrapper.llm_client.provider} - {llm_client_wrapper.llm_client.model_name}) 的底层 aiohttp 会话 (如果存在)..."
                    )
                    await llm_client_wrapper.llm_client._close_session_if_any()  # type: ignore
                except Exception as e_llm_close:
                    logger.warning(f"关闭 LLM 客户端的底层会话时出错: {e_llm_close}")
        logger.info("AIcarus Core 系统所有组件的关闭流程已执行完毕。")


async def start_core_system() -> None:
    """主异步函数，用于初始化并启动 AIcarus Core 系统。"""
    initializer = CoreSystemInitializer()
    try:
        await initializer.initialize()
        await initializer.start()
    except Exception as e:
        logger.critical(f"AIcarus Core 系统启动或运行过程中遭遇致命错误: {e}", exc_info=True)
        await initializer.shutdown()


if __name__ == "__main__":
    if os.name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        asyncio.run(start_core_system())
    except KeyboardInterrupt:
        logger.info("AIcarus Core (main.py __main__): 检测到用户中断 (KeyboardInterrupt)，程序正在准备退出...")
    except Exception as main_execution_exc:
        logger.critical(
            f"AIcarus Core (main.py __main__): 顶层执行过程中发生未捕获的严重异常: {main_execution_exc}", exc_info=True
        )
    finally:
        logger.info("AIcarus Core (main.py __main__): 程序最终执行流程结束。")
