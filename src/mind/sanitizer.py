# src/core_logic/sanitizer.py
import re
from typing import Any

from src.common.custom_logging.logging_config import get_logger

logger = get_logger(__name__)


class LLMOutputSanitizer:
    """一个路径感知的智能修正器.

    它递归地修正LLM输出，根据字段的语义路径应用不同的规则。
    """

    def __init__(
        self, user_map: dict[str, dict[str, Any]], uid_str_to_platform_id_map: dict[str, str]
    ) -> None:
        """初始化修正器所需的上下文信息.

        Args:
            user_map: 从平台ID到用户详细信息（nick, card等）的映射。
            uid_str_to_platform_id_map: 从内部UID字符串（U0, U1...）到平台ID的映射。
        """
        self._uid_to_name_map = {}
        self._uid_to_platform_id_map = {}
        self._uid_pattern = re.compile(r"\bU\d+\b")

        # 1. 构建 UID -> 显示名 的映射 (用于自然语言字段)
        for uid_str, platform_id in uid_str_to_platform_id_map.items():
            if (user_data := user_map.get(platform_id)) and (
                display_name := user_data.get("card") or user_data.get("nick")
            ):
                self._uid_to_name_map[uid_str] = display_name
        self._uid_to_name_map["U0"] = "我"

        # 2. 构建 UID -> 平台原生ID 的映射 (用于指令参数字段)
        self._uid_to_platform_id_map = uid_str_to_platform_id_map.copy()
        # 注意: U0 对应的平台原生ID就是机器人自己的ID，这个映射是正确的

        logger.debug(f"Sanitizer Name Map: {self._uid_to_name_map}")
        logger.debug(f"Sanitizer ID Map: {self._uid_to_platform_id_map}")

    def _get_name_replacer(self, match: re.Match) -> str:
        """替换为显示名."""
        uid = match[0]
        return self._uid_to_name_map.get(uid, "某人")

    def _get_id_replacer(self, match: re.Match) -> str:
        """替换为平台原生ID."""
        uid = match[0]
        # 如果在ID映射表中找不到，这是一个严重错误，但我们仍然提供一个回退
        # 绝不能让 "U1" 这样的字符串污染指令
        return self._uid_to_platform_id_map.get(uid, "INVALID_UID_REFERENCE")

    def _recursive_sanitize(self, node: Any, current_path: tuple[str, ...]) -> Any:
        """递归核心，根据路径应用不同规则."""
        # 定义需要进行平台原生ID替换的字段名集合
        # 这是一个更健壮、更易于扩展的解决方案
        id_field_names = {
            "user_id",
            "target_user_id",
            "group_id",
            "conversation_id",
        }

        if isinstance(node, dict):
            return {
                key: self._recursive_sanitize(value, (*current_path, key))
                for key, value in node.items()
            }
        elif isinstance(node, list):
            # 对于列表，我们假设其所有项都遵循相同的净化规则
            # 这是一个合理的简化，因为JSON Schema通常会定义list item的类型
            return [self._recursive_sanitize(item, current_path) for item in node]
        elif isinstance(node, str):
            # 这是决策点：根据当前路径决定使用哪个替换规则
            # 规则1: 如果当前字段的 'key' 是一个ID字段，使用ID替换
            if current_path and current_path[-1] in id_field_names:
                logger.debug(f"ID Rule triggered for path: {current_path}")
                return self._uid_pattern.sub(self._get_id_replacer, node)

            # 规则2: 默认情况，对所有其他字符串使用名称替换
            # 这会覆盖 mood, think, intent, 还有 send_message 中的 text content
            return self._uid_pattern.sub(self._get_name_replacer, node)
        else:
            return node

    def sanitize(self, llm_json: dict[str, Any]) -> dict[str, Any]:
        """公开的调用接口，启动整个净化和修正过程."""
        if not isinstance(llm_json, dict):
            return llm_json
        return self._recursive_sanitize(llm_json, ())
