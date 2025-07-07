# 需求

当前 `SummarizationService` 生成的会话总结仅存在于内存中，在专注聊天会话结束后作为交接信息传递给主意识，但没有被持久化存储。需要将会话的最终总结存入数据库，以便后续查阅和分析。

# 方案设计

选择在专注聊天会话（Focus Chat Session）完全结束时，将该会话生命周期内生成的最终版本 `current_handover_summary` 存储到数据库中。

## 1. 数据库模型定义 (models.py)

在 `AIcarusCore/src/database/models.py` 中新增一个 `dataclass` 用于定义会话总结的文档结构。

```python
@dataclass
class ConversationSummaryDocument:
    """代表存储在数据库中的会话总结文档结构."""

    _key: str  # summary_id 将作为数据库文档的 _key
    summary_id: str  # 总结的唯一ID
    conversation_id: str  # 关联的会话ID
    timestamp: int  # 总结创建的时间戳 (毫秒, UTC)
    platform: str  # 会话所属平台
    bot_id: str  # 处理此会话的机器人ID
    summary_text: str  # 总结的文本内容
    event_ids_covered: list[str] = field(default_factory=list) # 此总结覆盖的事件ID列表
```

## 2. 新建数据库服务 (SummaryStorageService)

创建一个新的服务文件 `AIcarusCore/src/database/services/summary_storage_service.py`。
该服务将包含一个 `save_summary` 方法，负责接收总结数据并将其写入数据库的 `conversation_summaries` 集合中。

## 3. 修改核心逻辑 (FocusChatCycler)

在 `AIcarusCore/src/sub_consciousness/focus_chat_cycler.py` 中，修改会话结束的逻辑（例如在 `shutdown` 或 `_handle_end_focus_chat_if_needed` 方法中）。
在会话停用前，调用 `SummaryStorageService.save_summary` 方法，将 `self.session.current_handover_summary` 以及必要的上下文信息（如 `conversation_id`, `bot_id` 等）存入数据库。
数据库写入操作需要包含在 `try...except` 块中，以防止数据库问题影响主逻辑。

## 4. 依赖注入

- 在 `main.py` 中实例化 `SummaryStorageService`。
- 将 `SummaryStorageService` 的实例通过 `ChatSessionManager` 和 `ChatSession` 的构造函数，最终注入到 `FocusChatCycler` 中，使其可以调用。
