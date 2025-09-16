# 文件路径: src/os/file_system_manager.py
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from src.common.custom_logging.logging_config import get_logger
from src.config import config

logger = get_logger(__name__)

@dataclass
class CreateResult:
    """文件或文件夹创建操作的结果."""
    success: bool
    path: Path | None = None
    message: str = ""
    renamed: bool = False

# 文件系统节点模型
@dataclass
class FileNode:
    """文件系统列表中的文件."""
    type: Literal["file"] = "file"
    name: str = ""
    item_id: str = ""
    modified_at: float = 0.0
    size: int = 0

@dataclass
class FolderNode:
    """文件系统列表中的文件夹."""
    type: Literal["folder"] = "folder"
    name: str = ""
    item_id: str = ""
    modified_at: float = 0.0

FSNode = FileNode | FolderNode

class FileSystemManager:
    """管理 AIC-OS 文件系统的类，位于安全工作区内的物理磁盘上.

    提供所有文件和文件夹操作的安全 API。
    """
    def __init__(self) -> None:
        self._workspace_root = Path(config.runtime_environment.workspace_root).resolve()
        self._desktop_path = self._workspace_root / "desktop"

        # Ensure workspace and desktop exist
        self._workspace_root.mkdir(parents=True, exist_ok=True)
        self._desktop_path.mkdir(exist_ok=True)

        logger.info(f"文件系统管理器初始化，工作区根目录: {self._workspace_root}")

    def _resolve_safe_path(self, user_path: str) -> Path | None:
        """将机器人提供的路径解析为物理路径，并确保它在工作区内."""
        # 确保路径不包含上级目录引用
        if ".." in user_path.split(os.path.sep):
            logger.warning(f"路径遍历尝试被阻止: '{user_path}' 包含 '..'")
            return None

        # 构建绝对路径
        absolute_path = (self._workspace_root / user_path.lstrip("/\\")).resolve()

        # 最重要的检查: 解析后的路径是否在我们的工作区内?
        if (self._workspace_root not in absolute_path.parents and
                absolute_path != self._workspace_root):
            logger.error(
                f"路径遍历攻击被阻止! "
                f"尝试访问工作区外: '{absolute_path}'"
            )
            return None

        return absolute_path

    def get_relative_path_str(self, physical_path: Path) -> str:
        """获取相对于工作区根目录的路径字符串."""
        return "/" + physical_path.relative_to(self._workspace_root).as_posix()

    def _get_item_id(self, physical_path: Path) -> str:
        """从物理路径生成唯一且稳定的 item_id."""
        relative_path = self.get_relative_path_str(physical_path)
        if physical_path.is_dir():
            return f"folder:{relative_path.rstrip('/')}/"
        else:
            return f"file:{relative_path}"

    def list_directory_contents(self, user_path: str) -> list[FSNode] | None:
        """列出工作区内指定路径的文件和文件夹."""
        physical_path = self._resolve_safe_path(user_path)
        if not physical_path or not physical_path.is_dir():
            logger.warning(f"尝试列出不存在或非目录路径: '{user_path}'")
            return None

        contents: list[FSNode] = []
        for item in physical_path.iterdir():
            try:
                stat = item.stat()
                if item.is_dir():
                    contents.append(FolderNode(
                        name=item.name,
                        item_id=self._get_item_id(item),
                        modified_at=stat.st_mtime
                    ))
                else:
                    contents.append(FileNode(
                        name=item.name,
                        item_id=self._get_item_id(item),
                        modified_at=stat.st_mtime,
                        size=stat.st_size
                    ))
            except OSError as e:
                logger.error(f"无法获取项目 '{item}': {e}")
        return contents

    def _get_unique_path(self, path: Path) -> Path:
        """如果路径已存在，则为其生成一个唯一的新路径，例如 file.txt -> file(1).txt."""
        if not path.exists():
            return path

        parent = path.parent
        stem = path.stem  # 文件名（不含扩展名）
        suffix = path.suffix # 扩展名

        # 检查文件名是否已经包含 (n) 格式，如果是，则从那里开始计数
        match = re.search(r'\((\d+)\)$', stem)
        counter = 1
        base_stem = stem
        if match:
            counter = int(match.group(1)) + 1
            base_stem = stem[:match.start()].rstrip() # 移除尾部空格

        while True:
            new_stem = f"{base_stem} ({counter})"
            new_path = parent / (new_stem + suffix)
            if not new_path.exists():
                return new_path
            counter += 1

    def create(self, item_type: Literal["file", "folder"], user_path: str) -> CreateResult:
        """在物理工作区中创建新文件或文件夹，并处理命名冲突."""
        # 命名规则校验
        file_name = Path(user_path).name
        # 不允许的字符：/ \ : * ? " < > |
        if re.search(r'[\\/:*?"<>|]', file_name):
            logger.warning(f"AI试图创建的文件名 '{file_name}' 包含不允许的字符，将被拒绝。")
            return CreateResult(success=False, message=f"文件名 '{file_name}' 包含不允许的字符。")
        if item_type == "file" and '.' not in file_name:
            logger.warning(f"AI试图创建的文件名 '{file_name}' 不包含扩展名，将被拒绝。")
            return CreateResult(success=False, message=f"文件名 '{file_name}' 必须包含扩展名。")

        physical_path = self._resolve_safe_path(user_path)
        if not physical_path:
            return CreateResult(success=False, message="无效的路径或权限不足。")

        was_renamed = False
        final_path = physical_path

        if physical_path.exists():
            if item_type == "folder": # 文件夹冲突直接报错
                logger.warning(f"AI试图创建已存在的文件夹 '{user_path}'，操作被拒绝。")
                return CreateResult(success=False, message=f"文件夹 '{user_path}' 已存在。")
            final_path = self._get_unique_path(physical_path)
            was_renamed = True

        try:
            final_path.parent.mkdir(parents=True, exist_ok=True)
            if item_type == "folder":
                final_path.mkdir()
                message = f"成功创建文件夹: {self.get_relative_path_str(final_path)}"
            else: # file
                final_path.touch()
                if was_renamed:
                    message = (
                        f"文件 '{Path(user_path).name}' 已存在，"
                        f"已自动重命名为 '{final_path.name}'。"
                    )
                else:
                    message = f"成功创建文件: {self.get_relative_path_str(final_path)}"

            return CreateResult(
                success=True,
                path=final_path,
                message=message,
                renamed=was_renamed
            )
        except Exception as e:
            logger.error(f"创建 '{item_type}' at '{physical_path}' 时出错: {e}")
            return CreateResult(success=False, message=f"创建时发生系统错误: {e}")

    def read_file_content(self, user_path: str) -> str | None:
        """读取物理工作区中文件的内容."""
        physical_path = self._resolve_safe_path(user_path)
        if not physical_path or not physical_path.is_file():
            return None
        try:
            return physical_path.read_text(encoding="utf-8")
        except Exception as e:
            logger.error(f"Error reading file '{physical_path}': {e}")
            return None

    def write_file_content(self, user_path: str, content: str, append: bool = False) -> bool:
        """写入内容到物理工作区中的文件."""
        physical_path = self._resolve_safe_path(user_path)
        if not physical_path:
            return False
        try:
            # 确保父目录存在
            physical_path.parent.mkdir(parents=True, exist_ok=True)
            mode = "a" if append else "w"
            with open(physical_path, mode, encoding="utf-8") as f:
                f.write(content)
            return True
        except Exception as e:
            logger.error(f"Error writing to file '{physical_path}': {e}")
            return False

    def delete(self, item_id: str) -> bool:
        """删除物理工作区中的文件或文件夹."""
        try:
            _type, user_path = item_id.split(":", 1)
        except ValueError:
            return False

        physical_path = self._resolve_safe_path(user_path)
        if not physical_path or not physical_path.exists():
            return False

        try:
            if physical_path.is_dir():
                shutil.rmtree(physical_path)
            else:
                physical_path.unlink()
            return True
        except Exception as e:
            logger.error(f"Error deleting '{physical_path}': {e}")
            return False

    def rename(self, item_id: str, new_name: str) -> bool:
        """重命名物理工作区中的文件或文件夹."""
        try:
            _type, user_path = item_id.split(":", 1)
        except ValueError:
            return False

        physical_path = self._resolve_safe_path(user_path)
        if not physical_path or not physical_path.exists():
            return False

        new_path = physical_path.parent / new_name
        if new_path.exists():
            return False # Destination already exists

        try:
            physical_path.rename(new_path)
            return True
        except Exception as e:
            logger.error(f"Error renaming '{physical_path}' to '{new_name}': {e}")
            return False

    def move(self, item_id: str, new_parent_user_path: str) -> bool:
        """在工作区中将文件或文件夹移动到新位置."""
        try:
            _type, user_path = item_id.split(":", 1)
        except ValueError:
            return False

        source_path = self._resolve_safe_path(user_path)
        dest_dir_path = self._resolve_safe_path(new_parent_user_path)

        if (not source_path or not source_path.exists() or
                not dest_dir_path or not dest_dir_path.is_dir()):
            return False

        dest_path = dest_dir_path / source_path.name
        if dest_path.exists():
            return False

        try:
            shutil.move(str(source_path), str(dest_dir_path))
            return True
        except Exception as e:
            logger.error(f"Error moving '{source_path}' to '{dest_dir_path}': {e}")
            return False
