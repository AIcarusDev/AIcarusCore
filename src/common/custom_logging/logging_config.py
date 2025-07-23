# src/common/custom_logging/logging_config.py
import os
import sys
import threading
from pathlib import Path

from loguru import logger
from loguru._logger import Logger

# --- 核心配置 ---
LOG_DIR = Path(os.getcwd()) / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

# 模块名称到显示别名和颜色的映射
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
    "llmrequest": ("LLM 请求", "light-blue"),
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
    "common.json_parser.json_parser": ("JSON 解析", "white"),
    # 配置
    "config.config_io": ("配置IO", "yellow"),
    "config.config_manager": ("配置管理", "yellow"),
    "config.config_updater": ("配置更新", "yellow"),
    "config.config_validator": ("配置验证", "yellow"),
    # 核心通信
    "core_communication.action_sender": ("动作发送", "yellow"),
    "core_communication.core_ws_server": ("核心WS服务", "yellow"),
    "core_communication.event_receiver": ("事件接收", "yellow"),
    "core_communication.event_sender": ("事件发送", "yellow"),
    "core_communication.message_receiver": ("消息接收", "yellow"),
    # 核心逻辑
    "core_logic.consciousness_flow": ("核心循环", "yellow"),
    "core_logic.context_builder": ("上下文构建", "yellow"),
    "core_logic.intrusive_thoughts": ("侵入思考", "light-red"),
    "core_logic.prompt_builder": ("提示词构建", "yellow"),
    "core_logic.state_manager": ("状态管理", "light-yellow"),
    "core_logic.thought_generator": ("思考生成", "yellow"),
    "core_logic.thought_persistor": ("思考持久化", "yellow"),
    "core_logic.unread_info_service": ("未读服务", "yellow"),
    # 插件
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
    "llmrequest.llm_processor": ("LLM 处理", "light-blue"),
    "llmrequest.utils_model": ("LLM 底层", "blue"),
    "tools.failure_reporter": ("失败报告", "blue"),
    "tools.platform_actions": ("平台动作", "blue"),
    "tools.web_searcher": ("网页搜索", "blue"),
    "tools.search": ("搜索工具", "blue"),
    # 消息处理
    "message_processing.default_message_processor": ("默认消息处理", "magenta"),
}

# --- Loguru 初始化 (不变) ---
logger.remove()

# --- 全局状态与锁 ---
_handlers_created = set()
_lock = threading.Lock()


def get_logger(module_name: str) -> Logger:
    """获取一个为指定模块配置好的 logger 实例."""
    # 找到最匹配的别名和颜色
    best_match_key = ""
    for prefix in MODULE_CONFIG_MAP:
        normalized_module_name = module_name.replace("AIcarusCore\\", "").replace("\\", ".")
        # 核心修复：将 startswith 改回 endswith
        if normalized_module_name.endswith(prefix) and len(prefix) > len(best_match_key):
            best_match_key = prefix

    if best_match_key:
        alias, color = MODULE_CONFIG_MAP[best_match_key]
    else:
        alias = module_name.split(".")[-1]
        color = "white"

    handler_key = f"{alias}_{color}"

    # 1. 计算最大显示宽度（考虑汉字占2个字符）
    max_width = 0
    for a, _ in MODULE_CONFIG_MAP.values():
        # 简单的宽度计算，一个汉字约等于两个英文字符
        width = sum(2 if "\u4e00" <= char <= "\u9fff" else 1 for char in a)
        if width > max_width:
            max_width = width

    # 使用锁确保线程安全地添加 handler
    with _lock:
        if handler_key not in _handlers_created:
            # --- 控制台日志格式 ---
            console_format = (
                "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
                f"<{color}>{{extra[alias]: <{max_width}}}</{color}> | "
                "<level>{message}</level>"
            )
            logger.add(
                sys.stdout,
                format=console_format,
                level="DEBUG",
                filter=lambda record: record["extra"].get("alias") == alias,
            )

            # --- 文件日志格式 ---
            module_log_dir = LOG_DIR / alias
            module_log_dir.mkdir(exist_ok=True)
            log_file_path = module_log_dir / "{time:YYYY-MM-DD}.log"
            
            file_format = "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {extra[alias]} | {name}:{function}:{line} - {message}"

            logger.add(
                sink=log_file_path,
                rotation="00:00",
                retention="90 days",
                compression="zip",
                level="DEBUG",
                format=file_format,
                encoding="utf-8",
                filter=lambda record: record["extra"].get("alias") == alias,
                enqueue=True,
                backtrace=True,
                diagnose=True
            )

            _handlers_created.add(handler_key)

    # 返回一个绑定了别名的 logger 实例
    return logger.bind(alias=alias)


