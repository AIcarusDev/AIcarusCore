from loguru import logger as loguru_logger  # <--- 确保这个导入或者类似 LoguruLogger 的类型导入

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

LoguruLoggerType = loguru_logger.__class__  # 获取 Loguru logger 实例的类型

# 新增这个对应表
MODULE_NAME_TRANSLATIONS = {
    "AIcarusCore.ActionHandler": "动作",
    "AIcarusCore.CoreLogic": "核心",
    "AIcarusCore.IntrusiveThoughtsGenerator": "侵入思考",
    "AIcarusCore.web_searcher": "网络搜索",
    "AIcarusCore.database": "数据库操作",
    "AIcarusCore.config_manager": "配置",
    "AIcarusCore.llm.utils": "LLM工具集",
    "AIcarusCore.llm.processor": "LLM处理器",
    "AIcarusCore.message_processor": "消息处理",
    "AIcarusCore.ws_server": "核心服务",
    "AIcarusCore.database.ArangoDBHandler": "数据库操作",
}

# 可根据实际需要补充更多模块配置
MODULE_LOGGER_CONFIGS = {
    "AIcarusCore.core_logic.main": HEARTFLOW_STYLE_CONFIG,
    "AIcarusCore.action_handler": TOOL_USE_STYLE_CONFIG,  # 假设你在 action_handler.py 中用了 get_logger("AIcarusCore.action_handler")
    "AIcarusCore.llm.utils": MODEL_UTILS_STYLE_CONFIG,  # 用于 utils_model.py
    "AIcarusCore.llm.processor": PROCESSOR_STYLE_CONFIG,  # 用于 llm_processor.py
    "AIcarusCore.database": MAIN_STYLE_CONFIG,  # 给数据库操作一个样式，例如用MAIN_STYLE
    "AIcarusCore.config_manager": CONFIG_STYLE_CONFIG,  # 给配置管理一个样式
    # ...如有更多模块，继续添加...
}


def get_logger(original_english_module_name: str) -> LoguruLoggerType:  # <--- 返回类型改成 logger 实例的类型
    # 1. 先用原始英文模块名获取样式配置 (如果这个模块有特定样式)
    style_config_data = MODULE_LOGGER_CONFIGS.get(original_english_module_name)

    # 2. 获取翻译后的显示名称，如果没在翻译表里，就用回原来的英文名
    display_name_for_log = MODULE_NAME_TRANSLATIONS.get(original_english_module_name, original_english_module_name)

    actual_log_config_to_use: LogConfig | None = None
    if style_config_data:
        if isinstance(style_config_data, LogConfig):  # 如果 MODULE_LOGGER_CONFIGS 里直接存的是 LogConfig 实例
            actual_log_config_to_use = style_config_data
        elif isinstance(style_config_data, dict) and "console_format" in style_config_data:  # 如果存的是包含格式的字典
            actual_log_config_to_use = LogConfig(
                console_format=style_config_data.get("console_format", ""),
                file_format=style_config_data.get("file_format", ""),
                # 如果你的 STYLE_CONFIG 字典里还定义了 console_level, file_level 等，也一并传入 LogConfig
                # console_level=style_config_data.get("console_level"), # 比如这样
            )
        # else: style_config_data 格式不对或不包含所需信息，actual_log_config_to_use 保持 None

    # 3. 调用 logger.py 里的 get_module_logger_from_logger_py 时，
    #    把我们想要在日志里显示的 display_name_for_log (中文名) 作为第一个参数传过去！
    if actual_log_config_to_use:
        return get_module_logger_from_logger_py(display_name_for_log, config=actual_log_config_to_use)
    else:
        # 如果没有特定样式配置，或者配置格式不对，就用显示名和默认配置（由 get_module_logger_from_logger_py 内部处理）
        return get_module_logger_from_logger_py(display_name_for_log)
