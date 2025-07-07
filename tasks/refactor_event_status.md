# 重构事件处理状态

将 `is_processed` 布尔值替换为更灵活的 `status` 字符串字段。

## 待办事项

1.  **修改数据库模型 (`AIcarusCore/src/database/models.py`)**:
    *   在 `DBEventDocument` dataclass 中，添加一个新的字段 `status: str`。
    *   为 `status` 字段设置默认值 `"unprocessed"`。

2.  **修改消息处理器 (`AIcarusCore/src/message_processing/default_message_processor.py`)**:
    *   移除 `is_event_processed` 布尔变量。
    *   引入 `event_status` 字符串变量，并根据逻辑（如测试模式）将其设置为 `"unprocessed"` 或 `"ignored"` 等。
    *   更新事件持久化逻辑，直接使用 `DBEventDocument` 中定义的 `status` 字段，不再手动添加 `is_processed` 到字典中。
    *   修改事件分发前的检查逻辑，从检查 `is_event_processed` 变为检查 `event_status` 是否为 `"unprocessed"`。
