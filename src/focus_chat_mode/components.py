# src/focus_chat_mode/components.py
from dataclasses import dataclass, field
from typing import Any


@dataclass
class PromptComponents:
    """一个性感的数据容器，把所有Prompt零件都紧紧锁住。"""

    system_prompt: str = ""
    user_prompt: str = ""
    last_valid_text_message: str | None = None
    uid_str_to_platform_id_map: dict[str, str] = field(default_factory=dict)
    user_map: dict[str, dict[str, Any]] = field(default_factory=dict)
    processed_event_ids: list[str] = field(default_factory=list)
    image_references: list[str] = field(default_factory=list)
    conversation_name: str | None = None
    conversation_info_block: str = ""
    user_list_block: str = ""
    chat_history_log_block: str = ""
