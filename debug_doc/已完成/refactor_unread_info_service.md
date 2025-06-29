# 重构未读消息服务 (`unread_info_service`)

**状态:** 已完成
**完成时间:** 2025-06-22

**目标:** 使用时间戳机制替代现有的 `is_processed` 布尔标记来判断消息是否已读，以提高效率和简化逻辑。

---

## 重构计划

### 第一步：修改数据库模型 (`models.py`)

1.  **为 `EnrichedConversationInfo` 添加新字段**:
    - 在 `EnrichedConversationInfo` dataclass 中，添加一个新字段 `last_processed_timestamp: int | None = None`。
    - **作用**: 记录该会话的消息被AI核心处理到的最新时间点。
    - **默认值**: `None`，表示该会话从未被处理过。

2.  **从 `DBEventDocument` 中移除 `is_processed` 字段**:
    - 在 `DBEventDocument` dataclass 中，删除 `is_processed: bool = False` 这一行。

---

### 第二步：修改数据库服务层

#### 1. `event_storage_service.py`

-   **`save_event_document` 方法**:
    - 移除在保存消息事件时自动添加 `is_processed = False` 的逻辑。
-   **`get_unprocessed_message_events` 方法**:
    - **标记为“待废弃”**。新的逻辑将不再直接依赖此方法。
-   **`mark_events_as_processed` 方法**:
    - **直接删除**。此方法将不再被需要。
-   **`has_new_events_since` 方法**:
    - **保留并作为核心**。此方法将用于高效检查会话是否有新消息。

#### 2. `conversation_storage_service.py`

-   **新增 `update_conversation_processed_timestamp` 方法**:
    - **签名**: `async def update_conversation_processed_timestamp(self, conversation_id: str, timestamp: int) -> bool:`
    - **作用**: 在一个会话的所有新消息处理完毕后，调用此方法更新 `EnrichedConversationInfo` 文档中的 `last_processed_timestamp` 字段为最新的消息时间戳。
-   **新增 `get_all_active_conversations` 方法**:
    - **签名**: `async def get_all_active_conversations(self) -> list[dict[str, Any]]:`
    - **作用**: 获取所有需要检查未读状态的会话列表。可以先简单实现为获取所有会话，后续可根据 `attention_profile` 进行优化，只返回需要关注的会话。

---

### 第三步：修改核心逻辑层 (`unread_info_service.py`)

将 `generate_unread_summary_text` 和 `get_structured_unread_conversations` 方法的逻辑重构为以下新流程：

1.  **获取会话**: 调用 `conversation_storage.get_all_active_conversations()` 获取所有需要检查的会话。

2.  **遍历检查**: 对每个会话 `conv` 进行遍历。

3.  **检查新消息**:
    a.  获取会话的 `last_processed_timestamp` (如果为 `None`，则视为 0)。
    b.  调用 `event_storage.get_messages_in_conversation_after(conv.conversation_id, last_processed_timestamp)` 获取该时间戳之后的新消息。 (注意: `event_storage_service` 中可能需要新增或调整一个方法来实现此功能，例如 `get_events_by_conversation_after_timestamp`)

4.  **聚合未读信息**:
    - 如果步骤3返回了新消息，则将该会话视为“未读”。
    - 将新消息聚合起来，生成预览文本、统计未读数量等。
    - 将处理后的会话信息（包含未读详情）添加到一个临时列表中。

5.  **生成最终输出**:
    - 遍历结束后，使用上一步生成的临时列表来构建最终的文本摘要或结构化数据。

6.  **更新时间戳**:
    - 在 `consciousness_flow.py` 或其他调用 `unread_info_service` 的地方，在处理完一个会话后，需要调用 `conversation_storage.update_conversation_processed_timestamp()` 来更新时间戳，以便下次不再重复处理。

---
### 完成说明
所有计划中的代码修改均已完成。相关文件已被更新，新机制已在 `consciousness_flow.py` 中正确应用。
