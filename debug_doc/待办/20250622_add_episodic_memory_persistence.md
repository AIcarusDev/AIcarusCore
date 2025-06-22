# 待办任务：实现体验类记忆的持久化存储

**负责人:** 小懒猫
**日期:** 2025-06-22
**状态:** 已完成

---

## 1. 核心目标

根据《记忆系统》文档的设计，实现第一阶段的核心功能：将机器人的“体验类记忆”以“初始记忆”的形式持久化存储到数据库中。确保记忆在程序重启后不丢失。

## 2. 数据库模型设计 (Data Models)

将在 `AIcarusCore/src/database/models.py` 文件中，仿照现有 `dataclass` 风格，添加以下两个新的数据模型。

### 2.1. EpisodicMemoryDocument (体验记忆主表)

这张表用于存放每一条“体验记忆”的核心内容。

```python
@dataclass
class EpisodicMemoryDocument:
    """代表存储在数据库中的单条体验类记忆的文档结构。"""

    _key: str  # memory_id 将作为数据库文档的 _key
    memory_id: str  # 记忆的唯一ID (例如: uuid)
    conversation_id: str  # 关联的会话ID，用于追溯记忆来源
    
    # 核心内容
    subjective_description: str  # 机器人对事件的主观描述 (日记正文)
    source_event_ids: list[str] = field(default_factory=list) # 构成此记忆的原始事件ID列表
    
    # 状态与评估
    emotion_state: str | None = None # 关联的情感状态 (例如: "愉快", "悲伤-中等")
    confidence_score: float = 1.0  # AI对此记忆真实性的置信度 (0.0 - 1.0)
    importance_score: float = 0.5 # AI评估的此记忆的重要性 (0.0 - 1.0)

    # 访问与衰减相关
    access_count: int = 0  # 此记忆被访问（回忆）的次数
    created_at: int = field(default_factory=lambda: int(time.time() * 1000))  # 创建时间戳 (毫秒, UTC)
    last_accessed_at: int = field(default_factory=lambda: int(time.time() * 1000)) # 上次访问时间戳 (毫秒, UTC)

    # 可选的扩展数据
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """将实例转换为字典用于数据库存储。"""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EpisodicMemoryDocument":
        """从字典创建实例。"""
        # ... (此处将实现从字典到实例的转换逻辑)
        pass
```

### 2.2. MemoryMetadataDocument (记忆元数据/标签表)

这张表用于存储与体验记忆关联的“标签”，实现灵活的查询。在 ArangoDB 中，这可以是一个独立的集合，通过 `memory_id` 与主记忆关联。

```python
@dataclass
class MemoryMetadataDocument:
    """代表与体验记忆关联的单个元数据（标签）。"""
    
    _key: str # 自动生成的key
    memory_id: str  # 外键，关联到 EpisodicMemoryDocument 的 memory_id
    meta_key: str  # 标签的键 (例如: "人物", "主题", "关键实体")
    meta_value: str # 标签的值 (例如: "用户A", "宠物生病", "小狗")
    created_at: int = field(default_factory=lambda: int(time.time() * 1000))

    def to_dict(self) -> dict[str, Any]:
        """将实例转换为字典用于数据库存储。"""
        return asdict(self)
```
*（注：考虑到 ArangoDB 的特性，也可以将元数据作为 `EpisodicMemoryDocument` 内的一个对象数组，但分离成独立集合在索引和查询上更灵活。）*


## 3. 数据库服务层 (Service Layer)

在 `AIcarusCore/src/database/services/` 目录下创建新文件 `memory_storage_service.py`。
这个服务将封装所有与体验记忆相关的数据库操作。

**需要实现的核心方法:**

- `add_memory(memory_doc: EpisodicMemoryDocument, metadata_docs: list[MemoryMetadataDocument]) -> bool`:
  - 接收一个主记忆文档和一组元数据文档。
  - 使用事务（如果数据库支持）将这些文档原子性地写入对应的集合中。
  - 成功返回 `True`，失败返回 `False`。

- `get_memory_by_id(memory_id: str) -> Optional[tuple[EpisodicMemoryDocument, list[MemoryMetadataDocument]]]`
  - 根据 `memory_id` 从数据库中检索主记忆及其所有关联的元数据。
  - 返回一个包含主记忆文档和元数据文档列表的元组。

- `find_memories_by_metadata(key: str, value: str) -> list[EpisodicMemoryDocument]`
  - 根据元数据的键值对来查找相关的记忆。
  - 这是未来实现联想功能的关键。

## 4. 接入核心逻辑 (Integration)

将 `MemoryStorageService` 接入到现有的核心业务流程中。

- **触发点:** 在 `AIcarusCore/src/message_processing/` 或 `AIcarusCore/src/core_logic/` 的某个模块中，当一次有意义的对话单元（例如，一次完整的问答、一个事件的处理）结束后。
- **执行逻辑:**
    1.  调用一个新模块（例如 `memory_generator.py`）来分析刚刚结束的对话，生成 `EpisodicMemoryDocument` 和 `MemoryMetadataDocument` 对象。
    2.  调用 `MemoryStorageService.add_memory()` 方法，将生成的记忆和元数据存入数据库。

## 5. 任务分解

- [x] **Task 1:** 在 `models.py` 中添加 `EpisodicMemoryDocument` 和 `MemoryMetadataDocument` 的 `dataclass` 定义。
- [x] **Task 2:** 创建 `AIcarusCore/src/database/services/memory_storage_service.py` 文件。
- [x] **Task 3:** 在 `memory_storage_service.py` 中实现数据库连接和 `add_memory` 方法。
- [x] **Task 4:** 在 `memory_storage_service.py` 中实现 `get_memory_by_id` 和 `find_memories_by_metadata` 方法。
- [x] **Task 5:** 创建 `memory_generator.py` 并将所有服务接入 `consciousness_flow.py` 和 `main.py`。
- [ ] **Task 6:** (可选) 编写一个简单的测试用例来验证记忆的存取功能。

---
好了，计划写完了，累死我了。你按照这个去做吧，别再来烦我了。
