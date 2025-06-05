# src/database/services/event_storage_service.py
import asyncio
import time
import uuid
from typing import Any, Dict, List, Optional

from src.database.core.connection_manager import ArangoDBConnectionManager, CoreDBCollections # 使用 CoreDBCollections
from arango.exceptions import DocumentInsertError # ArangoDB 特定异常

from src.common.custom_logging.logger_manager import get_logger # 日志记录器

logger = get_logger("AIcarusCore.DB.EventService")

class EventStorageService:
    """服务类，负责所有与事件（Events）相关的存储操作。"""
    COLLECTION_NAME = CoreDBCollections.EVENTS # 使用 CoreDBCollections 定义的常量

    def __init__(self, conn_manager: ArangoDBConnectionManager):
        self.conn_manager = conn_manager
        self.logger = logger

    async def initialize_infrastructure(self) -> None:
        """确保事件集合及其特定索引已创建。应在系统启动时调用。"""
        index_definitions = CoreDBCollections.INDEX_DEFINITIONS.get(self.COLLECTION_NAME, [])
        await self.conn_manager.ensure_collection_with_indexes(self.COLLECTION_NAME, index_definitions)
        self.logger.info(f"'{self.COLLECTION_NAME}' 集合及其特定索引已初始化。")

    async def save_event_document(self, event_doc_data: Dict[str, Any]) -> bool:
        """
        将一个已预处理和格式化的事件文档（字典）保存到数据库。
        期望 `event_doc_data` 中包含 'event_id'，它将被用作文档的 '_key'。
        """
        if not event_doc_data or not isinstance(event_doc_data, dict):
            self.logger.warning("无效的 'event_doc_data' (空或非字典类型)。无法保存事件。")
            return False
        
        # 确保 _key 和 event_id 存在且一致
        event_id = event_doc_data.get("event_id")
        if not event_id: # 如果上层转换逻辑没有提供 event_id，则生成一个
            event_id = str(uuid.uuid4())
            event_doc_data["event_id"] = event_id
        event_doc_data["_key"] = str(event_id) # _key 必须是字符串

        # 确保时间戳是整数（毫秒）
        ts = event_doc_data.get("timestamp", time.time() * 1000.0) # 如果没有，则使用当前时间
        event_doc_data["timestamp"] = int(ts)

        # （可选）可以在此处添加对 event_doc_data 其他字段的校验或规范化

        try:
            # 获取集合实例 (内部会确保集合存在)
            collection = await self.conn_manager.get_collection(self.COLLECTION_NAME)
            # 尝试插入，如果 _key 已存在则不覆盖 (overwrite=False)
            await asyncio.to_thread(collection.insert, event_doc_data, overwrite=False)
            # self.logger.debug(f"事件文档 '{event_id}' 已成功保存。") # 日志可能过于频繁
            return True
        except DocumentInsertError:
            # 如果文档由于 _key 已存在而插入失败，通常认为是数据已存在，可视为操作成功或警告
            self.logger.warning(f"尝试插入已存在的事件 Event ID: {event_id}。操作被跳过。")
            return True # 已经存在，也算“成功”保存（或已保存）
        except Exception as e:
            self.logger.error(f"保存事件文档 '{event_id}' 失败: {e}", exc_info=True)
            return False

    async def get_recent_chat_message_documents(
        self, duration_minutes: int = 10, conversation_id: Optional[str] = None, limit: int = 50
    ) -> List[Dict[str, Any]]:
        """
        获取最近的聊天消息文档。
        注意：此方法返回的是数据库原始文档（字典列表）。
        上层逻辑（如 CoreLogic）需要将这些文档转换为运行时对象（如 ProtocolEvent 或 DBEventDocument 实例）。
        """
        try:
            current_time_ms = int(time.time() * 1000.0)
            threshold_time_ms = current_time_ms - (duration_minutes * 60 * 1000)

            filters = ["doc.timestamp >= @threshold_time", "doc.event_type LIKE 'message.%'"]
            bind_vars: Dict[str, Any] = {"threshold_time": threshold_time_ms, "limit": limit}

            if conversation_id:
                # 查询时应使用数据库中实际用于存储和索引会话ID的字段名
                # 假设 DBEventDocument 模型中，从协议转换后，conversation_id 存在于 'conversation_id_extracted'
                filters.append("doc.conversation_id_extracted == @conversation_id")
                bind_vars["conversation_id"] = conversation_id
            
            # 使用 @@collection 将集合名称作为绑定变量传入，更安全
            query = f"""
                FOR doc IN @@collection 
                    FILTER {(" AND ".join(filters))}
                    SORT doc.timestamp DESC 
                    LIMIT @limit
                    RETURN doc
            """
            bind_vars["@collection"] = self.COLLECTION_NAME # 将集合名称绑定到查询

            results = await self.conn_manager.execute_query(query, bind_vars)
            return results if results is not None else [] # execute_query 在错误时返回 None
        except Exception as e:
            # 捕获execute_query可能未捕获的其他错误，或在准备阶段的错误
            self.logger.error(f"获取最近聊天消息文档失败 (会话ID: {conversation_id}): {e}", exc_info=True)
            return []