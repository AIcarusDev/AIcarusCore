# config_manager.py - 配置总指挥
import os
import sys
import traceback

from dotenv import load_dotenv

from src.common.custom_logging.logging_config import get_logger

from .aicarus_configs import AlcarusRootConfig
from .config_io import ConfigIOHandler
from .config_paths import PROJECT_ROOT
from .config_updater import perform_config_update_check, substitute_env_vars_recursive

logger = get_logger(__name__)

_loaded_settings_dict = None
_loaded_typed_settings = None
_config_checked_this_session = False


def _prompt_user_and_exit(message: str) -> None:
    """提示用户并退出程序"""
    logger.info("-" * 70)
    logger.info("重要提示:")
    logger.info(message)
    sys.exit(0)


def _perform_config_update_check(io_handler: ConfigIOHandler) -> bool:
    """检查并更新配置文件"""
    global _config_checked_this_session
    if _config_checked_this_session:
        return False
    logger.debug("开始检查和更新配置文件...")  # INFO -> DEBUG
    _config_checked_this_session = True
    return perform_config_update_check(io_handler, _prompt_user_and_exit)


def load_settings() -> dict:
    """慵懒地加载配置文件，只在需要时才真正动手"""
    global _loaded_settings_dict
    if _loaded_settings_dict is not None:
        return _loaded_settings_dict

    # 先看看有没有 .env 文件，有的话就偷偷加载一下
    # 注意：测试脚本会在更早的时候尝试加载 AIcarusCore/.env
    # 这里的逻辑是加载项目根目录的 .env，如果两者都加载，override=True 会确保后加载的生效
    # 或者，如果测试脚本已加载，这里的 verbose=True 可能会打印相关信息
    dotenv_path_project_root = PROJECT_ROOT / ".env"
    if dotenv_path_project_root.exists():
        # 如果测试脚本已经加载了 AIcarusCore/.env, 这里的 override=True 可能会覆盖掉测试脚本加载的值
        # 这取决于环境变量的实际用途和哪个 .env 文件应该优先
        # 为保持 manager 的独立性，它尝试加载它期望位置的 .env
        load_dotenv(dotenv_path=dotenv_path_project_root, override=True, verbose=True)
        logger.debug(f"ConfigManager尝试从项目根目录加载 .env 文件: {dotenv_path_project_root}")  # INFO -> DEBUG
    else:
        logger.debug(  # INFO -> DEBUG
            f"ConfigManager未在项目根目录找到 .env 文件: {dotenv_path_project_root}。将依赖已加载的环境变量或配置文件。"
        )

    io_handler = ConfigIOHandler()
    config_just_created_or_updated = _perform_config_update_check(io_handler)

    if config_just_created_or_updated:
        _prompt_user_and_exit("配置文件已被创建或更新")

    final_config_data = io_handler.load_toml_file(io_handler.runtime_path)
    if final_config_data is None:
        logger.error("从运行时路径加载TOML配置失败，将尝试使用空字典进行环境变量替换。")
        final_config_data = {}  # 使用空字典，让后续逻辑主要依赖环境变量

    # 替换环境变量占位符 (如果toml中有的话)
    substitute_env_vars_recursive(final_config_data)
    _loaded_settings_dict = final_config_data
    return _loaded_settings_dict


def get_settings() -> dict:
    """获取已加载的配置字典，懒得重复加载"""
    global _loaded_settings_dict
    if _loaded_settings_dict is None:
        return load_settings()
    return _loaded_settings_dict


def get_typed_settings() -> AlcarusRootConfig:
    """获取类型化的配置对象，让IDE知道我们在做什么"""
    global _loaded_typed_settings
    if _loaded_typed_settings is not None:
        return _loaded_typed_settings

    config_dict = get_settings()  # 这会加载toml并尝试替换占位符

    try:
        # 从字典创建初步的类型化对象
        # AlcarusRootConfig.from_dict 应该能处理 config_dict 中某些部分缺失的情况，
        # 并使用 dataclass 的 default_factory 创建默认实例。
        typed_config = AlcarusRootConfig.from_dict(config_dict)

        # --- 从环境变量覆盖数据库配置 ---
        db_settings = typed_config.database
        db_host = os.getenv("ARANGODB_HOST")
        db_user = os.getenv("ARANGODB_USER")
        db_password = os.getenv("ARANGODB_PASSWORD")
        db_name = os.getenv("ARANGODB_DATABASE")

        if db_host:
            db_settings.host = db_host
            logger.debug("已从环境变量 ARANGODB_HOST 更新数据库主机。")
        if db_user:
            db_settings.username = db_user
            logger.debug("已从环境变量 ARANGODB_USER 更新数据库用户名。")
        if db_password:
            db_settings.password = db_password
            logger.debug("已从环境变量 ARANGODB_PASSWORD 更新数据库密码。")
        if db_name:
            db_settings.database_name = db_name
            logger.debug("已从环境变量 ARANGODB_DATABASE 更新数据库名称。")
        # --- 环境变量覆盖结束 ---

        _loaded_typed_settings = typed_config
        logger.debug("配置已成功加载并转换为类型化对象。")
        return typed_config
    except Exception as e:
        logger.error(f"将配置字典转换为类型化对象或从环境变量更新时失败: {e}", exc_info=True)
        traceback.print_exc()  # 打印更详细的错误堆栈
        _prompt_user_and_exit(f"类型化配置加载或环境变量处理失败: {e}")
        # 在 _prompt_user_and_exit 中已经 sys.exit，所以这里的 raise 理论上不会执行
        # 但为了代码逻辑完整性，保留它，或者让 _prompt_user_and_exit 直接 raise
        raise SystemExit(f"配置处理失败: {e}") from e
