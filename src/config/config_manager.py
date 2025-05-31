import os
import shutil
import sys
from datetime import datetime  # 用于生成时间戳
from pathlib import Path  # 用于面向对象的路径操作
from typing import Any  # 类型提示
from src.common.custom_logging.logger_manager import get_logger

import tomlkit  # 用于处理 TOML 文件，保留注释和格式
from dotenv import load_dotenv  # 用于加载 .env 文件

# 从同级目录的 alcarus_configs.py 导入类型化的配置根类
from .alcarus_configs import AlcarusRootConfig

logger = get_logger("AIcarusCore.config_manager")

# --- 全局模块级变量 ---

# 用于缓存加载的原始配置字典 (避免重复从文件加载和处理)
_loaded_settings_dict: dict[str, Any] | None = None
# 用于缓存加载的类型化配置对象
_loaded_typed_settings: AlcarusRootConfig | None = None
# 标记本会话是否已执行过配置检查和更新流程，避免重复执行
_config_checked_this_session: bool = False

# --- 路径和版本常量定义 ---
PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent.parent

TEMPLATE_DIR: Path = PROJECT_ROOT / "template"  # 模板文件存放目录
RUNTIME_CONFIG_DIR: Path = PROJECT_ROOT / "config"  # 运行时实际使用的配置文件目录
OLD_CONFIG_BACKUP_DIR: Path = RUNTIME_CONFIG_DIR / "old"  # 旧配置文件备份目录

CONFIG_TEMPLATE_FILENAME: str = "config_template.toml"  # 模板配置文件名
ACTUAL_CONFIG_FILENAME: str = "config.toml"  # 运行时实际使用的配置文件名

# Alcarus 代码期望的配置文件结构版本号。
# 当 template/settings_template.toml 的结构发生重大变化时，应提升此版本号，
# 并确保 settings_template.toml 文件内的 [inner].version 也同步更新。
EXPECTED_CONFIG_VERSION: str = "0.0.1"  # 请根据您的实际模板版本调整


# --- 辅助函数：用户交互与程序退出 ---


def _prompt_user_and_exit(message: str) -> None:
    """
    向用户显示重要提示信息，并正常退出程序。
    用于在配置文件首次创建或更新后，引导用户检查配置。
    """
    logger.info("-" * 70)  # 使用更宽的分隔线
    logger.info("重要提示:")
    logger.info(message)
    logger.info(f"请检查并根据需要修改位于 '{RUNTIME_CONFIG_DIR / ACTUAL_CONFIG_FILENAME}' 的配置文件。")
    logger.info("特别是涉及到 API 密钥等敏感信息的配置项，它们通常需要您在 .env 文件中正确设置对应的环境变量。")
    logger.info("完成配置后，请重新运行程序。")
    logger.info("-" * 70)
    sys.exit(0)  # 正常退出程序，返回码 0


# --- 类：配置文件输入输出处理器 ---


class ConfigIOHandler:
    """
    封装与配置文件相关的基本文件操作和 TOML 处理。
    """

    def __init__(
        self, template_dir: Path, runtime_dir: Path, backup_dir: Path, template_filename: str, runtime_filename: str
    ) -> None:
        self.template_path: Path = template_dir / template_filename
        self.runtime_path: Path = runtime_dir / runtime_filename
        self.backup_dir: Path = backup_dir
        self.runtime_filename: str = runtime_filename  # 用于生成备份文件名

        self._ensure_directories_exist()

    def _ensure_directories_exist(self) -> None:
        """确保运行时配置目录和备份目录存在。"""
        self.runtime_path.parent.mkdir(exist_ok=True)  # 确保运行时配置文件的父目录存在
        self.backup_dir.mkdir(exist_ok=True)  # 确保备份目录存在

    def template_exists(self) -> bool:
        """检查模板配置文件是否存在。"""
        return self.template_path.exists()

    def runtime_config_exists(self) -> bool:
        """检查运行时配置文件是否存在。"""
        return self.runtime_path.exists()

    def load_toml_file(self, file_path: Path) -> tomlkit.TOMLDocument | None:
        """从指定路径加载 TOML 文件，处理可能的解析错误。"""
        if not file_path.exists():
            return None
        try:
            with open(file_path, encoding="utf-8") as f:
                return tomlkit.load(f)
        except tomlkit.exceptions.TOMLKitError as e:
            logger.info(f"错误：解析 TOML 文件 '{file_path}' 失败：{e}")
            return None  # 返回 None 表示加载或解析失败
        except Exception as e:
            logger.info(f"错误：读取文件 '{file_path}' 时发生未知错误：{e}")
            return None

    def save_toml_file(self, file_path: Path, data: tomlkit.TOMLDocument) -> bool:
        """将 TOML 数据保存到指定路径。"""
        try:
            with open(file_path, "w", encoding="utf-8") as f:
                tomlkit.dump(data, f)
            return True
        except Exception as e:
            logger.info(f"错误：保存 TOML 文件 '{file_path}' 失败：{e}")
            return False

    def copy_template_to_runtime(self) -> bool:
        """从模板复制配置文件到运行时位置。"""
        if not self.template_exists():
            logger.info(f"错误：模板文件 '{self.template_path}' 不存在，无法复制。")
            return False
        try:
            shutil.copy2(self.template_path, self.runtime_path)
            logger.info(f"已从模板 '{self.template_path}' 复制到运行时位置 '{self.runtime_path}'。")
            return True
        except Exception as e:
            logger.info(f"错误：复制模板文件失败：{e}")
            return False

    def backup_runtime_config(self, prefix: str = "") -> Path | None:
        """备份当前的运行时配置文件到备份目录，文件名包含时间戳和可选前缀。"""
        if not self.runtime_config_exists():
            logger.info(f"信息：运行时配置文件 '{self.runtime_path}' 不存在，无需备份。")
            return None
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_filename = f"{prefix}{self.runtime_filename}_{timestamp}.toml"
        backup_file_path = self.backup_dir / backup_filename
        try:
            shutil.move(self.runtime_path, backup_file_path)
            logger.info(f"已备份运行时配置文件 '{self.runtime_path}' 到 '{backup_file_path}'。")
            return backup_file_path
        except Exception as e:
            logger.info(f"错误：备份运行时配置文件失败：{e}")
            return None


# --- 辅助函数：配置合并与环境变量替换 ---


def _merge_configs_recursive(
    target_config: dict[str, Any] | tomlkit.items.Table,
    old_values_source: dict[str, Any] | tomlkit.items.Table,
) -> None:
    """
    递归地将旧配置 (old_values_source) 中的值合并到新配置 (target_config) 中。
    主要用于在版本更新时，保留用户的自定义设置。
    """
    for key, old_value in old_values_source.items():
        # 特殊处理 'inner' 表，通常不进行深层合并，特别是版本号
        if key == "inner":
            if (
                isinstance(old_value, dict | tomlkit.items.Table)
                and "version" in old_value
                and "inner" in target_config
                and isinstance(target_config.get("inner"), dict | tomlkit.items.Table)
                and "version" in target_config["inner"]
            ):  # type: ignore
                # 打印版本信息，但版本号最终由新模板决定
                logger.info(
                    f"  信息：保留新配置的版本号: {target_config['inner']['version']} (旧配置版本是: {old_value['version']})"
                )  # type: ignore
            continue  # 跳过 'inner' 表的进一步合并

        if key in target_config:
            target_value = target_config[key]
            # 如果键对应的值在新旧配置中都是字典/表类型，则递归合并
            if isinstance(old_value, dict | tomlkit.items.Table) and isinstance(
                target_value, dict | tomlkit.items.Table
            ):
                _merge_configs_recursive(target_value, old_value)
            # 如果都不是字典/表类型 (即简单值或数组)，则尝试用旧值覆盖新模板中的值
            elif not isinstance(old_value, dict | tomlkit.items.Table) and not isinstance(
                target_value, dict | tomlkit.items.Table
            ):
                try:
                    # 使用 tomlkit.item 来创建/更新值，以更好地保留TOML类型和格式
                    if isinstance(old_value, list):
                        # 对于列表，创建一个新的TOML数组，并逐项用tomlkit.item处理
                        new_array = tomlkit.array()
                        for item in old_value:
                            new_array.append(tomlkit.item(item))
                        target_config[key] = new_array
                    else:
                        # 对于其他简单类型 (包括None)，直接用tomlkit.item处理
                        target_config[key] = tomlkit.item(old_value)
                    # logger.info(f"  合并值: [{key}] = {str(old_value)[:50]}{'...' if len(str(old_value)) > 50 else ''}") # 日志可以按需开启
                except Exception as e:
                    logger.info(f"  警告：合并键 '{key}' 的值 '{str(old_value)[:50]}' 时发生错误: {e}。将保留模板中的值。")
            else:
                # 类型不匹配 (例如，旧的是简单值，新的是表)，通常保留新模板的结构和值
                logger.info(f"  信息：键 '{key}' 在新旧配置中类型不匹配，将保留新模板的结构/值。")
        else:
            # 如果旧配置中的键在新模板中不存在，说明该配置项可能已被废弃，忽略它
            logger.info(f"  信息：旧配置中的键 '{key}' 在新模板中不存在，已忽略。")


def _substitute_env_vars_recursive(
    config_node: dict[str, Any] | list[Any] | tomlkit.items.Table | tomlkit.items.Array,
) -> None:
    """
    递归地替换配置节点中以 "ENV_" 开头的字符串占位符为对应的环境变量值。
    支持字典和列表的递归处理。
    """
    if isinstance(config_node, dict | tomlkit.items.Table):  # 处理字典或TOML表
        # 使用 list(config_node.items()) 是为了允许在迭代过程中修改字典/表
        for key, value in list(config_node.items()):
            if isinstance(value, str) and value.startswith("ENV_"):
                env_var_name = value[4:]  # 提取环境变量名
                env_value = os.getenv(env_var_name)  # 从环境中获取值

                if env_value is not None:
                    # 尝试进行类型转换
                    processed_value: Any
                    if env_value.lower() == "true":
                        processed_value = True
                    elif env_value.lower() == "false":
                        processed_value = False
                    else:
                        try:
                            processed_value = int(env_value)  # 尝试转为整数
                        except ValueError:
                            try:
                                processed_value = float(env_value)  # 尝试转为浮点数
                            except ValueError:
                                # 如果环境变量的值看起来像TOML列表或内联表，尝试用tomlkit解析
                                if (env_value.startswith("[") and env_value.endswith("]")) or (
                                    env_value.startswith("{") and env_value.endswith("}")
                                ):
                                    try:
                                        # 构造一个临时的TOML片段进行解析
                                        parsed_item = tomlkit.parse(f"temp_key = {env_value}")
                                        processed_value = parsed_item["temp_key"]
                                    except Exception:  # 解析失败则保持为原始字符串
                                        processed_value = env_value
                                else:
                                    processed_value = env_value  # 其他情况保持为原始字符串
                    config_node[key] = processed_value  # 使用处理后的值替换占位符
                    # logger.info(f"  配置值 '{key}' 从环境变量 '{env_var_name}' 加载。") # 日志可以按需开启
                else:
                    logger.info(
                        f"  警告：配置请求环境变量 '{env_var_name}' (用于键 '{key}'), 但该变量未在环境中设置。将使用原始占位符值 '{value}'。"
                    )
            elif isinstance(value, dict | tomlkit.items.Table | list | tomlkit.items.Array):
                # 如果值是嵌套的字典/表或列表/数组，则递归处理
                _substitute_env_vars_recursive(value)
    elif isinstance(config_node, list | tomlkit.items.Array):  # 处理列表或TOML数组
        for i, item in enumerate(config_node):
            if isinstance(item, str) and item.startswith("ENV_"):
                env_var_name = item[4:]
                env_value = os.getenv(env_var_name)
                if env_value is not None:
                    # 对列表中的环境变量占位符也进行类型转换
                    processed_value: Any
                    if env_value.lower() == "true":
                        processed_value = True
                    elif env_value.lower() == "false":
                        processed_value = False
                    else:
                        try:
                            processed_value = int(env_value)
                        except ValueError:
                            try:
                                processed_value = float(env_value)
                            except ValueError:
                                processed_value = env_value
                    config_node[i] = processed_value  # type: ignore # 更新列表中的元素
                    # logger.info(f"  配置列表元素从环境变量 '{env_var_name}' 加载。")
                else:
                    logger.info(f"  警告：配置列表中的元素请求环境变量 '{env_var_name}', 但该变量未设置。将保留原始占位符。")
            elif isinstance(item, dict | tomlkit.items.Table | list | tomlkit.items.Array):
                # 如果列表项是嵌套的字典/表或列表/数组，则递归处理
                _substitute_env_vars_recursive(item)


# --- 核心配置更新与加载逻辑 ---


def _perform_config_update_check(io_handler: ConfigIOHandler) -> bool:
    """
    执行配置文件的核心检查和更新流程。
    返回一个布尔值，指示配置是否是新创建或刚刚被更新的。
    """
    global _config_checked_this_session  # 引用全局会话检查标志
    if _config_checked_this_session:  # 如果本会话已检查过
        return False  # 则不再执行更新逻辑，直接返回False

    logger.info("开始检查和更新配置文件...")
    _config_checked_this_session = True  # 标记本会话已执行过检查

    config_was_created_or_updated: bool = False  # 初始化标志

    # 1. 检查模板文件是否存在
    if not io_handler.template_exists():
        # 这是严重错误，没有模板无法进行任何操作
        message = f"配置文件模板 '{io_handler.template_path}' 未找到！程序无法继续。"
        logger.info(f"错误：{message}")
        # 不直接调用 _prompt_user_and_exit，因为这更像是一个部署/开发环境问题
        raise FileNotFoundError(message)

    # 2. 处理运行时配置文件不存在或损坏的情况
    if not io_handler.runtime_config_exists():
        logger.info(f"运行时配置文件 '{io_handler.runtime_path}' 不存在，将从模板创建。")
        if io_handler.copy_template_to_runtime():
            config_was_created_or_updated = True  # 标记为新创建
            logger.info("已成功创建新的运行时配置文件。")
        else:
            # 如果复制失败，这是严重问题
            message = f"从模板创建运行时配置文件 '{io_handler.runtime_path}' 失败！请检查权限和路径。"
            logger.info(f"严重错误：{message}")
            _prompt_user_and_exit(message)  # 提示用户并退出
        return config_was_created_or_updated  # 返回 True，因为需要用户检查

    # 运行时配置文件存在，加载它
    logger.info(f"发现现有运行时配置文件: '{io_handler.runtime_path}'")
    actual_config = io_handler.load_toml_file(io_handler.runtime_path)

    if actual_config is None:  # 配置文件存在但无法加载 (损坏)
        logger.info(f"错误：无法解析现有的运行时配置文件 '{io_handler.runtime_path}'。它可能已损坏。")
        io_handler.backup_runtime_config(prefix="broken_")  # 备份损坏的配置
        if io_handler.copy_template_to_runtime():  # 从模板重新创建
            config_was_created_or_updated = True  # 标记为已更新 (通过重新创建)
            logger.info("已从模板重新创建配置文件。您可能需要从备份中恢复您的旧设置。")
        else:
            message = f"从模板重新创建损坏的配置文件 '{io_handler.runtime_path}' 失败！"
            logger.info(f"严重错误：{message}")
            _prompt_user_and_exit(message)
        return config_was_created_or_updated  # 返回 True

    # 3. 加载模板配置以进行版本比较
    template_config = io_handler.load_toml_file(io_handler.template_path)
    if template_config is None:  # 模板文件此时应该存在且可读，如果不是，则是严重问题
        message = f"无法加载模板配置文件 '{io_handler.template_path}' 进行版本比较！"
        logger.info(f"严重错误：{message}")
        raise RuntimeError(message)  # 抛出运行时错误

    # 获取版本号
    current_template_version = str(template_config.get("inner", {}).get("version", EXPECTED_CONFIG_VERSION))
    actual_runtime_version = str(actual_config.get("inner", {}).get("version", "未知"))  # 如果没有version，设为未知

    # 4. 版本比较和更新处理
    if actual_runtime_version == current_template_version:
        logger.info(
            f"运行时配置文件版本 (v{actual_runtime_version}) 与模板版本 (v{current_template_version}) 相同，无需更新。"
        )
        return False  # 版本相同，未发生更新

    logger.info(f"运行时配置文件版本 (v{actual_runtime_version}) 与模板版本 (v{current_template_version}) 不同。需要更新...")

    # 备份当前运行时配置
    if io_handler.backup_runtime_config(prefix="pre_update_"):
        # 从模板复制新的基础
        if io_handler.copy_template_to_runtime():
            # 加载这个新的基础配置 (即当前模板的内容)
            new_config_base = io_handler.load_toml_file(io_handler.runtime_path)
            if new_config_base:
                logger.info("开始将旧配置值合并到新模板结构中...")
                _merge_configs_recursive(new_config_base, actual_config)  # actual_config 是旧的、已加载的配置内容
                if io_handler.save_toml_file(io_handler.runtime_path, new_config_base):
                    logger.info(f"配置文件已成功更新并合并旧值: '{io_handler.runtime_path}'")
                    config_was_created_or_updated = True  # 标记为已更新
                else:
                    logger.info(f"严重错误：保存合并后的配置文件 '{io_handler.runtime_path}' 失败！程序可能无法按预期运行。")
                    # 此时可以考虑是否要恢复备份，或者提示用户手动处理
            else:
                logger.info(f"严重错误：无法加载新复制的模板文件 '{io_handler.runtime_path}' 进行合并！")
        else:
            logger.info("严重错误：从模板复制新的配置文件基础失败！无法完成更新。")
    else:
        logger.info("严重错误：备份旧的运行时配置文件失败！无法安全地进行更新。")

    return config_was_created_or_updated


# --- 公开的配置加载接口 ---


def load_settings() -> dict[str, Any]:
    """
    加载最终的运行时配置字典。
    此函数会处理 .env 文件加载、配置文件的创建/版本更新检查。
    如果配置文件是首次创建或刚刚更新，则会提示用户检查配置并退出程序。
    返回加载并处理（环境变量替换后）的配置字典。
    """
    global _loaded_settings_dict  # 引用全局缓存变量

    if _loaded_settings_dict is not None:  # 如果已有缓存
        return _loaded_settings_dict  # 直接返回缓存

    # 1. 加载 .env 文件 (应在所有配置读取之前)
    dotenv_path = PROJECT_ROOT / ".env"
    if dotenv_path.exists():
        if load_dotenv(dotenv_path=dotenv_path, override=True, verbose=True):
            logger.info(f".env 文件已成功加载: '{dotenv_path}'")
        else:
            logger.info(f"信息：从 '{dotenv_path}' 未加载任何新的环境变量 (可能为空或所有变量已存在)。")
    else:
        logger.info(f"警告：.env 文件未在 '{dotenv_path}' 找到。某些配置可能依赖于此文件中的环境变量。")

    # 2. 初始化 IO 处理器并执行配置更新检查
    io_handler = ConfigIOHandler(
        template_dir=TEMPLATE_DIR,
        runtime_dir=RUNTIME_CONFIG_DIR,
        backup_dir=OLD_CONFIG_BACKUP_DIR,
        template_filename=CONFIG_TEMPLATE_FILENAME,
        runtime_filename=ACTUAL_CONFIG_FILENAME,
    )
    config_just_created_or_updated = _perform_config_update_check(io_handler)

    # 3. 如果配置是新创建或刚被更新的，则提示用户并退出
    if config_just_created_or_updated:
        # 在 _perform_config_update_check 内部，如果发生严重错误导致无法创建/更新，
        # 或者成功创建/更新后，它已经处理了提示和退出的逻辑（通过 _prompt_user_and_exit）。
        # 但为了双重保险和明确流程，这里可以再加一个判断。
        # 不过，如果 _perform_config_update_check 内部已经退出了，这里就不会执行。
        # 因此，主要依赖 _perform_config_update_check 内部的 _prompt_user_and_exit。
        # 为了让 load_settings 决定是否退出，我们将 _prompt_user_and_exit 从 _perform_config_update_check 中移到这里。

        # 重新审视：_perform_config_update_check 应该只负责更新和返回状态。
        # load_settings 根据这个状态决定是否提示和退出。
        # (当前代码中 _perform_config_update_check 内部在某些严重错误时会调用 _prompt_user_and_exit)
        # 为了更清晰，让 _perform_config_update_check 只在“成功创建/更新”后返回True，
        # 其他错误则抛出异常或返回False，由 load_settings 统一处理退出。
        # 但为了最小化改动，我们暂时保持现有逻辑，即 _perform_config_update_check 在某些情况下会自己退出。
        # 如果它返回 True，意味着它认为应该提示用户。
        _prompt_user_and_exit(f"配置文件 '{io_handler.runtime_path}' 已被创建或更新至新版本。")

    # 4. 加载最终的运行时配置文件
    final_config_data = io_handler.load_toml_file(io_handler.runtime_path)

    if final_config_data is None:
        # 如果到这里配置文件仍然无法加载，说明存在严重问题
        message = f"最终的运行时配置文件 '{io_handler.runtime_path}' 未找到或无法解析！请检查之前的错误信息。"
        logger.info(f"严重错误：{message}")
        _prompt_user_and_exit(message)  # 提示用户并退出
        return {}  # 理论上不会执行到这里，因为 _prompt_user_and_exit 会退出

    logger.info(f"已成功加载运行时配置文件: '{io_handler.runtime_path}'")

    # 5. 替换配置中的环境变量占位符
    logger.info("开始替换配置文件中的环境变量占位符...")
    _substitute_env_vars_recursive(final_config_data)  # type: ignore
    logger.info("环境变量占位符替换完成。")

    _loaded_settings_dict = final_config_data  # 缓存加载的字典
    return _loaded_settings_dict


def get_settings() -> dict[str, Any]:
    """
    获取已加载的配置字典。
    如果配置尚未加载，则调用 load_settings() 进行加载。
    """
    global _loaded_settings_dict  # 确保引用的是全局缓存
    if _loaded_settings_dict is None:
        return load_settings()  # load_settings() 会填充 _loaded_settings_dict
    return _loaded_settings_dict


def get_typed_settings() -> AlcarusRootConfig:
    """
    加载并返回类型化的 Alcarus 配置对象 (AlcarusRootConfig)。
    此函数会确保首先加载原始配置字典，然后将其转换为类型化对象。
    如果转换失败，程序将打印错误并退出。
    """
    global _loaded_typed_settings  # 引用全局类型化配置缓存

    if _loaded_typed_settings is not None:  # 如果已有缓存
        return _loaded_typed_settings  # 直接返回

    # 确保原始配置字典已加载
    config_dict = get_settings()  # get_settings() 会确保 load_settings() 被调用

    try:
        # 使用 AlcarusRootConfig.from_dict 方法将字典转换为类型化对象
        typed_config = AlcarusRootConfig.from_dict(config_dict)
        _loaded_typed_settings = typed_config  # 缓存类型化对象
        logger.info("配置已成功加载并转换为类型化对象 AlcarusRootConfig。")
        return typed_config
    except Exception as e:
        # 捕获在类型转换过程中发生的任何错误 (例如字段缺失、类型不匹配等)
        logger.info(f"严重错误：将配置字典转换为 AlcarusRootConfig 类型化对象失败: {e}")
        logger.info(
            "这通常意味着 'config/config.toml' 文件的结构或数据类型与 'src/config/alcarus_configs.py' 中的 dataclass 定义不匹配。"
        )
        logger.info("请仔细检查以下几点：")
        logger.info("  1. TOML 文件中的所有键名是否与 dataclass 中的字段名完全一致（包括大小写）。")
        logger.info("  2. 嵌套结构是否匹配（例如，TOML 中的表是否对应 dataclass 中的嵌套 dataclass）。")
        logger.info("  3. 数据类型是否兼容（例如，期望整数的地方是否是字符串）。")
        logger.info("  4. 是否所有在 dataclass 中没有默认值的必需字段都在 TOML 文件中提供了。")
        import traceback

        traceback.logger.info_exc()  # 打印完整的错误堆栈信息，帮助定位问题
        _prompt_user_and_exit("类型化配置加载失败，请检查上述错误和您的配置文件/dataclass定义。")
        # _prompt_user_and_exit 会导致程序退出，所以下面的 return 理论上不会执行
        raise  # 或者直接重新抛出异常，让上层处理（但不推荐，因为配置错误应尽早终止）
