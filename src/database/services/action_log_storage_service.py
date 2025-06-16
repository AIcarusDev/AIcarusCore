# AIcarusCore/src/database/services/action_log_storage_service.py
import asyncio
from typing import Any

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
    """

    def __init__(self, conn_manager: ArangoDBConnectionManager) -> None:
        """
        初始化 ActionLogStorageService。

        Args:
            conn_manager: ArangoDBConnectionManager 的实例。
        """
        self.conn_manager = conn_manager
        self.collection_name = CoreDBCollections.ACTION_LOGS
        self.logger = logger
        self.logger.info(f"ActionLogStorageService 初始化完毕，将操作集合 '{self.collection_name}'。")

    async def _get_collection(self) -> StandardCollection:
        """获取 ActionLog 集合的实例。"""
        return await self.conn_manager.get_collection(self.collection_name)

    async def save_action_attempt(
        self,
        action_id: str,  # 通常是 action.* 事件的 event_id
        action_type: str,
        timestamp: int,  # 动作发出的时间戳 (毫秒)
        platform: str,
        bot_id: str,
        conversation_id: str,
        content: list[dict[str, Any]],
        original_event_id: str | None = None,  # 触发此动作的原始事件ID (如用户消息)
        target_user_id: str | None = None,
    ) -> bool:  # 返回类型修改为 bool
        """
        保存一个初始的动作尝试记录到 ActionLog 集合。
        状态默认为 'executing'。

        Args:
            action_id: 动作的唯一标识符 (通常是发送的 action.* 事件的 event_id)。
            action_type: 动作的类型 (例如 "action.message.send")。
            timestamp: 动作发出的时间戳 (毫秒)。
            platform: 目标平台。
            bot_id: 执行动作的机器人ID。
            conversation_id: 目标会话ID。
            content: 动作的内容段列表。
            original_event_id: 可选，触发此动作的原始事件ID。
            target_user_id: 可选，动作的目标用户ID。

        Returns:
            如果成功确保日志条目存在（新建或已存在），则返回 True，否则返回 False。
        """
        collection = await self._get_collection()
        action_log_doc = {
            "_key": action_id,  # 使用 action_id 作为文档的键，确保唯一性并方便查找
            "action_id": action_id,
            "action_type": action_type,
            "timestamp": timestamp,  # 动作发出时间
            "platform": platform,
            "bot_id": bot_id,
            "conversation_id": conversation_id,
            "target_user_id": target_user_id,
            "content": content,
            "status": "executing",  # 初始状态
            "original_event_id": original_event_id,
            "response_timestamp": None,  # 响应时间戳，后续更新
            "response_time_ms": None,  # 响应耗时，后续更新
            "error_info": None,  # 错误信息，后续更新
            "result_details": None,  # 结果详情，后续更新
        }
        try:
            existing_doc = await asyncio.to_thread(collection.get, action_id)
            if existing_doc:
                self.logger.info(
                    f"ActionLog 中已存在 action_id '{action_id}' 的记录。当前状态: {existing_doc.get('status')}"
                )
                return True  # 记录已存在，视为成功确保条目存在

            await asyncio.to_thread(collection.insert, action_log_doc, overwrite=False)
            self.logger.info(f"动作尝试 '{action_id}' ({action_type}) 已记录到 ActionLog，状态：executing。")
            return True
        except Exception as e:
            self.logger.error(f"保存动作尝试 '{action_id}' 到 ActionLog 失败: {e}", exc_info=True)
            return False

    async def update_action_log_with_response(
        self,
        action_id: str,
        status: str,  # "success", "failure", "timeout"
        response_timestamp: int,  # 收到响应或判定超时的时间戳 (毫秒)
        response_time_ms: int | None = None,
        error_info: str | None = None,
        result_details: dict[str, Any] | None = None,
    ) -> bool:
        """
        用最终的响应状态、响应时间、错误信息和结果详情更新 ActionLog 中的记录。

        Args:
            action_id: 要更新的动作的唯一标识符。
            status: 动作的最终状态 ("success", "failure", "timeout")。
            response_timestamp: 收到响应或判定超时的时间戳 (毫秒)。
            response_time_ms: 可选，从发送到收到响应/超时的耗时 (毫秒)。
            error_info: 可选，如果失败，则为错误信息。
            result_details: 可选，动作执行的结果详情。

        Returns:
            如果更新成功则返回 True，否则返回 False。
        """
        collection = await self._get_collection()
        doc_fields_to_update = {  # 只包含要更新的字段
            "status": status,
            "response_timestamp": response_timestamp,
            "response_time_ms": response_time_ms,
            "error_info": error_info,
            "result_details": result_details,
        }
        # 移除值为 None 的字段，这样它们就不会被发送给 update，从而不会用 None 覆盖已有值
        # 这模拟了 keep_null=False 的效果，如果驱动的 update 不直接支持该参数
        final_doc_to_update = {k: v for k, v in doc_fields_to_update.items() if v is not None}

        if not final_doc_to_update:  # 如果所有更新字段都是None，可能不需要更新
            self.logger.info(f"没有为 action_id '{action_id}' 提供有效的更新字段，跳过更新。")
            # 这种情况是否算成功取决于业务逻辑，如果只是没有新信息，可以认为原状态保持，算一种“成功”
            return True

        # 构建传递给 collection.update 的文档参数
        # 第一个参数是包含 _key 和要更新字段的字典
        document_for_update_api = {"_key": action_id, **final_doc_to_update}

        try:
            # python-arango 的 update 默认 merge_objects=True, keep_null=True
            # 我们通过只传递非None字段来间接实现类似 keep_null=False 的效果（对于我们想更新的字段）
            result = await asyncio.to_thread(collection.update, document_for_update_api, merge=True)

            if result and result.get("_id"):
                self.logger.info(
                    f"ActionLog 中动作 '{action_id}' 的状态已更新为 '{status}'。更新字段: {final_doc_to_update}"
                )
                return True
            else:
                self.logger.warning(
                    f"尝试更新 ActionLog 中动作 '{action_id}' 失败，可能记录不存在或更新未生效。Update result: {result}"
                )
                existing_doc = await asyncio.to_thread(collection.get, action_id)
                if not existing_doc:
                    self.logger.error(f"严重错误：尝试更新一个不存在的 ActionLog 记录 '{action_id}'。")
                return False
        except Exception as e:
            self.logger.error(f"更新 ActionLog 中动作 '{action_id}' 失败: {e}", exc_info=True)
            return False

    async def get_action_log(self, action_id: str) -> dict[str, Any] | None:
        """
        根据 action_id 获取单个动作日志记录。

        Args:
            action_id: 动作的唯一标识符。

        Returns:
            动作日志文档字典，如果未找到则返回 None。
        """
        collection = await self._get_collection()
        try:
            doc = await asyncio.to_thread(collection.get, action_id)
            return doc
        except Exception as e:
            self.logger.error(f"获取 ActionLog 记录 '{action_id}' 失败: {e}", exc_info=True)
            return None
