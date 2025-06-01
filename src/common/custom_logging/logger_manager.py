from .logger import (
    CONFIG_STYLE_CONFIG,
    HEARTFLOW_STYLE_CONFIG,
    MAIN_STYLE_CONFIG,
    MODEL_UTILS_STYLE_CONFIG,
    PROCESSOR_STYLE_CONFIG,
    TOOL_USE_STYLE_CONFIG,
    LogConfig,
    get_module_logger,
)

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


def get_logger(module_name: str) -> LogConfig:
    style_config = MODULE_LOGGER_CONFIGS.get(module_name)
    if style_config:
        log_config = LogConfig(
            console_format=style_config["console_format"],
            file_format=style_config["file_format"],
        )
        return get_module_logger(module_name, config=log_config)
    # 若无特殊样式，使用默认
    return get_module_logger(module_name)
