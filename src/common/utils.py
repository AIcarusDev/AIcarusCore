# AIcarusCore/src/common/utils.py
import os
import queue

from src.common.custom_logging.logging_config import get_logger

logger = get_logger(__name__)


def build_conversation_entity_uid(platform_id: str, conv_type: str, native_id: str) -> str:
    """构建一个全局唯一的会话实体UID (作为数据库的_key).

    Args:
        platform_id: 平台ID (e.g., 'qq').
        conv_type: 会话类型 (e.g., 'group', 'private').
        native_id: 平台原生的会话ID (e.g., '123456').

    Returns:
        格式化后的唯一ID字符串 (e.g., 'qq_group_123456').
    """
    return f"{platform_id}_{conv_type}_{native_id}"


def parse_entity_uid(entity_uid: str) -> tuple[str, str, str] | None:
    """将一个完整的实体UID解析为其组成部分（平台、类型、原生ID）.

    这是 build_conversation_entity_uid 的逆向操作。

    Args:
        entity_uid: 完整的实体UID字符串 (e.g., 'qq_group_123456').

    Returns:
        一个包含 (平台, 类型, 原生ID) 的元组，如果格式无效则返回 None.
    """
    # 使用 maxsplit=2 来确保只分割两次，允许原生ID本身包含下划线
    parts = entity_uid.split("_", 2)

    # 显式检查分割后的部分是否正好为3个，增强了代码的可读性和健壮性
    if len(parts) == 3:
        platform, conv_type, native_id = parts
        return platform, conv_type, native_id
    else:
        logger.warning(f"尝试解析一个格式不正确的实体UID: '{entity_uid}'")
        return None


def parse_focus_path(focus_path: str | None) -> tuple[str, str, str | None]:
    """一个可复用的工具函数，用于解析注意力焦点路径字符串.

    Args:
        focus_path: 当前的注意力焦点路径，例如 "core", "qq", "qq.123456".

    Returns:
        一个包含 (层级, 平台ID, 会话ID) 的元组.
        - 层级: 'core', 'platform', 或 'cellular'.
        - 平台ID: 例如 'core', 'qq'.
        - 会话ID: 如果在底层，则为会话ID字符串；否则为 None.
    """
    if focus_path and focus_path != "core":
        path_parts = focus_path.split(".")
        current_platform_id = path_parts[0]
        if len(path_parts) >= 2:
            current_level = "cellular"
            # 修复：会话ID可能是由多个部分组成的，例如 "private.123456"
            current_conv_id = ".".join(path_parts[1:])
        else:
            current_level = "platform"
            current_conv_id = None
    else:
        current_level = "core"
        current_platform_id = "core"
        current_conv_id = None
    return current_level, current_platform_id, current_conv_id


def find_files(directory: str, extensions: list, ignore_items: set, log_queue: queue.Queue) -> list:
    r"""查找指定目录下及其所有子目录中的所有指定后缀名的文件.

    忽略规则更新：
    1. 如果忽略项不含路径分隔符 (如 'venv'), 则忽略所有同名文件/文件夹。
    2. 如果忽略项包含路径分隔符 (如 'C:\\project\\data'), 则精确匹配该完整路径。
    """
    found_files_list = []
    log_queue.put(f"开始在 '{directory}' 中搜索...")

    extensions_lower = [ext.lower() for ext in extensions]

    # 将忽略项分为两类：纯名称 和 完整路径
    ignore_basenames = {item.lower() for item in ignore_items if os.path.sep not in item}
    ignore_full_paths = {
        os.path.abspath(item).lower() for item in ignore_items if os.path.sep in item
    }

    for root, dirs, files in os.walk(directory, topdown=True):
        # --- 过滤目录 ---
        # 1. 根据纯名称过滤
        dirs[:] = [d for d in dirs if d.lower() not in ignore_basenames]
        # 2. 根据完整路径过滤
        dirs[:] = [
            d
            for d in dirs
            if os.path.abspath(os.path.join(root, d)).lower() not in ignore_full_paths
        ]

        # --- 过滤文件 ---
        for file in files:
            # 1. 根据纯名称过滤
            if file.lower() in ignore_basenames:
                continue

            full_path = os.path.join(root, file)
            abs_path = os.path.abspath(full_path)

            # 2.根据完整路径过滤
            if abs_path.lower() in ignore_full_paths:
                continue

            # 检查文件后缀名是否匹配
            if any(file.lower().endswith(ext) for ext in extensions_lower):
                found_files_list.append(abs_path)
                log_queue.put(f"  -> 找到: {abs_path}")

    log_queue.put(f"搜索完成。共找到 {len(found_files_list)} 个文件。")
    return found_files_list


def generate_file_tree(root_dir: str, file_paths: list, log_queue: queue.Queue) -> str:
    """根据文件路径列表生成文件结构树状图."""
    log_queue.put("正在生成文件结构树...")
    tree = {}
    for path in file_paths:
        try:
            relative_path = os.path.relpath(path, root_dir)
            parts = relative_path.split(os.sep)
            current_level = tree
            for part in parts[:-1]:
                if part not in current_level:
                    current_level[part] = {}
                current_level = current_level[part]
            current_level[parts[-1]] = None
        except ValueError:
            parts = path.split(os.sep)
            current_level = tree
            for part in parts[:-1]:
                if part not in current_level:
                    current_level[part] = {}
                current_level = current_level[part]
            current_level[parts[-1]] = None
