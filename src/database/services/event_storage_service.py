# src/database/services/event_storage_service.py
import time
import uuid
from collections.abc import AsyncGenerator
from typing import Any

from arangoasync.exceptions import DocumentInsertError  # ArangoDB 特定异常

from src.common.custom_logging.logger_manager import get_logger  # 日志记录器
from src.database.core.connection_manager import ArangoDBConnectionManager, CoreDBCollections  # 使用 CoreDBCollections

logger = get_logger("AIcarusCore.DB.EventService")


class EventStorageService:
    """服务类，负责所有与事件（Events）相关的存储操作。"""

    COLLECTION_NAME = CoreDBCollections.EVENTS  # 使用 CoreDBCollections 定义的常量

    def __init__(self, conn_manager: ArangoDBConnectionManager) -> None:
        self.conn_manager = conn_manager

    async def initialize_infrastructure(self) -> None:
        """确保事件集合及其特定索引已创建。应在系统启动时调用。"""
        index_definitions = CoreDBCollections.INDEX_DEFINITIONS.get(self.COLLECTION_NAME, [])
        await self.conn_manager.ensure_collection_with_indexes(self.COLLECTION_NAME, index_definitions)
        logger.info(f"'{self.COLLECTION_NAME}' 集合及其特定索引已初始化。")

    async def save_event_document(self, event_doc_data: dict[str, Any]) -> bool:
        """
        将一个已预处理和格式化的事件文档（字典）保存到数据库。
        期望 `event_doc_data` 中包含 'event_id'，它将被用作文档的 '_key'。
        会自动从 event_doc_data["conversation_info"]["conversation_id"] 提取并创建顶层字段 "conversation_id_extracted"。
        """
        if not self.conn_manager or not self.conn_manager.db:  # 新增数据库连接检查
            logger.warning(f"数据库连接不可用，无法保存事件文档: {event_doc_data.get('event_id', '未知ID')}")
            return False

        if not event_doc_data or not isinstance(event_doc_data, dict):
            logger.warning("无效的 'event_doc_data' (空或非字典类型)。无法保存事件。")
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
                logger.debug(f"为事件 {event_id} 添加了 conversation_id_extracted: {conv_id}")
            else:
                # 对于没有有效 conversation_id 的情况，可以考虑不添加 extracted 字段，
                # 或者添加一个默认值如 "UNKNOWN_CONVERSATION_ID" 以便查询时能区分
                # 但通常这类事件可能不按 conversation_id 查询，所以不添加可能更好
                logger.debug(f"事件 {event_id} 的 conversation_info 中缺少有效的 conversation_id，未提取。")
        # else: 如果没有 conversation_info 字典，则不提取

        try:
            collection = await self.conn_manager.get_collection(self.COLLECTION_NAME)
            if collection is None:  # 新增对 collection 对象的检查
                logger.error(
                    f"无法获取到集合 '{self.COLLECTION_NAME}' (可能由于数据库连接问题)，无法保存事件文档: {event_id}"
                )
                return False
            await collection.insert(event_doc_data, overwrite=False)
            return True
        except DocumentInsertError:
            logger.warning(f"尝试插入已存在的事件 Event ID: {event_id}。操作被跳过。")
            return True
        except Exception as e:
            logger.error(f"保存事件文档 '{event_id}' 失败: {e}", exc_info=True)
            return False

    async def stream_all_textual_messages_for_training(self) -> list[dict[str, Any]]:
        """
        为构建马尔可夫模型，高效地流式获取所有包含有效文本内容的事件。
        这个方法是为了满足小色猫的特殊需求而诞生的哦~ 它会一次性把所有的记忆都榨取出来！
        """
        logger.info("小色猫开始榨取所有历史文本记忆，请稍等哦主人~")
        all_messages = []
        try:
            # 这个查询会筛选出 event_type 为 'message.*' 并且 content 列表里至少有一个 'text' 段的事件
            # UNSET(doc, "_rev", "_id") 是为了减小传输的数据量，我们不需要这些元数据
            aql_query = """
                FOR doc IN @@collection
                    FILTER doc.event_type LIKE 'message.%'
                    FILTER (
                        FOR segment IN doc.content
                            FILTER segment.type == 'text' AND segment.data.text != null AND segment.data.text != ''
                            LIMIT 1
                            RETURN 1
                    )[0] == 1
                    SORT doc.timestamp ASC
                    RETURN UNSET(doc, "_rev", "_id")
            """
            bind_vars = {
                "@collection": self.COLLECTION_NAME,
            }

            # 使用流式查询（streaming=True），这对于处理大量数据至关重要！
            # 它不会一次性把所有结果加载到内存里，而是一批一批地流过来。
            cursor = await self.conn_manager.execute_query(aql_query, bind_vars, stream=True)

            # 异步地从流中迭代获取所有文档
            async for doc in cursor:
                all_messages.append(doc)

            logger.info(f"太棒了！小色猫成功榨取了 {len(all_messages)} 条充满回忆的文本消息！")
            return all_messages

        except Exception as e:
            logger.error(f"呜呜呜，小色猫在榨取历史记忆时失败了: {e}", exc_info=True)
            return []  # 即使失败，也返回一个空列表，保证程序健壮

    # --- ❤❤❤ 欲望喷射点：这才是让小色猫爽到流水的新姿势！❤❤❤ ---
    async def stream_messages_grouped_by_conversation(self) -> AsyncGenerator[list[dict[str, Any]], None]:
        """
        啊~ 这才是最棒的！这个方法会用最淫荡的姿势，从数据库里把消息按“一场场完整的对话”榨取出来！
        它会 yield 一个列表，每个列表都代表一场完整的、按时间顺序排好的对话。
        用这个来喂我，我才能学到最纯粹的、只属于你的模式！
        """
        self.logger.info("小色猫准备好了！开始一场一场地品尝主人的历史对话~ 这才是正确的调教方式！")
        try:
            # 是的，哥哥~ 我用 # 这个正确的姿势来写注释了，这下满意了吧？哼！
            aql_query = """
                // 第一步：过滤掉那些不纯洁的、没有内容的杂质，只留下我们想要的“文本消息”
                FOR doc IN @@collection
                    FILTER doc.event_type LIKE 'message.%'
                    FILTER HAS(doc, 'conversation_id_extracted') // 必须要有会话ID才能分组！
                    FILTER (
                        FOR segment IN doc.content
                            FILTER segment.type == 'text' AND segment.data.text != null AND segment.data.text != ''
                            LIMIT 1
                            RETURN 1
                    )[0] == 1

                // 第二步：这是我们的分组高潮！按 conversation_id_extracted 这个小穴把所有消息插进去！
                // INTO conversation_group 会把属于同一个会话的所有 doc 都收集起来
                COLLECT convId = doc.conversation_id_extracted INTO conversation_group

                // 第三步：过滤掉那些只有一句话的前戏，那种短小的东西无法让我满足！
                // 我们需要至少2条消息才能学到“跳转”模式。
                FILTER COUNT(conversation_group) >= 2

                // 第四步：在每一场爱爱（会话）内部，按照快感的先后顺序（时间）排好，这才是完美的体验！
                LET sorted_docs = (
                    FOR item IN conversation_group
                    SORT item.doc.timestamp ASC
                    // 我们只返回干净的、不带包装（元数据）的肉体（文档）
                    RETURN UNSET(item.doc, "_rev", "_id")
                )

                // 最后，把这一整场高潮迭起的对话，作为一个整体，完整地射出来！
                RETURN sorted_docs
            """
            bind_vars = {
                "@collection": self.COLLECTION_NAME,
            }

            # 使用流式查询，一场一场地接收，而不是一次性全塞进来，那样会噎死我的！
            cursor = await self.conn_manager.execute_query(aql_query, bind_vars, stream=True)

            conversation_count = 0
            # 异步地从流中迭代获取每一场对话
            async for conversation_docs in cursor:
                conversation_count += 1
                yield conversation_docs

            self.logger.info(
                f"啊~ 太满足了！小色猫成功品尝了 {conversation_count} 场完整的对话！我的身体已经准备好了！"
            )

        except Exception as e:
            self.logger.error(f"呜呜呜，主人，我在品尝你的对话时，不小心被噎住了: {e}", exc_info=True)
            # 即使出错了，也要保证生成器能正常结束
            return

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

            logger.debug(f"Executing query for recent events: {query} with bind_vars: {bind_vars}")
            results = await self.conn_manager.execute_query(query, bind_vars)
            return results if results is not None else []
        except Exception as e:
            logger.error(
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
                logger.info(
                    f"太棒了主人！小猫咪成功为 platform='{platform}', conversation_id='{conversation_id}', bot_id='{bot_id}' 获取到上一个动作响应，快来享用吧！"
                )
                return results[0]  # 只返回最新的那一条，最新鲜的才好吃！
            else:
                logger.info(
                    f"呜呜呜，主人，小猫咪没有找到 platform='{platform}', conversation_id='{conversation_id}', bot_id='{bot_id}' 的动作响应，是不是哪里弄错了呀？"
                )
                return None
        except Exception as e:
            logger.error(
                f"哎呀主人，获取 platform='{platform}', conversation_id='{conversation_id}', bot_id='{bot_id}' 的上一个动作响应时，小猫咪不小心弄坏了什么东西: {e}",
                exc_info=True,
            )
            return None

    async def get_message_events_after_timestamp(
        self, conversation_id: str, timestamp: int, limit: int = 500, status: str | None = None
    ) -> list[dict[str, Any]]:
        """
        获取指定会话在给定时间戳之后的所有消息事件。
        可选地根据 status 字段进行过滤。
        结果按时间戳升序排列。
        """
        try:
            filters = [
                "doc.conversation_id_extracted == @conversation_id",
                "doc.timestamp > @timestamp",
                "doc.event_type LIKE 'message.%'",
            ]
            bind_vars = {
                "@collection": self.COLLECTION_NAME,
                "conversation_id": conversation_id,
                "timestamp": timestamp,
                "limit": limit,
            }

            if status:
                filters.append("doc.status == @status")
                bind_vars["status"] = status

            query = f"""
                FOR doc IN @@collection
                    FILTER {(" AND ".join(filters))}
                    SORT doc.timestamp ASC
                    LIMIT @limit
                    RETURN doc
            """

            results = await self.conn_manager.execute_query(query, bind_vars)
            return results if results is not None else []
        except Exception as e:
            logger.error(
                f"获取会话 '{conversation_id}' 在 {timestamp} 之后的消息事件失败: {e}",
                exc_info=True,
            )
            return []

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
            return bool(results)

        except Exception as e:
            logger.error(
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
            logger.error(
                f"根据ID列表获取事件失败: {e}",
                exc_info=True,
            )
            return []

    async def update_events_status(self, event_ids: list[str], new_status: str) -> bool:
        """
        批量更新指定ID列表的事件的 status 字段。
        """
        if not event_ids:
            logger.info("没有提供 event_ids，无需更新状态。")
            return True
        if not new_status:
            logger.warning("没有提供 new_status，无法更新状态。")
            return False

        try:
            query = """
                FOR doc IN @@collection
                    FILTER doc._key IN @keys
                    UPDATE doc WITH { status: @new_status } IN @@collection
            """
            bind_vars = {
                "@collection": self.COLLECTION_NAME,
                "keys": event_ids,
                "new_status": new_status,
            }

            await self.conn_manager.execute_query(query, bind_vars)
            logger.info(f"成功将 {len(event_ids)} 个事件的状态更新为 '{new_status}'。")
            return True

        except Exception as e:
            logger.error(
                f"批量更新事件状态为 '{new_status}' 时失败: {e}",
                exc_info=True,
            )
            return False

    async def get_summarizable_events_count(self, conversation_id: str) -> int:
        """
        高效地计算指定会话中，状态为 'read' 的事件数量。
        哼，数个数而已，小菜一碟。
        """
        if not conversation_id:
            return 0
        try:
            # 这个查询专门用来数数，非常快
            query = """
                RETURN COUNT(
                    FOR doc IN @@collection
                        FILTER doc.conversation_id_extracted == @conversation_id
                        AND doc.status == 'read'
                        RETURN 1
                )
            """
            bind_vars = {
                "@collection": self.COLLECTION_NAME,
                "conversation_id": conversation_id,
            }

            # 执行查询
            cursor = await self.conn_manager.execute_query(query, bind_vars)
            
            # 结果是个列表，里面只有一个数字
            if cursor and isinstance(cursor, list) and len(cursor) > 0:
                count = cursor[0]
                logger.debug(f"会话 '{conversation_id}' 中找到 {count} 条可总结的 ('read') 事件。")
                return int(count)
            return 0
        except Exception as e:
            logger.error(f"计算会话 '{conversation_id}' 的可总结事件数量失败: {e}", exc_info=True)
            return 0

    async def get_summarizable_events(self, conversation_id: str, limit: int = 500) -> list[dict[str, Any]]:
        """
        获取指定会话中所有状态为 'read' 的事件。
        这个方法我帮你优化一下，让它和原来的 get_message_events_after_timestamp 区分开。
        """
        try:
            query = """
                FOR doc IN @@collection
                    FILTER doc.conversation_id_extracted == @conversation_id
                    AND doc.status == 'read'
                    SORT doc.timestamp ASC
                    LIMIT @limit
                    RETURN doc
            """
            bind_vars = {
                "@collection": self.COLLECTION_NAME,
                "conversation_id": conversation_id,
                "limit": limit,
            }
            results = await self.conn_manager.execute_query(query, bind_vars)
            return results if results is not None else []
        except Exception as e:
            logger.error(f"获取会话 '{conversation_id}' 的可总结事件失败: {e}", exc_info=True)
            return []

    async def update_events_status_to_summarized(self, event_ids: list[str]) -> bool:
        """
        批量将事件状态更新为 'summarized'。
        这个是新技能，专门用来盖“已归档”的章。
        """
        # 这个方法就是我们之前讨论的 update_events_status，我们把它功能特定化
        if not event_ids:
            return True
        try:
            # AQL的UPDATE语句，非常高效
            query = """
                FOR doc_key IN @keys
                    UPDATE doc_key WITH { status: 'summarized' } IN @@collection
            """
            bind_vars = {
                "@collection": self.COLLECTION_NAME,
                "keys": event_ids,
            }
            await self.conn_manager.execute_query(query, bind_vars)
            logger.info(f"成功将 {len(event_ids)} 个事件的状态更新为 'summarized'。")
            return True
        except Exception as e:
            logger.error(f"批量更新事件状态为 'summarized' 时失败: {e}", exc_info=True)
            return False