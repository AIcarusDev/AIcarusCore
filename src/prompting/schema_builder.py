# 文件路径: src/prompting/schema_builder.py

from typing import Any

from src.common.custom_logging.logging_config import get_logger

logger = get_logger(__name__)


class SchemaBuilder:
    """一个纯粹的、无状态的 JSON Schema 组装器."""

    def __init__(self) -> None:
        logger.info("SchemaBuilder (纯粹组装者版) 已初始化。")

    def assemble(self, schema_parts: dict[str, Any]) -> dict[str, Any]:
        """将预先构建好的 Schema 片段组装成一个完整的 JSON Schema.

        Args:
            schema_parts: 一个字典，包含了所有顶级的 Schema 组件，
                        例如 'internal_state', 'internal_action' 等。

        Returns:
            一个完整的、可用的 JSON Schema 字典。
        """
        final_schema = {
            "type": "object",
            "properties": schema_parts,
            "required": ["internal_state"],  # internal_state 是思考的核心，总是必需的
        }
        return final_schema
