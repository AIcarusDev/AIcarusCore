from .logger import get_module_logger, LogConfig
from .logger import (
    BACKGROUND_TASKS_STYLE_CONFIG,
    MAIN_STYLE_CONFIG,
    MEMORY_STYLE_CONFIG,
    PFC_STYLE_CONFIG,
    MOOD_STYLE_CONFIG,
    TOOL_USE_STYLE_CONFIG,
    RELATION_STYLE_CONFIG,
    CONFIG_STYLE_CONFIG,
    HEARTFLOW_STYLE_CONFIG,
    SCHEDULE_STYLE_CONFIG,
    LLM_STYLE_CONFIG,
    CHAT_STYLE_CONFIG,
    EMOJI_STYLE_CONFIG,
    SUB_HEARTFLOW_STYLE_CONFIG,
    SUB_HEARTFLOW_MIND_STYLE_CONFIG,
    SUBHEARTFLOW_MANAGER_STYLE_CONFIG,
    BASE_TOOL_STYLE_CONFIG,
    CHAT_STREAM_STYLE_CONFIG,
    PERSON_INFO_STYLE_CONFIG,
    WILLING_STYLE_CONFIG,
    PFC_ACTION_PLANNER_STYLE_CONFIG,
    MAI_STATE_CONFIG,
    LPMM_STYLE_CONFIG,
    HFC_STYLE_CONFIG,
    OBSERVATION_STYLE_CONFIG,
    PLANNER_STYLE_CONFIG,
    PROCESSOR_STYLE_CONFIG,
    ACTION_TAKEN_STYLE_CONFIG,
    TIANYI_STYLE_CONFIG,
    REMOTE_STYLE_CONFIG,
    TOPIC_STYLE_CONFIG,
    SENDER_STYLE_CONFIG,
    CONFIRM_STYLE_CONFIG,
    MODEL_UTILS_STYLE_CONFIG,
    PROMPT_STYLE_CONFIG,
    CHANGE_MOOD_TOOL_STYLE_CONFIG,
    CHANGE_RELATIONSHIP_TOOL_STYLE_CONFIG,
    GET_KNOWLEDGE_TOOL_STYLE_CONFIG,
    GET_TIME_DATE_TOOL_STYLE_CONFIG,
    LPMM_GET_KNOWLEDGE_TOOL_STYLE_CONFIG,
    MESSAGE_BUFFER_STYLE_CONFIG,
    CHAT_MESSAGE_STYLE_CONFIG,
    CHAT_IMAGE_STYLE_CONFIG,
    INIT_STYLE_CONFIG,
    INTEREST_CHAT_STYLE_CONFIG,
    API_SERVER_STYLE_CONFIG,
)

# 可根据实际需要补充更多模块配置
MODULE_LOGGER_CONFIGS = {
    "AIcarusCore.core_logic.main": HEARTFLOW_STYLE_CONFIG,
    "AIcarusCore.action_handler": TOOL_USE_STYLE_CONFIG, # 假设你在 action_handler.py 中用了 get_logger("AIcarusCore.action_handler")
    "AIcarusCore.llm.utils": MODEL_UTILS_STYLE_CONFIG, # 用于 utils_model.py
    "AIcarusCore.llm.processor": PROCESSOR_STYLE_CONFIG, # 用于 llm_processor.py
    "AIcarusCore.database": MAIN_STYLE_CONFIG, # 给数据库操作一个样式，例如用MAIN_STYLE
    "AIcarusCore.config_manager": CONFIG_STYLE_CONFIG, # 给配置管理一个样式
    # ...如有更多模块，继续添加...
}


def get_logger(module_name: str):
    style_config = MODULE_LOGGER_CONFIGS.get(module_name)
    if style_config:
        log_config = LogConfig(
            console_format=style_config["console_format"],
            file_format=style_config["file_format"],
        )
        return get_module_logger(module_name, config=log_config)
    # 若无特殊样式，使用默认
    return get_module_logger(module_name)
