# 文件：AIcarusCore/src/main.py

import asyncio
import json
import os
import threading
from urllib.parse import urlparse

# 从 src 下的其他包导入
from action.action_handler import ActionHandler
from common.custom_logging.logger_manager import get_logger
from config.alcarus_configs import AlcarusRootConfig, LLMClientSettings, ModelParams, ProxySettings
from config.config_manager import get_typed_settings
from core_communication.core_ws_server import CoreWebsocketServer
from database.arangodb_handler import ArangoDBHandler
from llmrequest.llm_processor import Client as ProcessorClient
from sub_consciousness.chat_session_handler import ChatSessionManager
from message_processing.default_message_processor import DefaultMessageProcessor

# 从 core_logic 子包导入 CoreLogic 类
from core_logic.consciousness_loop import CoreLogic 

# 从新的 plugins 子包导入 intrusive_thoughts_plugin
from plugins.intrusive_thoughts_plugin import IntrusiveThoughtsGenerator #

logger = get_logger("AIcarusCore.main")

async def start_consciousness_flow():
    logger.info("开始执行 start_consciousness_flow，准备初始化核心组件...")

    try:
        # 1. 加载配置
        root_cfg = get_typed_settings()
        logger.info("配置加载完毕。")

        # 2. 初始化数据库处理器
        db_handler = await ArangoDBHandler.create()
        logger.info("数据库处理器初始化完毕。")

        # 3. 初始化 LLM 客户端
        def _create_llm_client(purpose_key: str, default_provider: str = "gemini") -> ProcessorClient:
            if not root_cfg.providers:
                raise ValueError(f"配置错误：RootConfig 中缺少 'providers' 段。无法为 '{purpose_key}' 创建LLM客户端。")
            provider_settings = getattr(root_cfg.providers, default_provider.lower(), None)
            if not provider_settings or not provider_settings.models:
                raise ValueError(
                    f"配置错误：在 providers 下未找到 '{default_provider}' 的配置或其 'models' 段。无法为 '{purpose_key}' 创建LLM客户端。"
                )
            model_params_cfg = getattr(provider_settings.models, purpose_key, None)
            if not model_params_cfg:
                raise ValueError(
                    f"配置错误：在提供商 '{default_provider}' 的 models 配置下未找到用途键 '{purpose_key}'。无法创建LLM客户端。"
                )
            
            client_args = {
                "model": {"provider": model_params_cfg.provider, "name": model_params_cfg.model_name},
                "abandoned_keys_config": json.loads(os.getenv("LLM_ABANDONED_KEYS", "null")) if os.getenv("LLM_ABANDONED_KEYS") else None,
                "proxy_host": None,
                "proxy_port": None,
                "image_placeholder_tag": root_cfg.llm_client_settings.image_placeholder_tag,
                "stream_chunk_delay_seconds": root_cfg.llm_client_settings.stream_chunk_delay_seconds,
                "enable_image_compression": root_cfg.llm_client_settings.enable_image_compression,
                "image_compression_target_bytes": root_cfg.llm_client_settings.image_compression_target_bytes,
                "rate_limit_disable_duration_seconds": root_cfg.llm_client_settings.rate_limit_disable_duration_seconds,
            }
            if root_cfg.proxy.use_proxy and root_cfg.proxy.http_proxy_url:
                try:
                    parsed_url = urlparse(root_cfg.proxy.http_proxy_url)
                    client_args["proxy_host"] = parsed_url.hostname
                    client_args["proxy_port"] = parsed_url.port
                except Exception as e_proxy_parse:
                    logger.warning(f"解析代理URL '{root_cfg.proxy.http_proxy_url}' 失败: {e_proxy_parse}。LLM客户端将不使用此配置的代理。")
            
            if model_params_cfg.temperature is not None: client_args["temperature"] = model_params_cfg.temperature
            if model_params_cfg.max_output_tokens is not None: client_args["maxOutputTokens"] = model_params_cfg.max_output_tokens
            if model_params_cfg.top_p is not None: client_args["top_p"] = model_params_cfg.top_p
            if model_params_cfg.top_k is not None: client_args["top_k"] = model_params_cfg.top_k
            client_args_cleaned = {k: v for k, v in client_args.items() if v is not None}
            logger.info(f"正在为 '{purpose_key}' (提供商: {model_params_cfg.provider}, 模型: {model_params_cfg.model_name}) 创建LLM客户端...")
            return ProcessorClient(**client_args_cleaned)

        main_consciousness_llm_client = _create_llm_client("main_consciousness", "gemini")
        intrusive_thoughts_llm_client = _create_llm_client("intrusive_thoughts", "gemini")
        sub_mind_llm_client = _create_llm_client("sub_mind_chat_reply", "gemini")
        action_decision_llm_client_for_core_logic = None
        information_summary_llm_client_for_core_logic = None
        logger.info("核心LLM客户端们初始化(尝试)完毕。ActionHandler的LLM客户端将由其自身管理。")

        # 4. 初始化 CoreWebSocketServer (如果需要)
        core_comm_layer: CoreWebsocketServer | None = None
        logger.info("核心WebSocket通信层(如果配置了的话)的初始化逻辑已跳过(示例)。")

        # 5. 创建 CoreLogic 实例
        stop_event = threading.Event()
        async_stop_event = asyncio.Event()
        sub_mind_update_event = asyncio.Event()

        _core_logic_instance = CoreLogic(
            root_cfg=root_cfg,
            db_handler=db_handler,
            main_consciousness_llm_client=main_consciousness_llm_client,
            intrusive_thoughts_llm_client=intrusive_thoughts_llm_client,
            sub_mind_llm_client=sub_mind_llm_client,
            action_decision_llm_client=action_decision_llm_client_for_core_logic,
            information_summary_llm_client=information_summary_llm_client_for_core_logic,
            stop_event=stop_event,
            async_stop_event=async_stop_event,
            sub_mind_update_event=sub_mind_update_event,
            chat_session_manager=None, # 先传入 None
            core_comm_layer=core_comm_layer
        )
        logger.info("CoreLogic 实例已创建 (ChatSessionManager 暂未设置)。")

        # 6. 初始化 ChatSessionManager 并将其设置回 CoreLogic
        chat_session_manager_instance = ChatSessionManager(core_logic_ref=_core_logic_instance)
        _core_logic_instance.chat_session_manager = chat_session_manager_instance
        logger.info("ChatSessionManager 实例已创建，并已引用 CoreLogic 实例，且已设置到 CoreLogic 中。")

        # 7. 如果 CoreWebSocketServer 存在，将 DefaultMessageProcessor 的实例传入作为回调
        if core_comm_layer:
            message_processor = DefaultMessageProcessor(
                db_handler=db_handler,
                root_config=root_cfg,
                chat_session_manager=chat_session_manager_instance,
                core_logic_ref=_core_logic_instance
            )
            core_comm_layer._message_handler_callback = message_processor.process_message
            logger.info("CoreWebSocketServer 消息处理回调已设置为 DefaultMessageProcessor。")
            
            asyncio.create_task(core_comm_layer.start(), name="CoreWebSocketServerTask")
            logger.info("CoreWebSocketServer 已作为异步任务启动。")
        else:
            logger.warning("CoreWebSocketServer 未初始化，将无法接收来自适配器的消息。")

        # 8. 初始化 IntrusiveThoughtsGenerator (插件化后的 IntrusiveThoughtsGenerator)
        intrusive_generator_instance: IntrusiveThoughtsGenerator | None = None # 声明变量
        if hasattr(root_cfg, 'intrusive_thoughts_module_settings') and \
           root_cfg.intrusive_thoughts_module_settings and \
           root_cfg.intrusive_thoughts_module_settings.enabled:
            
            intrusive_generator_instance = IntrusiveThoughtsGenerator(
                llm_client=intrusive_thoughts_llm_client, 
                db_handler=db_handler, 
                persona_cfg=root_cfg.persona, 
                module_settings=root_cfg.intrusive_thoughts_module_settings, 
                stop_event=stop_event
            )
            logger.info("IntrusiveThoughtsGenerator (插件) 已成功初始化。") 
            # 启动后台线程 (放在这里启动，因为它是一个独立的插件功能)
            intrusive_generator_instance.start_background_generation()
            logger.info("侵入性思维后台生成线程已通过 IntrusiveThoughtsGenerator (插件) 启动。")

        elif hasattr(root_cfg, 'intrusive_thoughts_module_settings') and \
             root_cfg.intrusive_thoughts_module_settings and \
             not root_cfg.intrusive_thoughts_module_settings.enabled:
            logger.info("IntrusiveThoughtsGenerator (插件) 在配置中被禁用，将不会被初始化。")
        else:
            logger.warning("警告：在 root_cfg 中未找到 intrusive_thoughts_module_settings 配置或其 enabled 状态。IntrusiveThoughtsGenerator (插件) 将不会被初始化。")
        
        # 将 intrusive_generator_instance 传递给 _core_logic_instance
        _core_logic_instance.intrusive_generator_instance = intrusive_generator_instance


        logger.info("准备启动 CoreLogic 并等待其核心循环...")
        thinking_task = await _core_logic_instance.start()
        logger.info("CoreLogic 的 start 方法已执行，核心思考循环任务已创建。")
        
        if thinking_task:
            logger.info("正在等待核心思考循环任务完成 (这通常意味着程序将持续运行直到被中断)...")
            try:
                await thinking_task
            except asyncio.CancelledError:
                logger.info("核心思考循环任务被取消。")
            except Exception as e_loop:
                logger.error(f"核心思考循环任务执行时发生错误: {e_loop}", exc_info=True)
        else:
            logger.error("CoreLogic 的 start 方法未能返回有效的任务对象！程序可能无法正常运行。")

        logger.info("start_consciousness_flow 执行流程即将结束 (如果核心循环已结束或未正确等待)。")

    except ValueError as ve:
        logger.critical(f"初始化核心流程时配置或参数错误: {ve}", exc_info=True)
        print(f"程序启动失败：配置错误 - {ve}")
    except Exception as e:
        logger.critical(f"初始化或运行核心流程时发生未预料的严重错误: {e}", exc_info=True)
        print(f"程序启动时发生严重内部错误: {e}")
        import traceback
        traceback.print_exc()

# 允许直接运行这个文件来启动 Core
if __name__ == "__main__":
    try:
        asyncio.run(start_consciousness_flow())
    except KeyboardInterrupt:
        print("\nAIcarus Core (src/main.py): 收到 KeyboardInterrupt，程序正在退出...")
    except Exception as e:
        print(f"AIcarus Core (src/main.py): 发生未处理的严重错误: {e}")
        import traceback
        traceback.print_exc()
    finally:
        print("AIcarus Core (src/main.py): 程序执行完毕。")