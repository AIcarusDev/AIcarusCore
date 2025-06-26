# AIcarusCore/src/database/services/action_log_storage_service.py
from typing import Any

# 哼，既然是 arangoasync，那就要用它的专属异常！
from arangoasync.exceptions import DocumentInsertError, DocumentUpdateError

from src.common.custom_logging.logger_manager import get_logger
from src.database.core.connection_manager import (
    ArangoDBConnectionManager,
    CoreDBCollections,
    StandardCollection,
)

logger = get_logger("AIcarusCore.DB.ActionLogStorageService")


class ActionLogStorageService:
    """
    服务类，用于处理 ActionLog 集合的读写操作。
    它记录了 Core 发出的动作尝试及其最终的响应状态。
    这次是专门为你那个 arangoasync 库定制的，再错我就……我就咬你！
    """

    def __init__(self, conn_manager: ArangoDBConnectionManager) -> None:
        """
        初始化 ActionLogStorageService。

        Args:
            conn_manager: ArangoDBConnectionManager 的实例。
        """
        self.conn_manager = conn_manager
        self.collection_name = CoreDBCollections.ACTION_LOGS
        logger.info(f"ActionLogStorageService 初始化完毕，将操作集合 '{self.collection_name}'。")

    async def _get_collection(self) -> StandardCollection:
        """获取 ActionLog 集合的实例。"""
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
        """
        保存一个初始的动作尝试记录到 ActionLog 集合。
        我保留了这个优化，因为 try/except 的插入方式对哪个库都适用，哼！
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
            logger.info(f"动作尝试 '{action_id}' ({action_type}) 已记录到 ActionLog，状态：executing。")
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
        """
        用最终的响应状态更新 ActionLog 中的记录。
        这次用回了你那种“笨拙”但有效的方式，专门伺候 arangoasync 这个老古董。

        Returns:
            如果更新成功则返回 True，否则返回 False。
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
                    f"尝试更新 ActionLog 中动作 '{action_id}' 未生效，可能记录不存在。Update result: {result}"
                )
                return False
        except DocumentUpdateError as e:
            # 专门捕捉更新失败的异常，这样就知道是插错洞了。
            logger.error(f"严重错误：尝试更新一个不存在的 ActionLog 记录 '{action_id}'。 ArangoError: {e}")
            return False
        except Exception as e:
            logger.error(f"更新 ActionLog 中动作 '{action_id}' 时发生未知错误: {e}", exc_info=True)
            return False

    async def get_action_log(self, action_id: str) -> dict[str, Any] | None:
        """
        根据 action_id 获取单个动作日志记录。
        """
        collection = await self._get_collection()
        try:
            doc = await collection.get(action_id)
            return doc
        except Exception as e:
            logger.error(f"获取 ActionLog 记录 '{action_id}' 失败: {e}", exc_info=True)
            return None
