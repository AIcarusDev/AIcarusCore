# AIcarusCore/src/database/services/action_log_storage_service.py
from typing import Any

# 哼，既然是 arangoasync，那就要用它的专属异常！
from arangoasync.exceptions import DocumentInsertError, DocumentUpdateError
from src.common.custom_logging.logging_config import get_logger
from src.database import (
    ArangoDBConnectionManager,
    CoreDBCollections,
    StandardCollection,
)

logger = get_logger(__name__)


class ActionLogStorageService:
    """服务类，负责处理动作日志的存储和管理.

    这个服务类提供了将动作日志保存到数据库的功能，确保数据的完整性和一致性.

    Attributes:
        conn_manager (ArangoDBConnectionManager): 数据库连接管理器实例，
            用于获取和管理数据库集合.
        collection_name (str): 动作日志集合的名称，默认为 CoreDBCollections.ACTION_LOGS.
    """

    def __init__(self, conn_manager: ArangoDBConnectionManager) -> None:
        """初始化 ActionLogStorageService.

        Args:
            conn_manager (ArangoDBConnectionManager): 数据库连接管理器实例，
        """
        self.conn_manager = conn_manager
        self.collection_name = CoreDBCollections.ACTION_LOGS
        logger.info(f"ActionLogStorageService 初始化完毕，将操作集合 '{self.collection_name}'。")

    async def _get_collection(self) -> StandardCollection:
        """获取 ActionLog 集合的实例."""
        return await self.conn_manager.get_collection(self.collection_name)

    async def save_action_attempt(
        self,
        action_id: str,
        action_type: str,
        timestamp: int,
        platform: str,
        bot_id: str,
        conversation_id: str,
        content: list[dict[str, Any]],
        original_event_id: str | None = None,
        target_user_id: str | None = None,
    ) -> bool:
        """保存一个动作尝试到 ActionLog 集合中.

        Args:
            action_id (str): 动作的唯一标识符.
            action_type (str): 动作的类型，例如 "send_message", "execute_command" 等.
            timestamp (int): 动作尝试的时间戳，单位为毫秒.
            platform (str): 动作所属的平台，例如 "telegram", "discord" 等.
            bot_id (str): 处理此动作的机器人的唯一标识符.
            conversation_id (str): 关联的会话 ID.
            content (list[dict[str, Any]]): 动作的内容，通常是一个字典列表，
                包含消息或命令的详细信息.
            original_event_id (str | None): 如果此动作是响应某个事件的，
                包含原始事件的 ID，否则为 None.
            target_user_id (str | None): 如果此动作是针对特定用户的，包含目标用户的 ID，否则为 None.

        Returns:
            bool: 如果保存成功返回 True，否则返回 False.
        """
        collection = await self._get_collection()
        action_log_doc = {
            "_key": action_id,
            "action_id": action_id,
            "action_type": action_type,
            "timestamp": timestamp,
            "platform": platform,
            "bot_id": bot_id,
            "conversation_id": conversation_id,
            "target_user_id": target_user_id,
            "content": content,
            "status": "executing",
            "original_event_id": original_event_id,
            "response_timestamp": None,
            "response_time_ms": None,
            "error_info": None,
            "result_details": None,
        }
        try:
            # 这个姿势依然是最高效的，直接插入，让数据库告诉我们是不是已经有了。
            await collection.insert(action_log_doc, overwrite=False)
            logger.info(
                f"动作尝试 '{action_id}' ({action_type}) 已记录到 ActionLog，状态：executing。"
            )
            return True
        except DocumentInsertError:
            logger.info(f"动作尝试 '{action_id}' 的记录已存在，无需重复插入。")
            return True
        except Exception as e:
            logger.error(f"保存动作尝试 '{action_id}' 到 ActionLog 失败: {e}", exc_info=True)
            return False

    async def update_action_log_with_response(
        self,
        action_id: str,
        status: str,
        response_timestamp: int,
        response_time_ms: int | None = None,
        error_info: str | None = None,
        result_details: dict[str, Any] | None = None,
    ) -> bool:
        """更新 ActionLog 中的动作状态和响应信息.

        Args:
            action_id (str): 动作的唯一标识符.
            status (str): 动作的当前状态，例如 "executing", "completed", "failed".
            response_timestamp (int): 响应的时间戳，单位为毫秒.
            response_time_ms (int | None): 响应时间，单位为毫秒，如果没有则为 None.
            error_info (str | None): 如果有错误发生，包含错误信息，否则为 None.
            result_details (dict[str, Any] | None): 如果有结果详情，包含相关信息，否则为 None.

        Returns:
            如果更新成功则返回 True，否则返回 False.
        """
        collection = await self._get_collection()
        doc_fields_to_update = {
            "status": status,
            "response_timestamp": response_timestamp,
            "response_time_ms": response_time_ms,
            "error_info": error_info,
            "result_details": result_details,
        }

        # 哼，看好了！因为 arangoasync 不支持 keep_null=False，所以只能用回你那个笨办法了。
        # 我们手动把所有值为 None 的肉棒……不，是字段，都过滤掉，免得它不高兴。
        final_doc_to_update = {k: v for k, v in doc_fields_to_update.items() if v is not None}

        if not final_doc_to_update:
            logger.info(f"没有为 action_id '{action_id}' 提供有效的更新字段，跳过更新。")
            return True

        # 构建传递给 collection.update 的文档参数，这次要温柔一点，不乱传参数了。
        document_for_update_api = {"_key": action_id, **final_doc_to_update}

        try:
            # 看清楚了，笨蛋主人！这里没有 merge，也没有 keep_null，就是最纯粹的 update！
            result = await collection.update(document_for_update_api)

            if result and result.get("_id"):
                logger.info(f"ActionLog 中动作 '{action_id}' 的状态已更新为 '{status}'。")
                return True
            else:
                logger.warning(
                    f"尝试更新 ActionLog 中动作 '{action_id}' 未生效，可能记录不存在。"
                    f"Update result: {result}"
                )
                return False
        except DocumentUpdateError as e:
            # 专门捕捉更新失败的异常，这样就知道是插错洞了。
            logger.error(
                f"严重错误：尝试更新一个不存在的 ActionLog 记录 '{action_id}'。 ArangoError: {e}"
            )
            return False
        except Exception as e:
            logger.error(f"更新 ActionLog 中动作 '{action_id}' 时发生未知错误: {e}", exc_info=True)
            return False

    async def get_action_log(self, action_id: str) -> dict[str, Any] | None:
        """根据 action_id 获取单个动作日志记录."""
        collection = await self._get_collection()
        try:
            doc = await collection.get(action_id)
            return doc
        except Exception as e:
            logger.error(f"获取 ActionLog 记录 '{action_id}' 失败: {e}", exc_info=True)
            return None

    async def get_recent_action_logs(self, limit: int = 10) -> list[dict[str, Any]]:
        """获取最近的动作日志记录.

        Args:
            limit (int): 要获取的记录数量，默认为 10。
        Returns:
            list[dict[str, Any]]: 包含最近动作日志记录的列表，每个记录是一个字典，
                包含时间戳、动作类型、状态和错误信息等字段。
        """
        if limit <= 0:
            return []
        try:
            query = """
                FOR doc IN @@collection
                    SORT doc.timestamp DESC
                    LIMIT @limit
                    RETURN { timestamp: doc.timestamp, action_type: doc.action_type, status: doc.status, error_info: doc.error_info }
            """  # noqa: E501
            bind_vars = {"@collection": self.collection_name, "limit": limit}
            results = await self.conn_manager.execute_query(query, bind_vars)
            return results if results is not None else []
        except Exception as e:
            logger.error(f"获取最近动作日志失败: {e}", exc_info=True)
            return []
