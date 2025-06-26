# src/common/custom_logging/logging_config.py (小懒猫·最终防线版)
import os
import sys
import threading # <--- 把它请进来！
from pathlib import Path

from loguru import logger

# --- 核心配置 (不变) ---
LOG_DIR = Path(os.getcwd()) / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

MODULE_CONFIG_MAP = {
    # 根模块
    "main": ("主程序", "white"),
    "__main__": ("主程序", "white"),

    # AIcarusCore 顶级模块
    "action": ("动作处理", "light-magenta"),
    "common": ("通用模块", "white"),
    "config": ("配置管理", "yellow"),
    "core_communication": ("核心通信", "yellow"),
    "core_logic": ("核心逻辑", "light-yellow"),
    "database": ("数据库", "light-cyan"),
    "focus_chat_mode": ("专注聊天", "light-green"),
    "llmrequest": ("LLM请求", "light-blue"),
    "message_processing": ("消息处理", "magenta"),
    "plugins": ("插件", "purple"),
    "tools": ("工具箱", "blue"),

    # 动作处理
    "action.action_handler": ("动作处理", "light-magenta"),
    "action.providers.internal_tools_provider": ("内部工具提供", "magenta"),
    "action.components.action_decision_maker": ("动作决策", "magenta"),

    # 通用模块
    "common.custom_logging.logging_config": ("日志配置", "white"),
    "common.custom_logging.logger_manager": ("日志管理", "white"),
    "common.focus_chat_history_builder.chat_prompt_builder": ("聊天提示构建", "green"),
    "common.intelligent_interrupt_system.iis_main": ("智能中断", "green"),
    "common.summarization_observation.summarization_service": ("观察摘要", "light-black"),
    "common.utils": ("通用工具", "white"),
    "common.summarization_observation": ("观察摘要", "light-black"),

    # 配置
    "config.config_io": ("配置IO", "yellow"),
    "config.config_manager": ("配置管理", "yellow"),
    "config.config_updater": ("配置更新", "yellow"),

    # 核心通信
    "core_communication.action_sender": ("动作发送", "yellow"),
    "core_communication.core_ws_server": ("核心WS服务", "yellow"),
    "core_communication.event_receiver": ("事件接收", "yellow"),

    # 核心逻辑
    "core_logic.consciousness_flow": ("核心循环", "yellow"),
    "core_logic.context_builder": ("上下文构建", "yellow"),
    "core_logic.intrusive_thoughts": ("侵入思考", "light-red"),
    "core_logic.prompt_builder": ("提示词构建", "yellow"),
    "core_logic.state_manager": ("状态管理", "light-yellow"),
    "core_logic.thought_generator": ("思考生成", "yellow"),
    "core_logic.thought_persistor": ("思考持久化", "yellow"),
    "core_logic.unread_info_service": ("未读服务", "yellow"),

    # 数据库
    "database.core.connection_manager": ("数据库核心", "cyan"),
    "database.models": ("数据库模型", "cyan"),
    "database.services.action_log_storage_service": ("动作日志", "cyan"),
    "database.services.conversation_storage_service": ("会话存储", "cyan"),
    "database.services.event_storage_service": ("事件存储", "cyan"),
    "database.services.summary_storage_service": ("摘要存储", "cyan"),
    "database.services.thought_storage_service": ("思考存储", "cyan"),
    "database.services": ("数据库服务", "cyan"),

    # 专注聊天
    "focus_chat_mode.action_executor": ("动作执行", "green"),
    "focus_chat_mode.chat_session": ("专注会话", "light-green"),
    "focus_chat_mode.chat_session_manager": ("会话管理", "green"),
    "focus_chat_mode.focus_chat_cycler": ("专注循环", "green"),
    "focus_chat_mode.llm_response_handler": ("LLM响应处理", "green"),
    "focus_chat_mode.summarization_manager": ("摘要管理", "green"),

    # LLM & 工具
    "llmrequest.llm_processor": ("LLM处理", "light-blue"),
    "llmrequest.utils_model": ("LLM底层", "blue"),
    "tools.failure_reporter": ("失败报告", "blue"),
    "tools.platform_actions": ("平台动作", "blue"),
    "tools.web_searcher": ("网页搜索", "blue"),
    "tools.search": ("搜索工具", "blue"),

    # 消息处理
    "message_processing.default_message_processor": ("默认消息处理", "magenta"),
    "message_processing": ("消息处理器", "white"),
}

# --- Loguru 初始化 (不变) ---
logger.remove()

# --- 全局状态与锁 ---
_handlers_created = set()
_lock = threading.Lock() # <--- 这就是我们的贞操锁！

# --- 核心获取函数 ---
def get_logger(module_name: str):
    """
    获取一个为指定模块配置好的 logger 实例 (线程安全版)。
    """
    
    # 匹配逻辑不变
    best_match_key = ""
    for prefix in MODULE_CONFIG_MAP:
        normalized_module_name = module_name.replace("AIcarusCore\\", "").replace("\\", ".")
        if normalized_module_name.endswith(prefix) and len(prefix) > len(best_match_key):
            best_match_key = prefix
            
    if best_match_key:
        alias, color = MODULE_CONFIG_MAP[best_match_key]
    else:
        alias = module_name.split('.')[-1]
        color = "white"

    handler_key = f"{alias}_{color}"

    # 【关键改动】在检查和创建处理器之前，先上锁！
    with _lock:
        if handler_key not in _handlers_created:
            # --- 下面的创建逻辑都在锁的保护之下，一次只许一个人进来！ ---
            console_format = (
                "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
                "<level>{level: <8}</level> | "
                f"<{color}><bold>{{extra[alias]: <15}}</bold></{color}> | "
                "<level>{message}</level>"
            )
            logger.add(
                sys.stderr,
                level=os.getenv("CONSOLE_LOG_LEVEL", "INFO").upper(),
                format=console_format,
                filter=lambda record: record["extra"].get("alias") == alias,
                colorize=True,
                enqueue=True,
            )

            log_file_path = LOG_DIR / alias / "{time:YYYY-MM-DD}.log"
            log_file_path.parent.mkdir(parents=True, exist_ok=True)
            logger.add(
                sink=log_file_path,
                level=os.getenv("FILE_LOG_LEVEL", "DEBUG").upper(),
                format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {extra[alias]: <15} | {message}",
                rotation="00:00",
                retention="7 days",
                compression="zip",
                encoding="utf-8",
                enqueue=True,
                filter=lambda record: record["extra"].get("alias") == alias
            )
            
            _handlers_created.add(handler_key)
            logger.debug(f"已为别名 '{alias}' (颜色: {color}) 创建专属日志处理器。")
            # --- 锁在这里被释放 ---

    return logger.bind(alias=alias, color=color)
