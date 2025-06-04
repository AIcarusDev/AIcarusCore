from loguru import logger as loguru_logger

from .logger import (
    CONFIG_STYLE_CONFIG,
    HEARTFLOW_STYLE_CONFIG,
    MAIN_STYLE_CONFIG,
    MODEL_UTILS_STYLE_CONFIG,
    PROCESSOR_STYLE_CONFIG,
    TOOL_USE_STYLE_CONFIG,
    LogConfig,
)
from .logger import (
    get_module_logger as get_module_logger_from_logger_py,
)

LoguruLoggerType = loguru_logger.__class__

# 简化的模块名翻译表
MODULE_NAME_TRANSLATIONS = {
    "AIcarusCore.ActionHandler": "动作处理",
    "AIcarusCore.CoreLogic": "核心逻辑",
    "AIcarusCore.IntrusiveThoughtsGenerator": "侵入思考",
    "AIcarusCore.web_searcher": "网络搜索",
    "AIcarusCore.database": "数据库",
    "AIcarusCore.config_manager": "配置管理",
    "AIcarusCore.llm.utils": "LLM工具",
    "AIcarusCore.llm.processor": "LLM处理",
    "AIcarusCore.message_processor": "消息处理",
    "AIcarusCore.ws_server": "WebSocket服务",
    "AIcarusCore.StorageManager": "存储管理",  # 新增
}

# 简化的模块配置
MODULE_LOGGER_CONFIGS = {
    "AIcarusCore.core_logic.main": HEARTFLOW_STYLE_CONFIG,
    "AIcarusCore.action_handler": TOOL_USE_STYLE_CONFIG,
    "AIcarusCore.llm.utils": MODEL_UTILS_STYLE_CONFIG,
    "AIcarusCore.llm.processor": PROCESSOR_STYLE_CONFIG,
    "AIcarusCore.StorageManager": MAIN_STYLE_CONFIG,  # 新增存储管理器样式
    "AIcarusCore.database": MAIN_STYLE_CONFIG,
    "AIcarusCore.config_manager": CONFIG_STYLE_CONFIG,
}


def get_logger(original_english_module_name: str) -> LoguruLoggerType:
    """获取模块日志器"""
    # 获取样式配置
    style_config_data = MODULE_LOGGER_CONFIGS.get(original_english_module_name)
    
    # 获取翻译后的显示名称
    display_name_for_log = MODULE_NAME_TRANSLATIONS.get(original_english_module_name, original_english_module_name)

    actual_log_config_to_use: LogConfig | None = None
    if style_config_data:
        if isinstance(style_config_data, LogConfig):
            actual_log_config_to_use = style_config_data
        elif isinstance(style_config_data, dict) and "console_format" in style_config_data:
            actual_log_config_to_use = LogConfig(
                console_format=style_config_data.get("console_format", ""),
                file_format=style_config_data.get("file_format", ""),
            )

    # 调用底层日志器
    if actual_log_config_to_use:
        return get_module_logger_from_logger_py(display_name_for_log, config=actual_log_config_to_use)
    else:
        return get_module_logger_from_logger_py(display_name_for_log)
