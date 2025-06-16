# src/database/services/event_storage_service.py
import asyncio
import time
import uuid
from typing import Any

from arango.exceptions import DocumentInsertError  # ArangoDB 特定异常

from src.common.custom_logging.logger_manager import get_logger  # 日志记录器
from src.database.core.connection_manager import ArangoDBConnectionManager, CoreDBCollections  # 使用 CoreDBCollections

logger = get_logger("AIcarusCore.DB.EventService")


class EventStorageService:
    """服务类，负责所有与事件（Events）相关的存储操作。"""

    COLLECTION_NAME = CoreDBCollections.EVENTS  # 使用 CoreDBCollections 定义的常量

    def __init__(self, conn_manager: ArangoDBConnectionManager) -> None:
        self.conn_manager = conn_manager
        self.logger = logger

    async def initialize_infrastructure(self) -> None:
        """确保事件集合及其特定索引已创建。应在系统启动时调用。"""
        index_definitions = CoreDBCollections.INDEX_DEFINITIONS.get(self.COLLECTION_NAME, [])
        await self.conn_manager.ensure_collection_with_indexes(self.COLLECTION_NAME, index_definitions)
        self.logger.info(f"'{self.COLLECTION_NAME}' 集合及其特定索引已初始化。")

    async def save_event_document(self, event_doc_data: dict[str, Any]) -> bool:
        """
        将一个已预处理和格式化的事件文档（字典）保存到数据库。
        期望 `event_doc_data` 中包含 'event_id'，它将被用作文档的 '_key'。
        会自动从 event_doc_data["conversation_info"]["conversation_id"] 提取并创建顶层字段 "conversation_id_extracted"。
        """
        if not self.conn_manager or not self.conn_manager.db:  # 新增数据库连接检查
            self.logger.warning(f"数据库连接不可用，无法保存事件文档: {event_doc_data.get('event_id', '未知ID')}")
            return False

        if not event_doc_data or not isinstance(event_doc_data, dict):
            self.logger.warning("无效的 'event_doc_data' (空或非字典类型)。无法保存事件。")
            return False

        event_id = event_doc_data.get("event_id")
        if not event_id:
            event_id = str(uuid.uuid4())
            event_doc_data["event_id"] = event_id
        event_doc_data["_key"] = str(event_id)

        ts = event_doc_data.get("timestamp", time.time() * 1000.0)
        event_doc_data["timestamp"] = int(ts)

        # --- 新增逻辑：提取 conversation_id 到顶层 ---
        conversation_info = event_doc_data.get("conversation_info")
        if isinstance(conversation_info, dict):
            conv_id = conversation_info.get("conversation_id")
            if isinstance(conv_id, str) and conv_id:
                event_doc_data["conversation_id_extracted"] = conv_id
                self.logger.debug(f"为事件 {event_id} 添加了 conversation_id_extracted: {conv_id}")
            else:
                # 对于没有有效 conversation_id 的情况，可以考虑不添加 extracted 字段，
                # 或者添加一个默认值如 "UNKNOWN_CONVERSATION_ID" 以便查询时能区分
                # 但通常这类事件可能不按 conversation_id 查询，所以不添加可能更好
                self.logger.debug(f"事件 {event_id} 的 conversation_info 中缺少有效的 conversation_id，未提取。")
        # else: 如果没有 conversation_info 字典，则不提取

        # --- 新增逻辑：为消息类型事件添加 is_processed 字段 ---
        if event_doc_data.get("event_type", "").startswith("message."):
            event_doc_data["is_processed"] = False
            self.logger.debug(f"为消息事件 {event_id} 添加了 is_processed=False")
        # --- 结束新增逻辑 ---

        try:
            collection = await self.conn_manager.get_collection(self.COLLECTION_NAME)
            if collection is None:  # 新增对 collection 对象的检查
                self.logger.error(
                    f"无法获取到集合 '{self.COLLECTION_NAME}' (可能由于数据库连接问题)，无法保存事件文档: {event_id}"
                )
                return False
            await asyncio.to_thread(collection.insert, event_doc_data, overwrite=False)
            return True
        except DocumentInsertError:
            self.logger.warning(f"尝试插入已存在的事件 Event ID: {event_id}。操作被跳过。")
            return True
        except Exception as e:
            self.logger.error(f"保存事件文档 '{event_id}' 失败: {e}", exc_info=True)
            return False

    async def get_recent_chat_message_documents(
        self,
        duration_minutes: int = 0,  # 默认不按时间筛选，主要靠limit
        conversation_id: str | None = None,
        exclude_conversation_id: str | None = None,
        limit: int = 50,
        fetch_all_event_types: bool = False,
    ) -> list[dict[str, Any]]:
        """
        获取最近的事件文档。主要根据 limit 获取数量，duration_minutes 作为可选的时间窗口限制。
        默认 (fetch_all_event_types=False) 只获取聊天消息 (event_type LIKE 'message.%')。
        当 fetch_all_event_types=True 时，获取所有类型的事件（仍受其他过滤器如conversation_id影响）。
        """
        try:
            filters = []
            bind_vars: dict[str, Any] = {"limit": limit}

            if duration_minutes > 0:  # 如果指定了有效的时间窗口，则添加时间过滤
                current_time_ms = int(time.time() * 1000.0)
                threshold_time_ms = current_time_ms - (duration_minutes * 60 * 1000)
                filters.append("doc.timestamp >= @threshold_time")
                bind_vars["threshold_time"] = threshold_time_ms

            if not fetch_all_event_types:
                filters.append("doc.event_type LIKE 'message.%'")

            if conversation_id:
                filters.append("doc.conversation_id_extracted == @conversation_id")
                bind_vars["conversation_id"] = conversation_id

            if exclude_conversation_id:
                filters.append("doc.conversation_id_extracted != @exclude_conversation_id")
                bind_vars["exclude_conversation_id"] = exclude_conversation_id

            query_parts = ["FOR doc IN @@collection"]
            if filters:  # 只有当存在其他过滤器时才添加 FILTER 子句
                query_parts.append(f"FILTER {(' AND '.join(filters))}")
            query_parts.append("SORT doc.timestamp DESC")
            query_parts.append("LIMIT @limit")
            query_parts.append("RETURN doc")

            query = "\n".join(query_parts)

            bind_vars["@collection"] = self.COLLECTION_NAME

            self.logger.debug(f"Executing query for recent events: {query} with bind_vars: {bind_vars}")
            results = await self.conn_manager.execute_query(query, bind_vars)
            return results if results is not None else []
        except Exception as e:
            self.logger.error(
                f"获取最近事件文档失败 (会话ID: {conversation_id}, 获取所有类型: {fetch_all_event_types}): {e}",
                exc_info=True,
            )
            return []

    async def get_last_action_response(
        self,
        platform: str,
        conversation_id: str | None = None,
        bot_id: str | None = None,  # 主人，小猫咪在这里加上了 bot_id 哦
    ) -> dict[str, Any] | None:
        """
        获取指定平台和会话的最后一个 'action_response.*' 事件。
        如果提供了 bot_id，则会进一步筛选。
        哼，这个方法可是为了满足主人您特殊的需求才加上的呢，是不是很色情？
        """
        try:
            filters = ["doc.event_type LIKE 'action_response.%'", "doc.platform == @platform"]
            bind_vars: dict[str, Any] = {"platform": platform}

            if conversation_id:
                filters.append("doc.conversation_id_extracted == @conversation_id")  # 主人你看，这里用了 extracted 哦
                bind_vars["conversation_id"] = conversation_id

            if bot_id:  # 如果主人给了 bot_id，小猫咪就用上它
                filters.append("doc.bot_id == @bot_id")
                bind_vars["bot_id"] = bot_id

            # 小猫咪把查询语句写得更色情一点
            query = f"""
                FOR doc IN @@collection
                    FILTER {(" AND ".join(filters))}
                    SORT doc.timestamp DESC
                    LIMIT 1
                    RETURN doc
            """
            # 主人，这里的 @collection 还是我们的小秘密哦
            bind_vars["@collection"] = self.COLLECTION_NAME

            results = await self.conn_manager.execute_query(query, bind_vars)
            if results and len(results) > 0:
                self.logger.info(
                    f"太棒了主人！小猫咪成功为 platform='{platform}', conversation_id='{conversation_id}', bot_id='{bot_id}' 获取到上一个动作响应，快来享用吧！"
                )
                return results[0]  # 只返回最新的那一条，最新鲜的才好吃！
            else:
                self.logger.info(
                    f"呜呜呜，主人，小猫咪没有找到 platform='{platform}', conversation_id='{conversation_id}', bot_id='{bot_id}' 的动作响应，是不是哪里弄错了呀？"
                )
                return None
        except Exception as e:
            self.logger.error(
                f"哎呀主人，获取 platform='{platform}', conversation_id='{conversation_id}', bot_id='{bot_id}' 的上一个动作响应时，小猫咪不小心弄坏了什么东西: {e}",
                exc_info=True,
            )
            return None

    async def get_unprocessed_message_events(
        self,
        conversation_id: str | None = None,
        limit: int = 1000,  # 默认获取大量未处理消息
    ) -> list[dict[str, Any]]:
        """
        获取所有 is_processed = False 且 event_type LIKE 'message.%' 的事件。
        可选按 conversation_id 筛选。
        结果按时间戳升序排列 (旧消息在前)。
        哼，这个方法是专门给 UnreadInfoService 那个小弟用的，别搞错了！
        """
        try:
            filters = ["doc.event_type LIKE 'message.%'", "doc.is_processed == false"]
            bind_vars: dict[str, Any] = {"limit": limit}

            if conversation_id:
                filters.append("doc.conversation_id_extracted == @conversation_id")
                bind_vars["conversation_id"] = conversation_id

            query_parts = ["FOR doc IN @@collection"]
            if filters:
                query_parts.append(f"FILTER {(' AND '.join(filters))}")
            query_parts.append("SORT doc.timestamp ASC")  # 按时间升序
            query_parts.append("LIMIT @limit")
            query_parts.append("RETURN doc")

            query = "\n".join(query_parts)
            bind_vars["@collection"] = self.COLLECTION_NAME

            self.logger.debug(f"Executing query for unprocessed message events: {query} with bind_vars: {bind_vars}")
            results = await self.conn_manager.execute_query(query, bind_vars)
            return results if results is not None else []
        except Exception as e:
            self.logger.error(
                f"获取未处理消息事件失败 (会话ID: {conversation_id}): {e}",
                exc_info=True,
            )
            return []

    async def mark_events_as_processed(self, event_ids: list[str], processed_status: bool = True) -> bool:
        """
        批量更新指定 event_id 列表的事件的 is_processed 状态。
        哼，ChatSession 那个小家伙会用这个来告诉我哪些消息它看过了！
        """
        if not event_ids:
            self.logger.info("没有提供 event_ids，无需更新 is_processed 状态。")
            return True  # 认为操作成功，因为没有事情可做

        if not self.conn_manager or not self.conn_manager.db:
            self.logger.error("数据库连接不可用，无法更新事件的 is_processed 状态。")
            return False

        # ArangoDB的批量更新通常使用 AQL FOR循环 + UPDATE/REPLACE
        # 构建AQL查询
        # 注意：直接在AQL字符串中插入 event_ids 列表可能不是最佳实践，
        # 但对于 _key 的列表，通常可以接受。更好的方式是作为绑定参数，但AQL对数组IN操作符的绑定参数处理可能需要特定格式。
        # 这里我们用一个简单的FOR循环来逐个更新，如果event_ids非常多，可能需要优化。
        # 或者使用一个更复杂的AQL，如：
        # FOR key_val IN @event_keys UPDATE key_val WITH { is_processed: @status } IN @@collection
        # 但这要求 event_ids 列表中的是 _key 值。我们的 event_id 就是 _key。

        # 使用更安全的绑定参数方式
        aql_query = """
        FOR event_key IN @event_keys
            UPDATE event_key WITH { is_processed: @status } IN @@collection
            OPTIONS { ignoreErrors: true } // 如果某个key不存在，忽略错误继续执行
        RETURN { updated: OLD._key, status: NEW.is_processed }
        """
        # ignoreErrors: true 可以防止因某个 event_id 不存在而导致整个批量操作失败。
        # RETURN 子句是可选的，但可以用来确认哪些文档被更新了。

        bind_vars = {
            "@collection": self.COLLECTION_NAME,
            "event_keys": event_ids,  # event_ids 列表应该包含文档的 _key 值
            "status": processed_status,
        }

        try:
            self.logger.info(f"准备批量更新 {len(event_ids)} 个事件的 is_processed 状态为 {processed_status}。")
            # ArangoDB Python驱动的 execute_query 通常用于读操作，
            # 对于写操作，虽然也可以用，但更常见的是直接用 collection.update_many 或类似的。
            # 然而，如果需要复杂的AQL，execute_query 也是可以的。
            # 我们这里用 execute_query 来执行AQL。

            # collection = await self.conn_manager.get_collection(self.COLLECTION_NAME)
            # if collection is None:
            #     self.logger.error(f"无法获取到集合 '{self.COLLECTION_NAME}'，无法更新事件状态。")
            #     return False
            # # 驱动程序可能没有直接的 update_many_by_keys 方法，所以我们用AQL

            update_results = await self.conn_manager.execute_query(aql_query, bind_vars=bind_vars)

            if update_results is not None:
                # update_results 会是一个列表，每个元素是 RETURN 子句返回的字典
                updated_count = len(update_results)
                self.logger.info(f"成功更新了 {updated_count} / {len(event_ids)} 个事件的 is_processed 状态。")
                # 可以根据 updated_count 和 len(event_ids) 的比较来判断是否所有都成功了
                # 但由于 ignoreErrors: true，即使有些key不存在，操作本身也算成功。
                return True
            else:
                # 如果 execute_query 返回 None，通常表示查询执行层面有错误，而不是AQL逻辑错误
                self.logger.error("批量更新 is_processed 状态时，数据库查询执行返回了 None。")
                return False

        except Exception as e:
            self.logger.error(f"批量更新事件的 is_processed 状态失败: {e}", exc_info=True)
            return False

    async def has_new_events_since(self, conversation_id: str, timestamp: float) -> bool:
        """
        高效地检查指定会话中，在给定时间戳之后是否有新的消息事件。
        """
        try:
            query = """
                FOR doc IN @@collection
                    FILTER doc.conversation_id_extracted == @conversation_id
                    FILTER doc.timestamp > @timestamp
                    FILTER doc.event_type LIKE 'message.%'
                    LIMIT 1
                    RETURN 1
            """
            bind_vars = {
                "@collection": self.COLLECTION_NAME,
                "conversation_id": conversation_id,
                "timestamp": timestamp,
            }
            
            results = await self.conn_manager.execute_query(query, bind_vars)
            
            # 如果 results 列表不为空，说明至少找到了一个匹配的文档
            if results:
                return True
            return False
            
        except Exception as e:
            self.logger.error(
                f"检查新事件失败 (会话ID: {conversation_id}): {e}",
                exc_info=True,
            )
            return False

    async def get_events_by_ids(self, event_ids: list[str]) -> list[dict[str, Any]]:
        """
        根据 event_id (_key) 列表，批量获取事件文档。
        """
        if not event_ids:
            return []
        
        try:
            query = """
                FOR doc IN @@collection
                    FILTER doc._key IN @keys
                    RETURN doc
            """
            bind_vars = {
                "@collection": self.COLLECTION_NAME,
                "keys": event_ids,
            }
            
            results = await self.conn_manager.execute_query(query, bind_vars)
            return results if results is not None else []
            
        except Exception as e:
            self.logger.error(
                f"根据ID列表获取事件失败: {e}",
                exc_info=True,
            )
            return []
