import logging  # 新增：导入 logging 模块，用于转换日志级别字符串
import os
from typing import Any  # 新增：导入 Union，用于类型提示
from urllib.parse import urlparse  # 用于从代理URL解析host和port

import toml
from dotenv import load_dotenv as dotenv_load

# 全局配置变量
_settings: dict[str, Any] = {}


def _setup_llm_client_environment_from_toml(parsed_toml_config: dict[str, Any]) -> None:
    """
    根据解析的TOML配置，设置LLMClient所需的环境变量。
    这个函数主要处理代理设置，并将TOML中定义的其他环境变量名（如API密钥和基础URL的变量名）
    用于后续验证这些变量是否已通过 .env 文件成功加载。
    """
    # 设置代理环境变量 (如果配置了)
    proxy_config = parsed_toml_config.get("proxy")
    if proxy_config and proxy_config.get("use_proxy"):
        http_url_str = proxy_config.get("http_proxy_url")
        if http_url_str:
            try:
                parsed_url = urlparse(http_url_str)
                if parsed_url.hostname:
                    os.environ["PROXY_HOST"] = parsed_url.hostname
                    # 调试信息，确认环境变量设置
                    print(f"调试：从TOML设置环境变量 PROXY_HOST={parsed_url.hostname}")
                if parsed_url.port:
                    os.environ["PROXY_PORT"] = str(parsed_url.port)
                    # 调试信息，确认环境变量设置
                    print(f"调试：从TOML设置环境变量 PROXY_PORT={parsed_url.port!s}")
            except Exception as e:
                print(f"错误：解析代理URL '{http_url_str}' 失败: {e}")
    else:
        # 调试信息，确认代理未启用或未配置
        print("调试：TOML中未启用代理或未配置代理URL。")

    # 验证提供商的API密钥和基础URL环境变量是否已由 python-dotenv 从 .env 加载
    providers = parsed_toml_config.get("providers", {})
    for _provider_name, provider_conf in providers.items():
        env_var_for_keys = provider_conf.get("api_keys_env_var")
        env_var_for_url = provider_conf.get("base_url_env_var")

        # API Keys 验证
        if env_var_for_keys:
            api_keys_value = os.getenv(env_var_for_keys)
            if api_keys_value:
                # python-dotenv 会将 .env 中的值加载到 os.environ
                # LLMClient 会从 os.environ 读取这个变量
                print(f"调试：环境变量 {env_var_for_keys} (值已由python-dotenv从.env加载) 已准备好供LLMClient使用。")
            else:
                # 警告信息，如果 .env 中未找到或值为空
                print(
                    f"警告：TOML指定的环境变量 {env_var_for_keys} 在.env中未找到或值为空 (即使在使用python-dotenv之后)。"
                )

        # Base URL 验证
        if env_var_for_url:
            base_url_value = os.getenv(env_var_for_url)
            if base_url_value:
                print(f"调试：环境变量 {env_var_for_url} (值已由python-dotenv从.env加载) 已准备好供LLMClient使用。")
            else:
                # 警告信息，如果 .env 中未找到或值为空
                print(
                    f"警告：TOML指定的环境变量 {env_var_for_url} 在.env中未找到或值为空 (即使在使用python-dotenv之后)。"
                )

    # 验证 LLMClient 的其他特定配置 (例如 abandoned_keys) 是否已由 python-dotenv 从 .env 加载
    llm_client_toml_settings = parsed_toml_config.get("llm_client_settings", {})
    abandoned_keys_env_var = llm_client_toml_settings.get("abandoned_keys_env_var")
    if abandoned_keys_env_var:
        abandoned_keys_value = os.getenv(abandoned_keys_env_var)
        if abandoned_keys_value:
            print(f"调试：环境变量 {abandoned_keys_env_var} (废弃密钥, 值已由python-dotenv从.env加载) 已准备好。")
        # 此处可以添加一个 else 警告，如果环境变量未找到，但根据当前逻辑，它不是关键错误


def get_logging_level_from_string(level_str: str | None, default_level: int = logging.INFO) -> int:
    """
    辅助函数：将日志级别字符串 (例如 "DEBUG", "INFO") 转换为 logging 模块的整数常量。
    如果字符串无效或未提供，则返回默认级别。

    Args:
        level_str (Union[str, None]): 从环境变量读取的日志级别字符串。
        default_level (int, optional): 如果转换失败，使用的默认日志级别。
                                       默认为 logging.INFO。

    Returns:
        int: 对应于 logging 模块的日志级别常量。
    """
    if not level_str:
        return default_level
    # 创建一个从字符串到 logging 常量的映射
    level_map: dict[str, int] = {
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARNING": logging.WARNING,
        "ERROR": logging.ERROR,
        "CRITICAL": logging.CRITICAL,
    }
    # 返回映射值，如果键不存在则返回默认值 (不区分大小写)
    return level_map.get(level_str.upper(), default_level)


def load_settings(toml_path: str = "config/config.toml", dotenv_path: str = ".env") -> dict[str, Any]:
    """
    加载应用程序的配置。
    1. 使用 python-dotenv 从指定的 .env 文件路径加载环境变量。
    2. 从 .env 文件中读取日志级别配置。
    3. 从指定的 TOML 文件路径加载主要的应用配置。
    4. 调用 _setup_llm_client_environment_from_toml 来设置或验证 LLMClient 相关的环境变量。
    5. 将日志级别配置合并到最终的配置字典中。
    6. 缓存并返回完整的配置字典。

    Args:
        toml_path (str, optional): TOML 配置文件的路径。
                                   默认为 "config/config.toml"。
        dotenv_path (str, optional): .env 文件的路径。
                                     默认为 ".env"。

    Returns:
        Dict[str, Any]: 包含所有已加载配置（包括TOML内容和日志级别）的字典。

    Raises:
        FileNotFoundError: 如果 TOML 配置文件未找到。
        Exception: 如果加载 TOML 配置文件时发生其他错误。
    """
    global _settings
    # 如果配置已加载，则直接返回，避免重复加载
    if _settings:
        return _settings

    # 步骤 1: 加载 .env 文件
    print(f"尝试从 {os.path.abspath(dotenv_path)} 加载 .env 文件 (使用 python-dotenv)...")
    # override=True 表示 .env 文件中的值会覆盖已存在的同名环境变量
    # verbose=True 会在加载时打印一些调试信息 (可选)
    loaded_dotenv = dotenv_load(dotenv_path=dotenv_path, override=True, verbose=True)
    if loaded_dotenv:
        print(f"python-dotenv 成功加载了 {dotenv_path}。")
    else:
        print(f"python-dotenv 未能从 {dotenv_path} 加载任何内容 (文件可能不存在或为空)。")

    # 步骤 2: 从 .env 文件中读取日志级别配置
    # 定义默认的日志级别，以防 .env 文件中未定义
    # APP_LOG_LEVEL 控制应用程序的整体/根日志级别
    # PYMONGO_LOG_LEVEL 控制 Pymongo 库的日志级别
    # ASYNCIO_LOG_LEVEL 控制 Asyncio 库的日志级别
    # LLM_CLIENT_LOG_LEVEL 单独控制 llm_client 模块的日志级别（如果需要比APP_LOG_LEVEL更细致的控制）
    log_levels_config: dict[str, int] = {
        "app": get_logging_level_from_string(os.getenv("APP_LOG_LEVEL"), logging.INFO),
        "pymongo": get_logging_level_from_string(os.getenv("PYMONGO_LOG_LEVEL"), logging.WARNING),
        "asyncio": get_logging_level_from_string(os.getenv("ASYNCIO_LOG_LEVEL"), logging.WARNING),
        "llm_client": get_logging_level_from_string(os.getenv("LLM_CLIENT_LOG_LEVEL"), logging.INFO),
    }
    print(
        f"从.env加载的日志级别配置: APP={logging.getLevelName(log_levels_config['app'])}, "
        f"Pymongo={logging.getLevelName(log_levels_config['pymongo'])}, "
        f"Asyncio={logging.getLevelName(log_levels_config['asyncio'])}, "
        f"LLM_Client={logging.getLevelName(log_levels_config['llm_client'])}"
    )

    # 步骤 3: 加载 TOML 配置文件
    print(f"尝试从 {os.path.abspath(toml_path)} 加载 TOML 配置文件...")
    try:
        with open(toml_path, encoding="utf-8") as f:
            parsed_toml = toml.load(f)
        print("TOML 配置文件加载成功。")
    except FileNotFoundError:
        print(f"错误：配置文件 {toml_path} 未找到。请确保它在正确的路径下。")
        raise  # 重新抛出异常，让调用者处理
    except Exception as e:
        print(f"错误：加载TOML配置文件失败：{e}")
        raise  # 重新抛出异常

    # 步骤 4: 设置或验证 LLMClient 相关的环境变量
    # 这个函数现在主要用于设置代理环境变量和验证其他变量是否已由 .env 加载
    _setup_llm_client_environment_from_toml(parsed_toml)

    # 步骤 5: 将日志级别配置合并到最终的配置中
    # 我们在 parsed_toml 字典中添加一个新的键 'logging_levels' 来存储这些级别
    parsed_toml["logging_levels"] = log_levels_config

    # 步骤 6: 缓存并返回配置
    _settings = parsed_toml
    return _settings


def get_settings() -> dict[str, Any]:
    """
    获取已加载的配置。
    如果配置尚未加载，则调用 load_settings() 进行加载。
    这是一个单例模式的简单实现，确保配置只加载一次。

    Returns:
        Dict[str, Any]: 应用程序的配置字典。
    """
    if not _settings:
        # 如果 _settings 为空，则调用 load_settings() 来加载配置
        # load_settings() 会处理 .env 和 TOML 文件，并填充 _settings
        return load_settings()
    return _settings


# 当此脚本作为主模块直接运行时，执行以下测试代码
if __name__ == "__main__":
    print("--- 测试 config_loader.py (使用 python-dotenv 和 日志级别配置) ---")
    # 获取当前文件所在目录的父目录，即项目根目录
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    # 构建 .env 和 config.toml 文件的测试路径
    test_dotenv_path = os.path.join(project_root, ".env")
    test_toml_path = os.path.join(project_root, "config", "config.toml")

    print(f"测试用.env路径: {test_dotenv_path}")
    print(f"测试用TOML路径: {test_toml_path}")

    # 调用 load_settings 进行测试
    # 确保你的项目根目录下有一个 .env 文件，并且其中可以包含类似如下的日志级别设置：
    # APP_LOG_LEVEL="DEBUG"
    # PYMONGO_LOG_LEVEL="INFO"
    test_settings = load_settings(dotenv_path=test_dotenv_path, toml_path=test_toml_path)

    print("\n--- 加载的TOML配置内容 (部分) ---")
    # 打印一部分TOML配置内容以供验证
    if "providers" in test_settings and "gemini" in test_settings["providers"]:
        print(f"Gemini Provider Config: {test_settings['providers']['gemini']}")

    print("\n--- 加载的日志级别配置 (来自.env) ---")
    # 打印从 .env 加载并处理后的日志级别
    if "logging_levels" in test_settings:
        logging_levels = test_settings["logging_levels"]
        print(
            f"APP 日志级别 (整数值): {logging_levels.get('app')}, "
            f"(名称: {logging.getLevelName(logging_levels.get('app', logging.INFO))})"
        )
        print(
            f"Pymongo 日志级别 (整数值): {logging_levels.get('pymongo')}, "
            f"(名称: {logging.getLevelName(logging_levels.get('pymongo', logging.WARNING))})"
        )
        print(
            f"Asyncio 日志级别 (整数值): {logging_levels.get('asyncio')}, "
            f"(名称: {logging.getLevelName(logging_levels.get('asyncio', logging.WARNING))})"
        )
        print(
            f"LLM Client 日志级别 (整数值): {logging_levels.get('llm_client')}, "
            f"(名称: {logging.getLevelName(logging_levels.get('llm_client', logging.INFO))})"
        )

    print("\n--- 检查环境变量 (应由python-dotenv加载) ---")
    # 打印一些关键环境变量的值，以验证 .env 加载是否成功
    print(f"GEMINI_API_KEYS: {os.getenv('GEMINI_API_KEYS')}")
    print(f"GEMINI_BASE_URL: {os.getenv('GEMINI_BASE_URL')}")
    print(f"MONGODB_CONNECTION_STRING: {os.getenv('MONGODB_CONNECTION_STRING')}")
    print(
        f"PROXY_HOST (来自TOML->env): {os.getenv('PROXY_HOST')}"
    )  # 这个是由 _setup_llm_client_environment_from_toml 设置的
    print(f"PROXY_PORT (来自TOML->env): {os.getenv('PROXY_PORT')}")  # 这个也是
    print(f"ABANDONED_KEYS: {os.getenv('ABANDONED_KEYS')}")
    print("--- 测试结束 ---")
