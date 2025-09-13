# 文件路径: src/services/action/components/pending_action_manager.py

import asyncio
from typing import Any

from src.common.custom_logging.logging_config import get_logger
from src.domain.models import ActionResult

logger = get_logger(__name__)

ACTION_RESPONSE_TIMEOUT_SECONDS = 30


class PendingActionManager:
    """一个纯粹的、无状态的异步动作承诺管理器.

    它只负责匹配请求和响应，并处理超时。
    """

    def __init__(self) -> None:
        # _pending_actions 现在只存储 Future，不再需要其他上下文信息
        self._pending_actions: dict[str, asyncio.Future[ActionResult]] = {}
        logger.info(f"{self.__class__.__name__} instance created (Refactored).")

    async def add_and_wait_for_action(
        self,
        action_id: str,
        original_action_description: str,
    ) -> ActionResult:
        """注册一个待处理的动作并异步等待其响应.

        Args:
            action_id: 动作的唯一ID。
            original_action_description: 用于日志和错误信息的动作描述。

        Returns:
            一个 ActionResult 领域对象，包含了动作的执行结果。
        """
        response_future: asyncio.Future[ActionResult] = asyncio.Future()
        self._pending_actions[action_id] = response_future
        try:
            return await asyncio.wait_for(response_future, timeout=ACTION_RESPONSE_TIMEOUT_SECONDS)
        except TimeoutError:
            # 超时后，从字典中移除Future并返回一个表示超时的ActionResult
            self._pending_actions.pop(action_id, None)
            logger.warning(f"动作 '{action_id}' ({original_action_description}) 响应超时。")
            return ActionResult(
                action_id=action_id,
                is_success=False,
                error_message=f"动作 '{original_action_description}' 响应超时。",
            )
        finally:
            # 确保无论成功、失败还是超时，最终都会被清理
            self._pending_actions.pop(action_id, None)

    async def handle_response(self, response_event_data: dict[str, Any]) -> None:
        """处理收到的动作响应事件，解析它并兑现相应的 Future."""
        original_action_id = self._get_original_id_from_response(response_event_data)
        if not original_action_id:
            return

        pending_future = self._pending_actions.get(original_action_id)
        if not pending_future or pending_future.done():
            logger.warning(f"收到未知的或已处理/超时的 action_response，ID: {original_action_id}。")
            return

        logger.info(f"已匹配到等待中的动作 '{original_action_id}'。")

        successful, _, error_msg, details = self._parse_response_content(response_event_data)

        # "铸造" ActionResult 领域模型
        action_result = ActionResult(
            action_id=original_action_id,
            is_success=successful,
            payload=details,
            error_message=None if successful else error_msg,
        )

        pending_future.set_result(action_result)

    def _get_original_id_from_response(self, data: dict[str, Any]) -> str | None:
        """辅助方法：从响应事件中解析出原始动作ID."""
        content = data.get("content", [])
        if content and isinstance(content, list) and len(content) > 0:
            first_seg = content[0]
            if isinstance(first_seg, dict) and "data" in first_seg:
                return first_seg.get("data", {}).get("original_event_id")
        return None

    def _parse_response_content(self, data: dict[str, Any]) -> tuple[bool, str, str, dict | None]:
        """辅助方法：解析响应事件的内容段."""
        content = data.get("content", [])
        if not content:
            return False, "unknown", "响应内容为空", None
        segment = content[0]
        seg_type = segment.get("type", "")
        if isinstance(segment, dict) and seg_type.startswith("action_response."):
            response_data = segment.get("data", {})
            status = seg_type.split(".")[-1]
            details = response_data.get("data")
            if status == "success":
                return True, "success", "", details
            else:
                return False, status, response_data.get("message", "适配器报告未知错误"), details
        return False, "unknown_format", "响应格式不正确", None
