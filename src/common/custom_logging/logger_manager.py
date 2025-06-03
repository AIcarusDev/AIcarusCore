from loguru import logger as loguru_logger

from .logger import (
    CONFIG_STYLE_CONFIG,
    HEARTFLOW_STYLE_CONFIG,
    MAIN_STYLE_CONFIG,
    MODEL_UTILS_STYLE_CONFIG,
    PROCESSOR_STYLE_CONFIG,
    TOOL_USE_STYLE_CONFIG,
    LogConfig,
    # 新增一个插件专属的日志样式，你也可以复用现有的
    BASE_TOOL_STYLE_CONFIG, # 假设这个是适合插件的通用工具样式
)
from .logger import (
    get_module_logger as get_module_logger_from_logger_py,
)

LoguruLoggerType = loguru_logger.__class__

MODULE_NAME_TRANSLATIONS = {
    "AIcarusCore.main": "核心主入口", # 新增翻译
    "AIcarusCore.ActionHandler": "动作",
    "AIcarusCore.CoreLogic": "核心",
    "AIcarusCore.core_logic.consciousness_loop": "主思维循环",
    "AIcarusCore.core_logic.thought_builder": "思考构建",
    "AIcarusCore.core_logic.thought_processor": "思考处理",
    "AIcarusCore.plugins.IntrusiveThoughtsGenerator": "侵入思考插件", # 更改翻译
    "AIcarusCore.web_searcher": "网络搜索",
    "AIcarusCore.database": "数据库操作",
    "AIcarusCore.config_manager": "配置",
    "AIcarusCore.llm.utils": "LLM工具集",
    "AIcarusCore.llm.processor": "LLM处理器",
    "AIcarusCore.message_processor": "消息处理",
    "AIcarusCore.ws_server": "核心服务",
    "AIcarusCore.database.ArangoDBHandler": "数据库操作",
    "AIcarusCore.sub_consciousness.ChatSessionHandler": "子思维会话",
}

MODULE_LOGGER_CONFIGS = {
    "AIcarusCore.main": MAIN_STYLE_CONFIG, # 新增主入口的日志样式
    "AIcarusCore.core_logic.consciousness_loop": HEARTFLOW_STYLE_CONFIG,
    "AIcarusCore.core_logic.thought_builder": MAIN_STYLE_CONFIG,
    "AIcarusCore.core_logic.thought_processor": PROCESSOR_STYLE_CONFIG,
    "AIcarusCore.action_handler": TOOL_USE_STYLE_CONFIG,
    "AIcarusCore.llm.utils": MODEL_UTILS_STYLE_CONFIG,
    "AIcarusCore.llm.processor": PROCESSOR_STYLE_CONFIG,
    "AIcarusCore.database": MAIN_STYLE_CONFIG,
    "AIcarusCore.config_manager": CONFIG_STYLE_CONFIG,
    "AIcarusCore.sub_consciousness.ChatSessionHandler": MAIN_STYLE_CONFIG,
    "AIcarusCore.plugins.IntrusiveThoughtsGenerator": BASE_TOOL_STYLE_CONFIG, # 更改日志样式
}

def get_logger(original_english_module_name: str) -> LoguruLoggerType:
    style_config_data = MODULE_LOGGER_CONFIGS.get(original_english_module_name)

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

    if actual_log_config_to_use:
        return get_module_logger_from_logger_py(display_name_for_log, config=actual_log_config_to_use)
    else:
        return get_module_logger_from_logger_py(display_name_for_log)