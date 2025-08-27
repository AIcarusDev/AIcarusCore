# 文件路径: src/apps/qq/components.py

from dataclasses import dataclass, field
from typing import Any


@dataclass
class PromptComponents:
    """一个数据容器，存放构建 Prompt 所需的各个部分."""

    system_prompt_blocks: dict[str, Any] = field(default_factory=dict)
    user_prompt_blocks: dict[str, Any] = field(default_factory=dict)
    response_schema: dict[str, Any] = field(default_factory=dict)
