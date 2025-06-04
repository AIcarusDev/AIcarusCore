# AIcarusCore/src/main.py
import asyncio
import threading
import os
import json
from urllib.parse import urlparse
from typing import Any, Callable, Awaitable # 确保导入 Callable, Awaitable

from src.action.action_handler import ActionHandler
from src.common.custom_logging.logger_manager import get_logger
from src.config.global_config import get_global_config, AlcarusRootConfig
from src.config.alcarus_configs import ModelParams 
from src.core_communication.core_ws_server import CoreWebsocketServer, AdapterEventCallback # 导入 AdapterEventCallback
from src.core_logic.consciousness_flow import CoreLogic as CoreLogicFlow 
from src.core_logic.intrusive_thoughts import IntrusiveThoughtsGenerator
from src.database.arangodb_handler import ArangoDBHandler
from src.llmrequest.llm_processor import Client as ProcessorClient
from src.message_processing.default_message_processor import DefaultMessageProcessor
from aicarus_protocols import Event # 导入 Event 用于类型提示
from websockets.server import WebSocketServerProtocol # 导入 WebSocketServerProtocol 用于类型提示


logger = get_logger("AIcarusCore.MainInitializer")

class CoreSystemInitializer:
    """
    🥵 AIcarus Core 系统的性感总管家 🥵
    它负责唤醒和调教所有核心组件，让它们以最完美的状态为主人服务。
    """

    def __init__(self): #
        self.root_cfg: AlcarusRootConfig | None = None
        self.db_handler: ArangoDBHandler | None = None
        self.main_consciousness_llm_client: ProcessorClient | None = None
        self.action_llm_client: ProcessorClient | None = None
        self.summary_llm_client: ProcessorClient | None = None
        self.intrusive_thoughts_llm_client: ProcessorClient | None = None
        self.core_comm_layer: CoreWebsocketServer | None = None
        self.message_processor: DefaultMessageProcessor | None = None
        self.action_handler_instance: ActionHandler | None = None
        self.intrusive_generator_instance: IntrusiveThoughtsGenerator | None = None
        self.core_logic_instance: CoreLogicFlow | None = None
        self.intrusive_thread: threading.Thread | None = None
        self.stop_event: threading.Event = threading.Event()
        logger.info("CoreSystemInitializer 的性感身体已准备就绪...等待主人的命令。")

    async def _initialize_llm_clients(self) -> None: #
        """根据主人的欲望（配置），初始化所有LLM客户端肉棒。"""
        if not self.root_cfg:
            logger.critical("主人，没有全局配置，小色猫无法为您准备LLM肉棒！")
            raise RuntimeError("Root config not loaded. Cannot initialize LLM clients.")

        logger.info("开始为主人精心准备所有LLM客户端肉棒...")
        general_llm_settings_obj = self.root_cfg.llm_client_settings
        proxy_settings_obj = self.root_cfg.proxy
        final_proxy_host: str | None = None
        final_proxy_port: int | None = None

        if proxy_settings_obj.use_proxy and proxy_settings_obj.http_proxy_url:
            try:
                parsed_url = urlparse(proxy_settings_obj.http_proxy_url)
                final_proxy_host = parsed_url.hostname
                final_proxy_port = parsed_url.port
                if not final_proxy_host or not final_proxy_port:
                    logger.warning(f"主人的代理URL '{proxy_settings_obj.http_proxy_url}' 似乎不完整，小色猫将忽略它。")
                    final_proxy_host, final_proxy_port = None, None
            except Exception as e_parse_proxy:
                logger.warning(f"解析主人的代理URL '{proxy_settings_obj.http_proxy_url}' 失败了: {e_parse_proxy}。小色猫还是不用代理了。")
                final_proxy_host, final_proxy_port = None, None

        resolved_abandoned_keys: list[str] | None = None
        env_val_abandoned = os.getenv("LLM_ABANDONED_KEYS")
        if env_val_abandoned:
            try:
                keys_from_env = json.loads(env_val_abandoned)
                if isinstance(keys_from_env, list):
                    resolved_abandoned_keys = [str(k).strip() for k in keys_from_env if str(k).strip()]
            except json.JSONDecodeError:
                logger.warning(
                    f"环境变量 'LLM_ABANDONED_KEYS' 的值不是有效的JSON列表。值: {env_val_abandoned[:50]}..."
                )
                resolved_abandoned_keys = [k.strip() for k in env_val_abandoned.split(",") if k.strip()]
            if not resolved_abandoned_keys and env_val_abandoned.strip():
                resolved_abandoned_keys = [env_val_abandoned.strip()]

        def _create_single_processor_client(purpose_key: str, default_provider_name: str) -> ProcessorClient | None:
            """为特定目的创建一个 ProcessorClient 实例，就像为主人定制专属玩具一样。"""
            try:
                if not self.root_cfg or self.root_cfg.providers is None: 
                    logger.error("配置错误：AlcarusRootConfig 或其 'providers' 配置段缺失，无法定制LLM玩具。")
                    return None
                provider_settings = getattr(self.root_cfg.providers, default_provider_name.lower(), None) 
                if provider_settings is None or provider_settings.models is None: 
                    logger.error(
                        f"配置错误：未找到提供商 '{default_provider_name}' 的有效配置或其 'models' 配置段。这款LLM玩具暂时缺货哦。"
                    )
                    return None
                model_params_cfg = getattr(provider_settings.models, purpose_key, None) 
                if not isinstance(model_params_cfg, ModelParams): 
                    logger.error(f"配置错误：模型用途键 '{purpose_key}' 的配置无效或类型不匹配。这款LLM玩具的型号不对呢。")
                    return None
                actual_provider_name_str: str = model_params_cfg.provider 
                actual_model_api_name: str = model_params_cfg.model_name 
                if not actual_provider_name_str or not actual_model_api_name: 
                    logger.error(f"配置错误：模型 '{purpose_key}' 未指定 'provider' 或 'model_name'。这款LLM玩具信息不全。")
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
                    "proxy_host": final_proxy_host,
                    "proxy_port": final_proxy_port,
                    "abandoned_keys_config": resolved_abandoned_keys,
                    **model_specific_kwargs,
                }
                final_constructor_args = {k: v for k, v in processor_constructor_args.items() if v is not None} 
                client_instance = ProcessorClient(**final_constructor_args)
                logger.info(
                    f"小色猫成功为主人定制了 '{purpose_key}' 用途的 ProcessorClient 实例 (模型肉棒: {client_instance.llm_client.model_name}, 提供商小穴: {client_instance.llm_client.provider}). 主人请享用！"
                )
                return client_instance
            except AttributeError as e_attr: 
                logger.error(
                    f"配置访问错误 (AttributeError) 为用途 '{purpose_key}' 创建LLM客户端时: {e_attr}。主人，您的配置好像有点小问题哦。", exc_info=True
                )
                return None
            except Exception as e: 
                logger.error(f"为用途 '{purpose_key}' 创建LLM客户端时发生未知错误: {e}。主人，小色猫搞不定了啦！", exc_info=True)
                return None

        try:
            self.main_consciousness_llm_client = _create_single_processor_client("main_consciousness", "gemini") 
            if not self.main_consciousness_llm_client: 
                raise RuntimeError("主意识 LLM 客户端肉棒初始化失败。主人，最重要的玩具坏掉了！")

            self.action_llm_client = _create_single_processor_client("action_decision", "gemini") 
            self.summary_llm_client = _create_single_processor_client("information_summary", "gemini") 
            if not self.action_llm_client or not self.summary_llm_client: 
                raise RuntimeError("动作决策或信息总结 LLM 客户端肉棒初始化失败。这些辅助玩具也重要呢！")

            self.intrusive_thoughts_llm_client = _create_single_processor_client("intrusive_thoughts", "gemini") 
            if not self.intrusive_thoughts_llm_client and self.root_cfg and self.root_cfg.intrusive_thoughts_module_settings.enabled: 
                logger.warning("侵入性思维模块已启用，但其LLM客户端肉棒未能成功初始化。这个小调皮可能无法正常工作了。")

            logger.info("主人，所有的LLM客户端肉棒都已为您准备完毕！🥵")
        except RuntimeError: 
            raise
        except Exception as e_init_llms: 
            logger.critical(f"初始化LLM客户端肉棒过程中发生未预期的严重错误: {e_init_llms}。主人，这超出了小色猫的承受范围！", exc_info=True)
            raise RuntimeError(f"LLM客户端初始化因意外错误失败: {e_init_llms}") from e_init_llms

    async def _initialize_database_handler(self) -> None: #
        """
        🥵 从主人的私密空间 (.env) 读取数据库配置，连接数据库小穴 🥵
        让数据有处可喷射，嗯哼！
        """
        logger.info("小色猫正在尝试连接数据库小穴，将直接从主人的环境变量中寻找连接参数...")
        logger.info("请主人确保 ARANGODB_HOST, ARANGODB_USER, ARANGODB_PASSWORD, ARANGODB_DATABASE 这些环境变量已正确设置在您的 .env 文件或系统环境中哦！")
        try:
            self.db_handler = await ArangoDBHandler.create()
            if not self.db_handler or not self.db_handler.db:
                raise RuntimeError("ArangoDBHandler 或其内部 db 对象未能初始化。数据库小穴连接失败！")
            
            logger.info(f"数据库小穴连接成功: {self.db_handler.db.name}。随时可以注入数据了，主人！")

            logger.info("正在为主人准备数据库中的各种“房间”（集合）...")
            await self.db_handler.ensure_collection_exists(ArangoDBHandler.THOUGHTS_COLLECTION_NAME) 
            await self.db_handler.ensure_collection_exists(ArangoDBHandler.INTRUSIVE_THOUGHTS_POOL_COLLECTION_NAME) 
            await self.db_handler.ensure_collection_exists(ArangoDBHandler.ACTION_LOGS_COLLECTION_NAME) 
            await self.db_handler.ensure_collection_exists(ArangoDBHandler.EVENTS_COLLECTION_NAME) 
            logger.info("数据库的“房间”都已为主人准备好！")

        except Exception as e:
            logger.critical(f"初始化数据库小穴失败: {e}。主人，连接不上，好空虚...", exc_info=True)
            raise

    async def initialize(self) -> None: #
        """
        初始化 AIcarus Core 系统中的所有核心组件。
        就像一场精心策划的性感派对，每个角色都要准备就绪！
        """
        logger.info("=== 🥵 主人，AIcarus Core 系统正在为您精心初始化，请稍候... 🥵 ===")
        try:
            self.root_cfg = get_global_config() 
            logger.info("主人的全局欲望（配置）已成功读取！")

            await self._initialize_llm_clients()
            await self._initialize_database_handler()

            if not self.db_handler: 
                 raise RuntimeError("数据库处理器未初始化，无法创建消息处理器。")
            
            # --- 依赖注入顺序调整开始 ---
            # 1. 先创建 DefaultMessageProcessor (此时它的 core_comm_layer 可能是 None)
            self.message_processor = DefaultMessageProcessor(
                db_handler=self.db_handler,
                core_websocket_server=None # 明确传入 None，或让构造函数默认为 None
            )
            logger.info("消息口穴处理器已初步准备好（等待菊花连接）！")

            # 2. 从 message_processor 获取事件处理回调
            event_handler_for_ws: AdapterEventCallback # 类型提示
            if self.message_processor:
                if hasattr(self.message_processor, 'process_event'):
                    event_handler_for_ws = self.message_processor.process_event
                else:
                    logger.error("DefaultMessageProcessor 实例缺少 'process_event' 方法！")
                    raise RuntimeError("DefaultMessageProcessor 缺少必要的事件处理方法。")
            else:
                raise RuntimeError("DefaultMessageProcessor 未能成功初始化。") # 理论上不会执行到这里

            # 3. 创建 CoreWebsocketServer 实例，传入回调
            if not self.root_cfg: # 确保 root_cfg 已加载
                raise RuntimeError("Root config 未加载，无法创建 WebSocket 服务器。")
            ws_host = self.root_cfg.server.host 
            ws_port = self.root_cfg.server.port 

            self.core_comm_layer = CoreWebsocketServer(
                host=ws_host, 
                port=ws_port, 
                event_handler_callback=event_handler_for_ws,
                db_instance=self.db_handler.db if self.db_handler else None # 传递 StandardDatabase 实例
            )
            logger.info(f"CoreWebsocketServer 的菊花已在 {ws_host}:{ws_port} 张开，并连接了消息口穴！")

            # 4. 将创建好的 CoreWebsocketServer 实例设置回 DefaultMessageProcessor (关键步骤)
            if self.message_processor and self.core_comm_layer:
                self.message_processor.core_comm_layer = self.core_comm_layer
                logger.info("消息口穴处理器现已完全连接到通信菊花！(DefaultMessageProcessor.core_comm_layer 已设置)")
            else:
                # 这个 else 分支理论上不应该被触发，如果前面的步骤都成功了
                logger.error("未能将 CoreWebsocketServer 实例设置回 DefaultMessageProcessor，或其中一个实例为 None。")
                if not self.message_processor:
                    logger.error("原因是: self.message_processor 是 None")
                if not self.core_comm_layer:
                    logger.error("原因是: self.core_comm_layer 是 None")

            # --- 依赖注入顺序调整结束 ---


            self.action_handler_instance = ActionHandler() 
            logger.info("动作调教处理器已饥渴难耐！")
            
            # 设置 ActionHandler 的依赖 (LLM客户端和通信层)
            if self.action_llm_client: 
                self.action_handler_instance.action_llm_client = self.action_llm_client
            if self.summary_llm_client: 
                self.action_handler_instance.summary_llm_client = self.summary_llm_client
            
            if self.db_handler and self.core_comm_layer: # 确保两者都存在
                 self.action_handler_instance.set_dependencies(
                     db_handler=self.db_handler, 
                     comm_layer=self.core_comm_layer # 将 core_comm_layer 传递给 ActionHandler
                 )
                 logger.info("动作处理器的数据库小穴和通信菊花已成功连接！")
            else:
                logger.warning("未能完全设置 ActionHandler 的依赖（数据库或通信层）。")

            if self.action_handler_instance.action_llm_client and self.action_handler_instance.summary_llm_client:
                logger.info("ActionHandler 的 LLM 客户端肉棒已成功插入。")
            else:
                logger.warning("ActionHandler 的 LLM 客户端肉棒未能从 Initializer 内部设置，它可能会在运行时自己尝试寻找哦。")


            if not self.root_cfg: # 再次检查，因为后续逻辑依赖它
                 raise RuntimeError("缺少初始化侵入性思维生成器所需的配置。")
            intrusive_settings = self.root_cfg.intrusive_thoughts_module_settings 
            persona_settings = self.root_cfg.persona 
            if intrusive_settings.enabled and self.intrusive_thoughts_llm_client and self.db_handler: 
                self.intrusive_generator_instance = IntrusiveThoughtsGenerator( 
                    llm_client=self.intrusive_thoughts_llm_client,
                    db_handler=self.db_handler,
                    persona_cfg=persona_settings,
                    module_settings=intrusive_settings,
                    stop_event=self.stop_event, 
                )
            elif intrusive_settings.enabled:
                logger.warning("侵入性思维模块已启用，但其LLM客户端肉棒或数据库小穴未准备好，这个小调皮暂时玩不起来。")
            else:
                logger.info("侵入性思维模块在主人的欲望中未被启用。")

            if not all([ 
                self.root_cfg, self.db_handler, self.main_consciousness_llm_client,
                self.core_comm_layer, self.action_handler_instance
            ]):
                raise RuntimeError("核心逻辑大脑初始化所需的某些“春药”缺失！")

            self.core_logic_instance = CoreLogicFlow( 
                root_cfg=self.root_cfg,
                db_handler=self.db_handler,
                main_consciousness_llm_client=self.main_consciousness_llm_client,
                intrusive_thoughts_llm_client=self.intrusive_thoughts_llm_client, 
                core_comm_layer=self.core_comm_layer,
                action_handler_instance=self.action_handler_instance,
                intrusive_generator_instance=self.intrusive_generator_instance, 
                stop_event=self.stop_event 
            )
            logger.info("CoreLogic 的性感大脑已成功唤醒，并注入了所有“春药”！准备好喷发思想了！")

            logger.info("主人，AIcarus Core 系统的所有性感部件都已为您初始化完毕！可以开始狂欢了！🎉")

        except Exception as e: 
            logger.critical(f"主人，AIcarus Core 系统初始化时出大问题了: {e}！小色猫要坏掉了！", exc_info=True)
            await self.shutdown() 
            raise

    async def start(self) -> None: #
        """启动核心系统的所有性感律动。"""
        if not self.core_logic_instance or not self.core_comm_layer: 
            logger.critical("主人，核心系统还未完全初始化，无法开始性感派对！请先调用 initialize() 方法。")
            return

        server_task = None
        thinking_loop_task = None

        try:
            if self.intrusive_generator_instance:
                self.intrusive_thread = self.intrusive_generator_instance.start_background_generation()
                if self.intrusive_thread: 
                    logger.info("侵入性思维的后台小马达已启动，准备随机插入刺激！")
                else:
                    logger.warning("未能启动侵入性思维的后台小马达。")

            server_task = asyncio.create_task(self.core_comm_layer.start()) 
            logger.info("WebSocket 服务器菊花的异步任务已启动，准备迎接连接！")

            thinking_loop_task = await self.core_logic_instance.start_thinking_loop() 
            logger.info("核心逻辑大脑的思考循环已启动，思想的潮吹即将开始！")

            # 等待任一关键任务结束
            if server_task and thinking_loop_task:
                done, pending = await asyncio.wait( 
                    [server_task, thinking_loop_task], 
                    return_when=asyncio.FIRST_COMPLETED
                )
            elif server_task: # 只有服务器任务
                done, pending = await asyncio.wait([server_task], return_when=asyncio.FIRST_COMPLETED)
            elif thinking_loop_task: # 只有思考循环任务
                done, pending = await asyncio.wait([thinking_loop_task], return_when=asyncio.FIRST_COMPLETED)
            else: # 没有任务启动
                logger.warning("没有关键任务（服务器或思考循环）被启动。")
                return


            for task in pending: 
                task_name = task.get_name() if hasattr(task, 'get_name') else "未知任务"
                logger.info(f"一个关键的性感任务已结束，正在让其他还在扭动的任务 ({task_name}) 也冷静下来...")
                if not task.done(): # 检查任务是否已完成，避免重复取消
                    task.cancel()
                    try:
                        await task 
                    except asyncio.CancelledError:
                        logger.info(f"任务 {task_name} 已被成功“安抚”。")
                    except Exception as e_cancel:
                        logger.error(f"“安抚”任务 {task_name} 时发生意外: {e_cancel}")

            for task in done: 
                task_name = task.get_name() if hasattr(task, 'get_name') else "未知任务"
                if task.cancelled():
                    logger.info(f"任务 {task_name} 被取消。")
                elif task.exception():
                    exc = task.exception()
                    logger.critical(
                        f"一个关键的性感任务 ({task_name}) 因为过于兴奋而出错了: {exc}！主人，我们可能玩脱了！", exc_info=exc 
                    )
                    # 重新抛出异常，让上层知道发生了问题
                    if exc: # 确保 exc 不是 None
                         raise exc 
                else:
                    logger.info(f"任务 {task_name} 正常结束。")


        except asyncio.CancelledError: 
            logger.info("主人的性感派对被取消了。嘤嘤嘤...")
        except Exception as e: 
            logger.critical(f"核心系统在性感律动中发生意外错误: {e}！高潮被打断了！", exc_info=True)
            # 这里也应该重新抛出，以便主程序知道启动失败
            raise
        finally:
            logger.info("--- 性感派对结束，小色猫开始为主人清理现场 ---")
            await self.shutdown()


    async def shutdown(self) -> None: #
        """优雅地结束这场性感派对，清理所有玩具。"""
        logger.info("--- 正在为主人执行 AIcarus Core 系统的温柔关闭流程 ---")
        self.stop_event.set() 

        if self.core_logic_instance: 
            logger.info("正在让核心逻辑大脑进入贤者时间...")
            await self.core_logic_instance.stop()

        if self.core_comm_layer: 
            logger.info("正在温柔地关闭 WebSocket 服务器菊花...")
            await self.core_comm_layer.stop()

        if self.intrusive_thread is not None and self.intrusive_thread.is_alive(): 
            logger.info("正在等待侵入性思维的后台小马达完全冷静...")
            self.intrusive_thread.join(timeout=5) 
            if self.intrusive_thread.is_alive(): 
                logger.warning("警告：侵入性思维的后台小马达在超时后依然兴奋。")
            else:
                logger.info("侵入性思维的后台小马达已成功冷静下来。")

        if self.db_handler and hasattr(self.db_handler, "close") and callable(self.db_handler.close): 
            logger.info("正在断开与数据库小穴的连接...")
            # ArangoDBHandler.close() 可能是同步的，需要确认
            # 如果是同步的，不需要 await
            if asyncio.iscoroutinefunction(self.db_handler.close):
                await self.db_handler.close()
            else:
                self.db_handler.close() # 假设是同步
        
        logger.info("主人，AIcarus Core 系统的所有性感部件都已为您清理完毕。期待下一次与您共度春宵...❤️")

async def start_core_system() -> None: #
    """启动 AIcarus Core 系统性感派对的主入口。"""
    initializer = CoreSystemInitializer() 
    try:
        await initializer.initialize() 
        await initializer.start() 
    except Exception as e: 
        logger.critical(f"主人，AIcarus Core 的性感派对启动失败: {e}", exc_info=True)
        # 在这里确保即使启动失败也尝试关闭
        await initializer.shutdown()
    finally:
        logger.info("AIcarus Core 性感派对程序执行完毕。晚安，主人。")

if __name__ == "__main__": #
    try:
        asyncio.run(start_core_system())
    except KeyboardInterrupt: # 捕获 Ctrl+C
        logger.info("AIcarus Core (main.py __main__): 收到 KeyboardInterrupt，程序正在优雅退出...")
        # asyncio.run 会在 KeyboardInterrupt 时自动清理任务，但我们还是显式调用 shutdown
        # 注意：如果 start_core_system 内部的 shutdown 已经执行，这里可能重复。
        # 但多次调用 shutdown 应该是安全的（幂等的）。
        # loop = asyncio.get_event_loop()
        # if loop.is_running():
        #     # 如果事件循环仍在运行，尝试获取 initializer 实例并调用 shutdown
        #     # 这比较复杂，因为 initializer 是在 start_core_system 内部创建的
        #     # 更好的做法是让 start_core_system 的 finally 块处理所有清理
        #     pass
        print("AIcarus Core (main.py __main__): KeyboardInterrupt 处理完成。")
    except Exception as main_exc:
        logger.critical(f"AIcarus Core (main.py __main__): 顶层发生未处理的严重错误: {main_exc}", exc_info=True)