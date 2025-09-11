# 文件路径: src/os/services/filesystem_service.py

import io
import os
from pathlib import Path
from typing import Any

from src.common.custom_logging.logging_config import get_logger
from src.common.utils import find_files, generate_file_tree
from src.config import config

logger = get_logger(__name__)

# 从旧 ActionHandler 迁移过来的常量
MAX_CONTENT_PREVIEW_SIZE = 32768
MAX_AGGREGATE_SIZE = 65535


class FileSystemService:
    """AIC-OS 提供的、带安全沙箱的文件系统访问服务."""

    def __init__(self) -> None:
        self._workspace_root = Path(config.runtime_environment.workspace_root)
        self._workspace_root.mkdir(parents=True, exist_ok=True)
        logger.info(f"FileSystemService 已初始化，工作区: {self._workspace_root}")

    def get_actions_schema(self) -> dict[str, Any]:
        """返回此服务提供的所有动作的 JSON Schema 定义."""
        # 这些功能暂时禁用，等待未来集成到OS的文件管理系统中
        return {}
        # return {
        # "list_files": {
        #     "type": "object",
        #     "description": "列出指定路径下的文件和文件夹。",
        #     "properties": {
        #         "path": {
        #             "type": "string",
        #             "description": "要查看的路径，相对于工作区根目录。使用'/'作为分隔符。'.' 代表当前目录。",  # noqa: E501
        #         },
        #         "motivation": {"type": "string"},
        #     },
        #     "required": ["path", "motivation"],
        # },
        # "read_file": {
        #     "type": "object",
        #     "description": "读取指定文件的内容。",
        #     "properties": {
        #         "path": {
        #             "type": "string",
        #             "description": "要读取的文件的路径，相对于工作区根目录。",
        #         },
        #         "motivation": {"type": "string"},
        #     },
        #     "required": ["path", "motivation"],
        # },
        # "write_file": {
        #     "type": "object",
        #     "description": "向指定文件写入内容。如果文件不存在，会自动创建。",
        #     "properties": {
        #         "path": {
        #             "type": "string",
        #             "description": "要写入的文件的路径，相对于工作区根目录。",
        #         },
        #         "content": {
        #             "type": "string",
        #             "description": "要写入的内容。这是一个标准的JSON字符串，换行符请使用'\\n'进行转义。",  # noqa: E501
        #         },
        #         "append": {
        #             "type": "boolean",
        #             "description": "是否以追加模式写入。True为追加到末尾，False为覆盖整个文件。默认为True。",  # noqa: E501
        #             "default": True,
        #         },
        #         "motivation": {"type": "string"},
        #     },
        #     "required": ["path", "content", "motivation"],
        # },
        # "edit_file": {
        #     "type": "object",
        #     "description": "在指定文件中搜索并替换内容。",
        #     "properties": {
        #         "path": {
        #             "type": "string",
        #             "description": "要编辑的文件的路径，相对于工作区根目录。",
        #         },
        #         "search_pattern": {"type": "string", "description": "要查找并替换的文本内容。"},  # noqa: E501
        #         "replace_string": {"type": "string", "description": "用来替换的新文本内容。"},
        #         "motivation": {"type": "string"},
        #     },
        #     "required": ["path", "search_pattern", "replace_string", "motivation"],
        # },
        # "get_aggregated_content": {
        #     "type": "object",
        #     "description": "扫描工作区内指定路径，将所有符合条件的文件内容聚合后，直接作为字符串返回。",  # noqa: E501
        #     "properties": {
        #         "source_path": {
        #             "type": "string",
        #             "description": "要扫描的源路径，相对于工作区根目录。例如 '.' 代表整个工作区。",  # noqa: E501
        #         },
        #         "extensions": {
        #             "type": "array",
        #             "description": "（可选）一个只包含指定文件扩展名的列表。如果省略，将使用默认配置。",  # noqa: E501
        #             "items": {"type": "string"},
        #         },
        #         "ignore_items": {
        #             "type": "array",
        #             "description": "（可选）一个要忽略的文件或文件夹名称的列表。如果省略，将使用默认配置。",  # noqa: E501
        #             "items": {"type": "string"},
        #         },
        #         "motivation": {"type": "string"},
        #     },
        #     "required": ["source_path", "motivation"],
        # },
        # "delete_workspace_file": {
        #     "type": "object",
        #     "description": "【危险操作】删除工作区内的指定文件。请谨慎使用！",
        #     "properties": {
        #         "path": {
        #             "type": "string",
        #             "description": "要删除的文件的路径，【必须】相对于工作区根目录。",
        #         },
        #         "motivation": {"type": "string"},
        #     },
        #     "required": ["path", "motivation"],
        # },

    # 方法作废
    def resolve_safe_path(self, user_path: str) -> Path | None:
        """解析用户提供的路径，确保其在工作区内并返回安全路径."""
        workspace_root = self.get_safe_workspace_root()
        sanitized_path_str = user_path.strip().lstrip("/\\")
        candidate_path = workspace_root.joinpath(sanitized_path_str)
        try:
            real_workspace_root = os.path.realpath(self._workspace_root)
            real_candidate_path = os.path.realpath(candidate_path)
            common_prefix = os.path.commonpath([real_workspace_root, real_candidate_path])
            if os.path.realpath(common_prefix) == real_workspace_root:
                return Path(real_candidate_path)
            else:
                logger.error(
                    f"路径遍历攻击尝试被阻止！目标路径 '{real_candidate_path}' "
                    f"超出工作区 '{real_workspace_root}'。"
                )
                return None
        except ValueError:
            logger.error(f"路径 '{candidate_path}' 与工作区不在同一驱动器上，操作被拒绝。")
            return None
        except Exception as e:
            logger.error(f"解析安全路径时发生未知错误: {e}", exc_info=True)
            return None

    # 方法作废
    def get_safe_workspace_root(self) -> Path:
        """返回工作区根目录的绝对路径，确保其存在."""
        self._workspace_root.mkdir(parents=True, exist_ok=True)
        return self._workspace_root.resolve()

    # 方法作废
    def list_files(self, params: dict) -> str:
        """列出指定路径下的所有文件和目录."""
        path_str = params.get("path", ".")
        safe_path = self.resolve_safe_path(path_str)
        if not safe_path:
            return f"错误：路径 '{path_str}' 不安全或无效。"
        try:
            if not safe_path.exists():
                return f"错误：路径 '{path_str}' 不存在。"
            if not safe_path.is_dir():
                return f"错误：'{path_str}' 不是一个目录。"
            items = [
                f"[{'DIR' if item.is_dir() else 'FILE'}] {item.name}"
                for item in safe_path.iterdir()
            ]
            if not items:
                return f"目录 '{path_str}' 是空的。"
            return f"目录 '{path_str}' 下的内容：\n" + "\n".join(items)
        except Exception as e:
            logger.error(f"列出文件时出错 ({path_str}): {e}", exc_info=True)
            return f"错误：列出文件时发生未知错误: {e}"

    # 方法作废
    def read_file(self, safe_path: Path, original_path: str) -> str:
        """验证路径是否为文件并读取其内容。失败时抛出 FileNotFoundError."""
        if not safe_path.is_file():
            raise FileNotFoundError(f"错误：路径 '{original_path}' 不是一个文件或不存在。")
        return safe_path.read_text(encoding="utf-8")

    # 方法作废
    def write_file(self, params: dict) -> str:
        """写入文件内容."""
        path_str = params.get("path")
        content = params.get("content")
        append = params.get("append", True)
        if not path_str or content is None:
            return "错误：未提供文件路径或写入内容。"
        safe_path = self.resolve_safe_path(path_str)
        if not safe_path:
            return f"错误：路径 '{path_str}' 不安全或无效。"
        try:
            safe_path.parent.mkdir(parents=True, exist_ok=True)
            mode = "a" if append else "w"
            with safe_path.open(mode, encoding="utf-8") as f:
                f.write(content)
            final_content = safe_path.read_text(encoding="utf-8")
            if len(final_content.encode("utf-8")) > MAX_CONTENT_PREVIEW_SIZE:
                final_content = final_content[:MAX_CONTENT_PREVIEW_SIZE] + "\n... [内容已截断]"
            action_desc = "追加内容到" if append else "覆写"
            return (
                f"成功！已{action_desc}文件 '{path_str}'。\n"
                f"目前文件的内容为:\n---\n{final_content}\n---"
            )
        except Exception as e:
            logger.error(f"写入文件时出错 ({path_str}): {e}", exc_info=True)
            return f"错误：写入文件时发生未知错误: {e}"

    # 方法作废
    def edit_file(self, params: dict) -> str:
        """编辑指定路径的文件，替换其中的内容."""
        path_str = params.get("path")
        search_pattern = params.get("search_pattern")
        replace_string = params.get("replace_string")
        if not all([path_str, search_pattern, replace_string is not None]):
            return "错误：缺少编辑文件所需的参数。"
        safe_path = self.resolve_safe_path(path_str)
        if not safe_path:
            return f"错误：路径 '{path_str}' 不安全或无效。"
        try:
            original_content = self.read_file(safe_path, path_str)
            if search_pattern not in original_content:
                return (
                    f"操作完成，但在文件 '{path_str}' 中未找到要替换的文本 '{search_pattern}'。\n"
                    f"目前文件内容未改变:\n---\n{original_content[:MAX_CONTENT_PREVIEW_SIZE]}\n---"
                )
            new_content = original_content.replace(search_pattern, replace_string)
            safe_path.write_text(new_content, encoding="utf-8")
            final_content_preview = new_content
            if len(final_content_preview.encode("utf-8")) > MAX_CONTENT_PREVIEW_SIZE:
                final_content_preview = (
                    final_content_preview[:MAX_CONTENT_PREVIEW_SIZE] + "\n... [内容已截断]"
                )
            return (
                f"成功！已编辑文件 '{path_str}'，将所有 '{search_pattern}' "
                f"替换为 '{replace_string}'。\n目前文件的内容为:\n---\n{final_content_preview}\n---"
            )
        except FileNotFoundError as e:
            return str(e)
        except Exception as e:
            logger.error(f"编辑文件时出错 ({path_str}): {e}", exc_info=True)
            return f"错误：编辑文件时发生未知错误: {e}"

    # 方法作废
    def get_aggregated_content(self, params: dict) -> str:
        """聚合指定路径下的所有文件内容."""
        if find_files is None or generate_file_tree is None:
            return "错误：代码聚合功能的核心模块未能加载，无法执行此操作。"
        source_path_str = params.get("source_path")
        if not source_path_str:
            return "错误：缺少源路径参数。"
        safe_source_path = self.resolve_safe_path(source_path_str)
        if not safe_source_path:
            return f"错误：源路径 '{source_path_str}' 不安全或无效。"
        log_buffer = io.StringIO()

        class FakeQueue:
            def put(self, msg: str) -> None:
                log_buffer.write(str(msg) + "\n")

        fake_log_queue = FakeQueue()
        try:
            api_defaults = {
                "extensions": [".py", ".md", ".txt", ".json", ".toml", ".yaml"],
                "ignore_items": {
                    "venv",
                    "__pycache__",
                    ".git",
                    ".vscode",
                    "node_modules",
                    "dist",
                    "build",
                    ".pytest_cache",
                    "output",
                },
            }
            extensions = params.get("extensions", api_defaults["extensions"])
            ignore_items = set(params.get("ignore_items", [])) | api_defaults["ignore_items"]
            found_files = find_files(
                str(safe_source_path), extensions, ignore_items, fake_log_queue
            )
            if not found_files:
                return f"在 '{source_path_str}' 路径下未找到符合条件的文件。"
            output_buffer = io.StringIO()
            output_buffer.write("=" * 80 + "\n")
            output_buffer.write(f"根目录: {safe_source_path}\n")
            output_buffer.write(f"共 {len(found_files)} 个文件\n")
            output_buffer.write("=" * 80 + "\n\n")
            tree_structure = generate_file_tree(str(safe_source_path), found_files, fake_log_queue)
            output_buffer.write("文件结构树:\n")
            output_buffer.write(tree_structure)
            output_buffer.write("\n\n" + "=" * 80 + "\n\n")
            for file_path in found_files:
                output_buffer.write("-" * 80 + "\n")
                output_buffer.write(f"文件路径: {file_path}\n")
                output_buffer.write("-" * 80 + "\n\n")
                try:
                    with open(file_path, encoding="utf-8", errors="ignore") as input_file:
                        content = input_file.read()
                        lang = os.path.splitext(file_path)[1].lstrip(".")
                        output_buffer.write(f"```{lang}\n")
                        output_buffer.write(content)
                        output_buffer.write("\n```\n\n")
                except Exception as e:
                    output_buffer.write(f"!!! 读取文件时出错: {file_path} -> {e} !!!\n\n")
            final_content = output_buffer.getvalue()
            if len(final_content.encode("utf-8")) > MAX_AGGREGATE_SIZE:
                safe_cut_pos = final_content.rfind("\n" + "-" * 80, 0, MAX_AGGREGATE_SIZE)
                if safe_cut_pos != -1:
                    final_content = final_content[:safe_cut_pos]
                else:
                    final_content = final_content[:MAX_AGGREGATE_SIZE]
                final_content += "\n... [聚合内容过长，已在末尾截断]"
            return f"成功聚合了 '{source_path_str}' 的内容：\n{final_content}"
        except Exception as e:
            logger.error(f"聚合内容时出错 ({source_path_str}): {e}", exc_info=True)
            return f"错误：聚合内容时发生未知错误: {e}"

    # 方法作废
    def delete_file(self, params: dict) -> str:
        """删除指定路径的文件."""
        path_str = params.get("path")
        if not path_str:
            return "错误：未提供要删除的文件路径。"
        safe_path = self.resolve_safe_path(path_str)
        if not safe_path:
            return f"错误：路径 '{path_str}' 不安全或无效。"
        try:
            if not safe_path.exists():
                return f"操作完成：文件 '{path_str}' 本来就不存在。"
            if not safe_path.is_file():
                return f"错误：路径 '{path_str}' 是一个目录，此功能只能删除文件。"
            safe_path.unlink()
            return f"成功！已删除文件 '{path_str}'。"
        except Exception as e:
            logger.error(f"删除文件时出错 ({path_str}): {e}", exc_info=True)
            return f"错误：删除文件时发生未知错误: {e}"
