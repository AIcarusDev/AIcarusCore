"""
配置文件输入输出小助手 (づ｡◕‿‿◕｡)づ
这个模块掌管着所有与配置文件相关的磁盘操作，
比如悄悄地读取它们，温柔地保存它们，或者在需要的时候给它们一个备份的家。
"""

import shutil
from datetime import datetime  # 生成时间戳，给备份文件一个独特的印记
from pathlib import Path  # 面向对象的路径操作，让路径处理更优雅

import tomlkit  # 用来和 TOML 文件玩耍，还能保留注释和格式哦

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
    """
    封装与配置文件相关的基本文件操作和 TOML 处理。
    它就像一个勤劳的小蜜蜂，负责配置文件的搬运和管理！
    """

    def __init__(self) -> None:
        """
        初始化小助手，告诉它模板在哪，运行时配置在哪，备份放哪。
        """
        self.template_path: Path = TEMPLATE_DIR / CONFIG_TEMPLATE_FILENAME
        self.runtime_path: Path = RUNTIME_CONFIG_DIR / ACTUAL_CONFIG_FILENAME
        self.backup_dir: Path = OLD_CONFIG_BACKUP_DIR
        self.runtime_filename: str = ACTUAL_CONFIG_FILENAME  # 用于生成备份文件名

        self._ensure_directories_exist()  # 先确保文件夹都乖乖待在原地

    def _ensure_directories_exist(self) -> None:
        """悄悄检查并确保运行时配置目录和备份目录都好好地存在着。"""
        logger.debug(f"检查目录: {self.runtime_path.parent}")
        self.runtime_path.parent.mkdir(parents=True, exist_ok=True)  # 运行时配置文件的家
        logger.debug(f"检查目录: {self.backup_dir}")
        self.backup_dir.mkdir(parents=True, exist_ok=True)  # 备份文件的家

    def template_exists(self) -> bool:
        """模板配置文件在不在呀？我瞅瞅 (¬‿¬)"""
        exists = self.template_path.exists()
        logger.debug(f"模板文件 '{self.template_path}' 是否存在: {exists}")
        return exists

    def runtime_config_exists(self) -> bool:
        """运行时配置文件在不在呀？再瞅瞅 (¬‿¬)"""
        exists = self.runtime_path.exists()
        logger.debug(f"运行时文件 '{self.runtime_path}' 是否存在: {exists}")
        return exists

    def load_toml_file(self, file_path: Path) -> tomlkit.TOMLDocument | None:
        """
        尝试从指定路径加载 TOML 文件。
        如果文件不存在，或者它心情不好（损坏了），就温柔地返回 None。
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
        """
        将 TOML 数据保存到指定路径。
        成功了就告诉我一声 (๑•̀ㅂ•́)و✧
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
        """
        从模板复制配置文件到运行时位置。
        就像复印一样，但更智能！
        """
        if not self.template_exists():
            logger.error(f"模板文件 '{self.template_path}' 不见了，复制任务取消！")
            return False
        try:
            shutil.copy2(self.template_path, self.runtime_path)
            logger.debug(f"已从模板 '{self.template_path}' 复制到运行时位置 '{self.runtime_path}'。")  # INFO -> DEBUG
            return True
        except Exception as e:
            logger.error(f"复制模板文件时发生错误: {e}")
            return False

    def backup_runtime_config(self, prefix: str = "") -> Path | None:
        """
        备份当前的运行时配置文件到备份目录。
        给它加上时间戳和可选的前缀，让它在备份文件夹里独一无二。
        """
        if not self.runtime_config_exists():
            logger.debug(f"运行时配置文件 '{self.runtime_path}' 本来就不在，不用备份啦。")  # INFO -> DEBUG
            return None

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        # 确保 runtime_filename 是纯文件名，不带路径
        base_filename = Path(self.runtime_filename).name
        backup_filename = f"{prefix}{base_filename}_{timestamp}.toml"
        backup_file_path = self.backup_dir / backup_filename

        try:
            shutil.move(str(self.runtime_path), str(backup_file_path))  # str() 确保兼容性
            logger.debug(f"已将运行时配置文件 '{self.runtime_path}' 备份到 '{backup_file_path}'。")  # INFO -> DEBUG
            return backup_file_path
        except Exception as e:
            logger.error(f"备份运行时配置文件 '{self.runtime_path}' 失败: {e}")
            return None
