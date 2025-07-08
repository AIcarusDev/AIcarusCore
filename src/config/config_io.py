# File: AIcarusCore/src/config/config_io.py
# 这个模块负责处理配置文件的输入输出操作

import shutil
from datetime import datetime
from pathlib import Path

import tomlkit
from src.common.custom_logging.logging_config import get_logger

from .config_paths import (
    ACTUAL_CONFIG_FILENAME,
    CONFIG_TEMPLATE_FILENAME,
    OLD_CONFIG_BACKUP_DIR,
    RUNTIME_CONFIG_DIR,
    TEMPLATE_DIR,
)

logger = get_logger(__name__)


class ConfigIOHandler:
    """ConfigIOHandler 类负责处理配置文件的输入输出操作.

    它提供了一些方法来加载、保存和备份配置文件.

    Attributes:
        template_path (Path): 模板配置文件的路径.
        runtime_path (Path): 运行时配置文件的路径.
        backup_dir (Path): 备份目录的路径.
        runtime_filename (str): 运行时配置文件的文件名，用于生成备份文件名.
    """

    def __init__(self) -> None:
        """初始化配置文件 I/O 处理器."""
        self.template_path: Path = TEMPLATE_DIR / CONFIG_TEMPLATE_FILENAME
        self.runtime_path: Path = RUNTIME_CONFIG_DIR / ACTUAL_CONFIG_FILENAME
        self.backup_dir: Path = OLD_CONFIG_BACKUP_DIR
        self.runtime_filename: str = ACTUAL_CONFIG_FILENAME  # 用于生成备份文件名

        self._ensure_directories_exist()  # 先确保文件夹都乖乖待在原地

    def _ensure_directories_exist(self) -> None:
        """确保所有必要的目录都存在，如果不存在就创建它们."""
        logger.debug(f"检查目录: {self.runtime_path.parent}")
        self.runtime_path.parent.mkdir(parents=True, exist_ok=True)  # 运行时配置文件的家
        logger.debug(f"检查目录: {self.backup_dir}")
        self.backup_dir.mkdir(parents=True, exist_ok=True)  # 备份文件的家

    def template_exists(self) -> bool:
        """检查模板配置文件是否存在."""
        exists = self.template_path.exists()
        logger.debug(f"模板文件 '{self.template_path}' 是否存在: {exists}")
        return exists

    def runtime_config_exists(self) -> bool:
        """检查运行时配置文件是否存在."""
        exists = self.runtime_path.exists()
        logger.debug(f"运行时文件 '{self.runtime_path}' 是否存在: {exists}")
        return exists

    def load_toml_file(self, file_path: Path) -> tomlkit.TOMLDocument | None:
        """尝试从指定路径加载 TOML 文件.

        Args:
            file_path (Path): 要加载的 TOML 文件路径.

        Returns:
            tomlkit.TOMLDocument | None: 如果加载成功返回 TOML 数据，否则返回 None.
        """
        if not file_path.exists():
            logger.warning(f"想加载的 TOML 文件 '{file_path}' 好像不见了...")
            return None
        try:
            with open(file_path, encoding="utf-8") as f:
                logger.debug(f"正在加载 TOML 文件: {file_path}")
                data = tomlkit.load(f)
                logger.debug(f"TOML 文件 '{file_path}' 加载成功！")  # INFO -> DEBUG
                return data
        except tomlkit.exceptions.TOMLKitError as e:
            logger.error(f"哎呀！解析 TOML 文件 '{file_path}' 失败了: {e}")
            return None
        except Exception as e:
            logger.error(f"读取文件 '{file_path}' 时发生了意想不到的错误: {e}")
            return None

    def save_toml_file(self, file_path: Path, data: tomlkit.TOMLDocument) -> bool:
        """将 TOML 数据保存到指定路径.

        Args:
            file_path (Path): 要保存的文件路径.
            data (tomlkit.TOMLDocument): 要保存的 TOML 数据.

        Returns:
            bool: 如果保存成功返回 True，否则返回 False.
        """
        try:
            with open(file_path, "w", encoding="utf-8") as f:
                tomlkit.dump(data, f)
            logger.debug(f"TOML 数据已成功保存到 '{file_path}'。")  # INFO -> DEBUG
            return True
        except Exception as e:
            logger.error(f"保存 TOML 文件 '{file_path}' 时失败了: {e}")
            return False

    def copy_template_to_runtime(self) -> bool:
        """从模板复制配置文件到运行时位置.

        Returns:
            bool: 如果复制成功返回 True，否则返回 False.
        """
        if not self.template_exists():
            logger.error(f"模板文件 '{self.template_path}' 不见了，复制任务取消！")
            return False
        try:
            shutil.copy2(self.template_path, self.runtime_path)
            logger.debug(
                f"已从模板 '{self.template_path}' 复制到运行时位置 '{self.runtime_path}'。"
            )  # INFO -> DEBUG
            return True
        except Exception as e:
            logger.error(f"复制模板文件时发生错误: {e}")
            return False

    def backup_runtime_config(self, prefix: str = "") -> Path | None:
        """备份当前的运行时配置文件到备份目录.

        Args:
            prefix (str): 备份文件名前缀，默认为空字符串.

        Returns:
            Path | None: 返回备份文件的路径，如果备份失败则返回 None.
        """
        if not self.runtime_config_exists():
            logger.debug(
                f"运行时配置文件 '{self.runtime_path}' 本来就不在，不用备份啦。"
            )  # INFO -> DEBUG
            return None

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        # 确保 runtime_filename 是纯文件名，不带路径
        base_filename = Path(self.runtime_filename).name
        backup_filename = f"{prefix}{base_filename}_{timestamp}.toml"
        backup_file_path = self.backup_dir / backup_filename

        try:
            shutil.move(str(self.runtime_path), str(backup_file_path))  # str() 确保兼容性
            logger.debug(
                f"已将运行时配置文件 '{self.runtime_path}' 备份到 '{backup_file_path}'。"
            )  # INFO -> DEBUG
            return backup_file_path
        except Exception as e:
            logger.error(f"备份运行时配置文件 '{self.runtime_path}' 失败: {e}")
            return None
