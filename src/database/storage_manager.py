"""
存储管理器 - 统一的数据库操作接口
简化设计，专注核心功能
"""

import time
from typing import Any

from arango import ArangoClient
from arango.database import StandardDatabase

from src.common.custom_logging.logger_manager import get_logger
from src.common.utils import format_chat_history_for_prompt

from .models import DATABASE_INDEXES, ActionRecord, Collections, Event

logger = get_logger("AIcarusCore.StorageManager")


class StorageManager:
    """
    统一的存储管理器
    负责所有数据库操作，保持接口简洁
    """

    def __init__(self, db_config: dict[str, Any]) -> None:
        """初始化存储管理器"""
        self.config = db_config
        self.client: ArangoClient | None = None
        self.db: StandardDatabase | None = None
        self._initialized = False

    async def initialize(self) -> bool:
        """初始化数据库连接和集合"""
        try:
            # 创建客户端连接
            self.client = ArangoClient(hosts=self.config.get("host", "http://localhost:8529"))

            # 连接到数据库
            db_name = self.config.get("database_name", "aicarus_core")
            username = self.config.get("username", "root")
            password = self.config.get("password", "")

            self.db = self.client.db(db_name, username=username, password=password)

            # 确保集合存在
            await self._ensure_collections()

            # 确保索引存在
            await self._ensure_indexes()

            self._initialized = True
            logger.info(f"存储管理器初始化成功，数据库: {db_name}")
            return True

        except Exception as e:
            logger.error(f"存储管理器初始化失败: {e}")
            return False

    async def _ensure_collections(self) -> None:
        """确保必要的集合存在"""
        for collection_name in [Collections.EVENTS, Collections.ACTIONS]:
            if not self.db.has_collection(collection_name):
                self.db.create_collection(collection_name)
                logger.info(f"创建集合: {collection_name}")

    async def _ensure_indexes(self) -> None:
        """确保必要的索引存在"""
        for collection_name, indexes in DATABASE_INDEXES.items():
            collection = self.db.collection(collection_name)
            for index_fields in indexes:
                try:
                    collection.add_persistent_index(fields=index_fields, unique=False)
                    logger.debug(f"创建索引: {collection_name}.{index_fields}")
                except Exception as e:
                    # 索引可能已存在，忽略错误
                    logger.debug(f"索引创建跳过: {collection_name}.{index_fields} - {e}")

    # ==================== Event 操作 ====================

    async def store_event(self, event: Event) -> bool:
        """存储事件"""
        if not self._initialized:
            logger.error("存储管理器未初始化")
            return False

        try:
            collection = self.db.collection(Collections.EVENTS)
            collection.insert(event.to_dict())
            logger.debug(f"事件已存储: {event.event_id}")
            return True
        except Exception as e:
            logger.error(f"存储事件失败: {e}")
            return False

    async def get_events(
        self,
        event_types: list[str] | None = None,
        platform: str | None = None,
        conversation_id: str | None = None,
        user_id: str | None = None,
        start_time: int | None = None,
        end_time: int | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """查询事件"""
        if not self._initialized:
            return []

        try:
            # 构建查询条件
            filters = []
            bind_vars = {"limit": limit}

            if event_types:
                if len(event_types) == 1:
                    filters.append("e.event_type == @event_type")
                    bind_vars["event_type"] = event_types[0]
                else:
                    filters.append("e.event_type IN @event_types")
                    bind_vars["event_types"] = event_types

            if platform:
                filters.append("e.platform == @platform")
                bind_vars["platform"] = platform

            if conversation_id:
                filters.append("e.conversation_id == @conversation_id")
                bind_vars["conversation_id"] = conversation_id

            if user_id:
                filters.append("e.user_id == @user_id")
                bind_vars["user_id"] = user_id

            if start_time:
                filters.append("e.timestamp >= @start_time")
                bind_vars["start_time"] = start_time

            if end_time:
                filters.append("e.timestamp <= @end_time")
                bind_vars["end_time"] = end_time

            # 构建查询语句
            filter_clause = " AND ".join(filters) if filters else "true"
            aql = f"""
                FOR e IN {Collections.EVENTS}
                    FILTER {filter_clause}
                    SORT e.timestamp DESC
                    LIMIT @limit
                    RETURN e
            """

            cursor = self.db.aql.execute(aql, bind_vars=bind_vars)
            return list(cursor)

        except Exception as e:
            logger.error(f"查询事件失败: {e}")
            return []

    async def get_recent_conversation_events(self, conversation_id: str, limit: int = 50) -> list[dict[str, Any]]:
        """获取会话最近的事件"""
        return await self.get_events(
            event_types=["message.group.normal", "message.private.normal"], conversation_id=conversation_id, limit=limit
        )

    # ==================== Action 操作 ====================

    async def store_action(self, action: ActionRecord) -> bool:
        """存储动作记录"""
        if not self._initialized:
            logger.error("存储管理器未初始化")
            return False

        try:
            collection = self.db.collection(Collections.ACTIONS)
            collection.insert(action.to_dict())
            logger.debug(f"动作记录已存储: {action.action_id}")
            return True
        except Exception as e:
            logger.error(f"存储动作记录失败: {e}")
            return False

    async def update_action_status(
        self,
        action_id: str,
        status: str,
        response_data: dict[str, Any] | None = None,
        error_message: str | None = None,
    ) -> bool:
        """更新动作执行状态"""
        if not self._initialized:
            return False

        try:
            collection = self.db.collection(Collections.ACTIONS)
            update_data = {"status": status}

            if response_data:
                update_data["response_data"] = response_data
            if error_message:
                update_data["error_message"] = error_message

            collection.update({"_key": action_id}, update_data)
            logger.debug(f"动作状态已更新: {action_id} -> {status}")
            return True
        except Exception as e:
            logger.error(f"更新动作状态失败: {e}")
            return False

    # ==================== 工具方法 ====================

    async def get_conversation_stats(self, conversation_id: str) -> dict[str, Any]:
        """获取会话统计信息"""
        if not self._initialized:
            return {}

        try:
            aql = f"""
                FOR e IN {Collections.EVENTS}
                    FILTER e.conversation_id == @conversation_id
                        AND e.event_type LIKE "message.%"
                    COLLECT user_id = e.user_id
                    AGGREGATE
                        message_count = COUNT(),
                        last_message = MAX(e.timestamp)
                    RETURN {{
                        user_id,
                        message_count,
                        last_message
                    }}
            """

            cursor = self.db.aql.execute(aql, bind_vars={"conversation_id": conversation_id})
            user_stats = list(cursor)

            return {"conversation_id": conversation_id, "active_users": len(user_stats), "user_stats": user_stats}

        except Exception as e:
            logger.error(f"获取会话统计失败: {e}")
            return {}

    async def cleanup_old_events(self, days_to_keep: int = 30) -> int:
        """清理旧事件"""
        if not self._initialized:
            return 0

        try:
            cutoff_time = int((time.time() - days_to_keep * 24 * 3600) * 1000)

            aql = f"""
                FOR e IN {Collections.EVENTS}
                    FILTER e.timestamp < @cutoff_time
                    REMOVE e IN {Collections.EVENTS}
                    RETURN OLD
            """

            cursor = self.db.aql.execute(aql, bind_vars={"cutoff_time": cutoff_time})
            deleted_count = len(list(cursor))

            logger.info(f"清理了 {deleted_count} 个旧事件")
            return deleted_count

        except Exception as e:
            logger.error(f"清理旧事件失败: {e}")
            return 0

    async def close(self) -> None:
        """关闭数据库连接"""
        if self.client:
            self.client.close()
            self._initialized = False
            logger.info("存储管理器已关闭")

    async def get_formatted_chat_history(self, conversation_id: str, limit: int = 50) -> str:
        """获取格式化的聊天历史"""
        events = await self.get_events(
            event_types=["message.group.normal", "message.private.normal"], conversation_id=conversation_id, limit=limit
        )

        # 转换为外部工具期望的格式
        formatted_messages = []
        for event in events:
            formatted_msg = {
                "timestamp": event["timestamp"],
                "platform": event["platform"],
                "group_id": event.get("conversation_id"),
                "group_name": event.get("conversation_info", {}).get("name"),
                "sender_id": event.get("user_id"),
                "sender_nickname": event.get("user_info", {}).get("user_nickname"),
                "message_content": event.get("content", []),
                "message_id": event["event_id"],
                "post_type": "message",
                "sub_type": event["event_type"].split(".")[-1],  # normal, notice, etc.
            }
            formatted_messages.append(formatted_msg)

        return format_chat_history_for_prompt(formatted_messages)

    async def store_event_with_text_extraction(self, event: Event) -> bool:
        """存储事件并提取文本内容用于搜索"""
        if await self.store_event(event):
            # 可以在这里添加全文搜索索引等功能
            text_content = event.get_text_content()
            if text_content:
                logger.debug(f"提取的文本内容: {text_content[:50]}...")
            return True
        return False
