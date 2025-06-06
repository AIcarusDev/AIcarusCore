"""
全局配置实例
提供一个全局可访问的配置对象，简化配置使用
"""

from ..common.custom_logging.logger_manager import get_logger
from .aicarus_configs import AlcarusRootConfig
from .config_manager import get_typed_settings

logger = get_logger("AIcarusCore.GlobalConfig")

# 全局配置实例
_global_config: AlcarusRootConfig | None = None


def get_global_config() -> AlcarusRootConfig:
    """获取全局配置实例"""
    global _global_config
    if _global_config is None:
        logger.info("正在初始化全局配置...")
        _global_config = get_typed_settings()
        logger.info("全局配置初始化完成")
    return _global_config


def reset_global_config() -> None:
    """重置全局配置（主要用于测试）"""
    global _global_config
    _global_config = None
    logger.info("全局配置已重置")


# 直接导出的全局配置实例
global_config = get_global_config()
