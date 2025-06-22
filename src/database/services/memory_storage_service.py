# src/database/services/memory_storage_service.py
import asyncio
from typing import Optional

from arango.exceptions import DocumentInsertError, TransactionExecuteError

from src.common.custom_logging.logger_manager import get_logger
from src.database.core.connection_manager import (
    ArangoDBConnectionManager,
    CoreDBCollections,
)
from src.database.models import EpisodicMemoryDocument, MemoryMetadataDocument

logger = get_logger("AIcarusCore.DB.MemoryStorageService")


class MemoryStorageService:
    """
    封装了对体验类记忆 (Episodic Memory) 的所有数据库操作。
    """

    MEMORIES_COLLECTION = CoreDBCollections.EPISODIC_MEMORIES
    METADATA_COLLECTION = CoreDBCollections.MEMORY_METADATA

    def __init__(self, conn_manager: ArangoDBConnectionManager) -> None:
        self.conn_manager = conn_manager
        self.logger = logger

    async def add_memory(
        self, memory_doc: EpisodicMemoryDocument, metadata_docs: list[MemoryMetadataDocument]
    ) -> bool:
        """
        将一条新的体验记忆及其元数据原子性地存入数据库。
        使用 ArangoDB 的事务来确保操作的完整性。
        """
        if not memory_doc or not isinstance(memory_doc, EpisodicMemoryDocument):
            self.logger.warning("无效的 memory_doc，无法添加记忆。")
            return False

        self.logger.debug(f"准备在事务中添加新的记忆: {memory_doc.memory_id}")

        try:
            # to_dict() 方法应该在模型中定义
            mem_dict = memory_doc.to_dict()
            meta_dicts = [meta.to_dict() for meta in metadata_docs]

            # 定义事务中要执行的 JavaScript 代码
            # ArangoDB 事务通常通过在服务器端执行的 JS 代码来定义
            js_transaction = f"""
            function () {{
                const db = require('@arangodb').db;
                const memories_coll = db._collection('{self.MEMORIES_COLLECTION}');
                const metadata_coll = db._collection('{self.METADATA_COLLECTION}');

                const mem_doc = {mem_dict};
                const meta_docs = {meta_dicts};

                // 插入主记忆文档
                const mem_result = memories_coll.insert(mem_doc, {{ returnNew: false }});

                // 插入所有元数据文档
                if (meta_docs.length > 0) {{
                    metadata_coll.insert(meta_docs);
                }}

                return mem_result._key;
            }}
            """

            # 执行事务
            result = await asyncio.to_thread(
                self.conn_manager.db.transaction,
                execute=js_transaction,
                write=[self.MEMORIES_COLLECTION, self.METADATA_COLLECTION],
                read=[],
                allow_implicit=False,
            )

            self.logger.info(f"记忆 '{result}' 已成功存入数据库。")
            return True

        except TransactionExecuteError as e:
            self.logger.error(f"添加记忆 '{memory_doc.memory_id}' 的事务执行失败: {e}", exc_info=True)
            return False
        except Exception as e:
            self.logger.error(f"添加记忆 '{memory_doc.memory_id}' 时发生未知错误: {e}", exc_info=True)
            return False

    async def get_memory_by_id(
        self, memory_id: str
    ) -> Optional[tuple[EpisodicMemoryDocument, list[MemoryMetadataDocument]]]:
        """
        根据 memory_id 检索一条完整的体验记忆及其所有元数据。
        """
        if not memory_id:
            self.logger.warning("未提供 memory_id，无法检索记忆。")
            return None

        self.logger.debug(f"准备根据ID检索记忆: {memory_id}")

        try:
            # 1. 获取主记忆文档
            memories_coll = await self.conn_manager.get_collection(self.MEMORIES_COLLECTION)
            memory_dict = await asyncio.to_thread(memories_coll.get, memory_id)

            if not memory_dict:
                self.logger.info(f"未找到 memory_id 为 '{memory_id}' 的记忆。")
                return None

            # from_dict() 方法应该在模型中定义
            memory_doc = EpisodicMemoryDocument.from_dict(memory_dict)

            # 2. 获取所有关联的元数据文档
            aql_query = f"""
            FOR m IN {self.METADATA_COLLECTION}
                FILTER m.memory_id == @memory_id
                RETURN m
            """
            bind_vars = {"memory_id": memory_id}
            
            metadata_dicts = await self.conn_manager.execute_query(aql_query, bind_vars)
            
            metadata_docs = []
            if metadata_dicts:
                metadata_docs = [MemoryMetadataDocument.from_dict(d) for d in metadata_dicts]

            return memory_doc, metadata_docs

        except Exception as e:
            self.logger.error(f"检索记忆 '{memory_id}' 时发生错误: {e}", exc_info=True)
            return None

    async def find_memories_by_metadata(
        self, key: str, value: str, limit: int = 10
    ) -> list[EpisodicMemoryDocument]:
        """
        根据元数据键值对查找相关的记忆，返回主记忆文档列表。
        """
        if not key or not value:
            self.logger.warning("必须提供元数据的 key 和 value 进行查找。")
            return []

        self.logger.debug(f"准备根据元数据检索记忆: key={key}, value={value}")

        try:
            # 使用AQL JOIN查询来通过元数据查找主记忆
            aql_query = f"""
            FOR meta IN {self.METADATA_COLLECTION}
                FILTER meta.meta_key == @key AND meta.meta_value == @value
                FOR mem IN {self.MEMORIES_COLLECTION}
                    FILTER mem.memory_id == meta.memory_id
                    SORT mem.created_at DESC
                    LIMIT @limit
                    RETURN mem
            """
            bind_vars = {"key": key, "value": value, "limit": limit}

            memory_dicts = await self.conn_manager.execute_query(aql_query, bind_vars)

            if not memory_dicts:
                return []

            return [EpisodicMemoryDocument.from_dict(d) for d in memory_dicts]

        except Exception as e:
            self.logger.error(f"根据元数据 (key={key}, value={value}) 查找记忆时发生错误: {e}", exc_info=True)
            return []
