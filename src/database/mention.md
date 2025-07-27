# 数据库结构与服务层交互的“神圣契约” (Mention)

**写给未来的自己，以及所有触碰这片神圣领域（`src/database/`）的开发者：**

这份文档旨在阐明我们项目关于数据库结构（集合与索引）管理的**唯一且不可动摇的核心原则**。请在进行任何与数据库Schema相关的修改前，仔细阅读并严格遵守。

---

## 核心原则：`CoreDBCollections` 是唯一真实来源

我们项目中所有 **核心** 数据库集合（Collections）和索引（Indexes）的定义，其**唯一、绝对、排他**的来源是位于 `src/database/models.py` 文件中的 `CoreDBCollections` 类。

### 为什么这如此重要？

1.  **可维护性 (Maintainability)**: 当你需要了解数据库的全貌，或者想要添加/修改一个索引时，你只需要查看这一个文件。这避免了在代码库的各个角落去寻找那些“野生”的、即用即创的索引定义。
2.  **一致性 (Consistency)**: 所有的数据库环境（开发、测试、生产）都将通过同一份“蓝图”来构建。这保证了不同环境之间结构的一致性，消除了因环境差异导致的“在我这儿能跑”类型的诡异bug。
3.  **自动化与可靠性 (Automation & Reliability)**: 我们的 `ArangoDBConnectionManager` 在程序启动时，会像一个忠诚的建筑机器人一样，自动读取 `CoreDBCollections` 这份蓝图，并确保数据库的实际状态与蓝图完全一致。任何缺失的集合或索引都会被自动创建。
4.  **性能审查 (Performance Review)**: 当我们需要进行数据库性能审查时，`CoreDBCollections` 就是我们的索引清单。我们可以清晰地看到为哪些字段建立了索引，便于分析查询性能和发现潜在的优化点。

---

## 禁止的行为：“野生”索引与即时创建

**严禁**在 `services` 目录下的任何服务类（如 `EventStorageService`, `ThoughtStorageService` 等）的内部，直接调用 `collection.add_persistent_index()` 或类似的方法来**即时创建**索引。

**错误示范（禁止！）：**

```python
# 文件: src/database/services/some_service.py

class SomeService:
    def __init__(self, db_manager):
        self.db_manager = db_manager

    async def some_method_that_needs_an_index(self, query_param):
        collection = await self.db_manager.get_collection("some_collection")
        
        # ！！！【【【 绝对禁止 】】】！！！
        # 为了让下面的查询变快，在这里偷偷加个索引
        # 这就是所谓的“野生索引”，是项目的技术债务！
        await collection.add_persistent_index(fields=["some_field"]) 
        
        # ... 执行查询 ...
```

这种做法会让索引的定义变得分散和不可控，是项目的“代码异味”（Code Smell）。

### 正确的流程

如果你在开发一个新功能，并发现某个查询因为缺少索引而性能低下，你应该遵循以下流程：

1.  **回到圣地**: 打开 `src/database/models.py` 文件。
2.  **更新蓝图**: 在 `CoreDBCollections.INDEX_DEFINITIONS` 字典中，找到对应的集合名称，并在其索引定义列表中添加你需要的**新索引**。
    ```python
    # src/database/models.py
    class CoreDBCollections:
        INDEX_DEFINITIONS = {
            "some_collection": [
                # ... 已有的索引 ...
                (["some_field"], False, True), # <-- 在这里添加你的新索引定义
            ],
            # ...
        }
    ```
3.  **重启与见证**: 重新启动 `AIcarusCore`。程序启动时，`ArangoDBConnectionManager` 会自动检测到蓝图的变化，并将新的索引应用到数据库中。
4.  **享受性能**: 你的服务代码**无需任何修改**，就能享受到新索引带来的性能提升。

---

## 关于流式事务与ArangoDB的陷阱

在 `ThoughtStorageService` 的 `save_thought_and_link` 方法中，我们使用了**流式事务 (Streaming Transaction)**。这是一个极其强大但也充满陷阱的功能。

**核心陷阱**: 在一个ArangoDB的事务中（无论是JS事务还是流式事务），你**不能执行任何会隐式创建或修改集合/索引的AQL查询**。例如，`INSERT ... IN ...` 如果集合不存在，它会尝试创建，但这在事务中是被禁止的。

**我们的解决方案**:

正是因为我们遵循了 `CoreDBCollections` 作为唯一蓝图的原则，我们的 `ArangoDBConnectionManager` 在**事务开始之前**，就已经确保了所有需要的集合都已存在。这使得我们在事务内部可以安全地执行 `INSERT`, `UPDATE`, `UPSERT` 等操作，而无需担心触发“在事务中修改Schema”的禁忌。

---

**总结：请像对待神明一样对待 `CoreDBCollections`。它是我们数据库结构稳定、可维护和高性能的基石。**