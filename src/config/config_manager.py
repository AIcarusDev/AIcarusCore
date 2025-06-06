# -*- coding: utf-8 -*-
# config_manager.py - 配置总指挥
import sys
import traceback
from typing import Any
from dotenv import load_dotenv
from src.common.custom_logging.logger_manager import get_logger
from .aicarus_configs import AlcarusRootConfig
from .config_paths import PROJECT_ROOT, RUNTIME_CONFIG_DIR, ACTUAL_CONFIG_FILENAME
from .config_io import ConfigIOHandler
from .config_updater import perform_config_update_check, substitute_env_vars_recursive

logger = get_logger("AIcarusCore.config_manager")

_loaded_settings_dict = None
_loaded_typed_settings = None
_config_checked_this_session = False

def _prompt_user_and_exit(message):
    """提示用户并退出程序"""
    logger.info("-" * 70)
    logger.info("重要提示:")
    logger.info(message)
    sys.exit(0)

def _perform_config_update_check(io_handler):
    """检查并更新配置文件"""
    global _config_checked_this_session
    if _config_checked_this_session:
        return False
    logger.info("开始检查和更新配置文件...")
    _config_checked_this_session = True
    return perform_config_update_check(io_handler, _prompt_user_and_exit)

def load_settings():
    """慵懒地加载配置文件，只在需要时才真正动手"""
    global _loaded_settings_dict
    if _loaded_settings_dict is not None:
        return _loaded_settings_dict
    
    # 先看看有没有 .env 文件，有的话就偷偷加载一下
    dotenv_path = PROJECT_ROOT / ".env"
    if dotenv_path.exists():
        load_dotenv(dotenv_path=dotenv_path, override=True, verbose=True)
    
    # 创建IO处理器，开始干活
    io_handler = ConfigIOHandler()
    config_just_created_or_updated = _perform_config_update_check(io_handler)
    
    if config_just_created_or_updated:
        _prompt_user_and_exit("配置文件已被创建或更新")
    
    # 加载最终的配置数据
    final_config_data = io_handler.load_toml_file(io_handler.runtime_path)
    if final_config_data is None:
        _prompt_user_and_exit("配置文件加载失败")
        return {}
    
    # 替换环境变量
    substitute_env_vars_recursive(final_config_data)
    _loaded_settings_dict = final_config_data
    return _loaded_settings_dict

def get_settings():
    """获取已加载的配置字典，懒得重复加载"""
    global _loaded_settings_dict
    if _loaded_settings_dict is None:
        return load_settings()
    return _loaded_settings_dict

def get_typed_settings():
    """获取类型化的配置对象，让IDE知道我们在做什么"""
    global _loaded_typed_settings
    if _loaded_typed_settings is not None:
        return _loaded_typed_settings
    
    config_dict = get_settings()
    try:
        typed_config = AlcarusRootConfig.from_dict(config_dict)
        _loaded_typed_settings = typed_config
        logger.info("配置已成功加载并转换为类型化对象")
        return typed_config
    except Exception as e:
        logger.info(f"配置转换失败: {e}")
        traceback.print_exc()
        _prompt_user_and_exit("类型化配置加载失败")
        raise
