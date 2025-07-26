# AIcarusCore/src/database/services/action_log_storage_service.py (欲望补完版 V1.1)
from typing import Any

from arangoasync.exceptions import DocumentInsertError, DocumentUpdateError
from src.common.custom_logging.logging_config import get_logger
from src.database import (
    ArangoDBConnectionManager,
    CoreDBCollections,
    StandardCollection,
)

logger = get_logger(__name__)


class ActionLogStorageService:
    """服务类，负责处理动作日志的存储和管理."""

    def __init__(self, conn_manager: ArangoDBConnectionManager) -> None:
        self.conn_manager = conn_manager
        self.collection_name = CoreDBCollections.ACTION_LOGS
        logger.info(f"ActionLogStorageService 初始化完毕，将操作集合 '{self.collection_name}'。")

    async def _get_collection(self) -> StandardCollection:
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
        """保存一个动作尝试到 ActionLog 中."""
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
        """更新动作日志的状态和响应信息."""
        collection = await self._get_collection()
        doc_fields_to_update = {
            "status": status,
            "response_timestamp": response_timestamp,
            "response_time_ms": response_time_ms,
            "error_info": error_info,
            "result_details": result_details,
        }
        final_doc_to_update = {k: v for k, v in doc_fields_to_update.items() if v is not None}
        if not final_doc_to_update:
            return True
        document_for_update_api = {"_key": action_id, **final_doc_to_update}
        try:
            result = await collection.update(document_for_update_api)
            if result and result.get("_id"):
                logger.info(f"ActionLog 中动作 '{action_id}' 的状态已更新为 '{status}'。")
                return True
            else:
                logger.warning(f"尝试更新 ActionLog 中动作 '{action_id}' 未生效，可能记录不存在。")
                return False
        except DocumentUpdateError as e:
            logger.error(
                f"严重错误：尝试更新一个不存在的 ActionLog 记录 '{action_id}'。 ArangoError: {e}"
            )
            return False
        except Exception as e:
            logger.error(f"更新 ActionLog 中动作 '{action_id}' 时发生未知错误: {e}", exc_info=True)
            return False

    async def get_action_log(self, action_id: str) -> dict[str, Any] | None:
        """根据动作ID获取对应的动作日志记录."""
        collection = await self._get_collection()
        try:
            return await collection.get(action_id)
        except Exception as e:
            logger.error(f"获取 ActionLog 记录 '{action_id}' 失败: {e}", exc_info=True)
            return None

    # =======================【 这 里 就 是 新 增 的 欲 望！】=======================
    async def get_action_log_by_platform_message_id(self, message_id: str) -> dict[str, Any] | None:
        """根据平台返回的消息ID，查找对应的、成功的 send_message 动作日志.

        这正是 DefaultMessageProcessor 识别“回声”所需要的关键方法!
        """
        if not message_id:
            return None
        try:
            # 这个查询会深入到 result_details 内部，去匹配那个 sent_message_id
            query = """
                FOR doc IN @@collection
                    FILTER doc.status == 'success'
                    AND doc.action_type LIKE '%.send_message'
                    AND doc.result_details.sent_message_id == @message_id
                    SORT doc.timestamp DESC
                    LIMIT 1
                    RETURN doc
            """
            bind_vars = {"@collection": self.collection_name, "message_id": message_id}
            results = await self.conn_manager.execute_query(query, bind_vars)
            if results:
                logger.debug(
                    f"通过平台消息ID '{message_id}' 成功匹配到动作日志: {results[0]['_key']}"
                )
                return results[0]
            return None
        except Exception as e:
            logger.error(f"通过平台消息ID '{message_id}' 查找动作日志失败: {e}", exc_info=True)
            return None

    async def get_recent_action_logs(self, limit: int = 10) -> list[dict[str, Any]]:
        """获取最近的动作日志，按时间降序排列."""
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
