# src/action/components/pending_action_manager.py
import asyncio
import json
import time
from typing import Any

from src.common.custom_logging.logging_config import get_logger
from src.config import config
from src.database.services.action_log_storage_service import ActionLogStorageService
from src.database.services.conversation_storage_service import ConversationStorageService
from src.database.services.event_storage_service import EventStorageService
from src.database.services.thought_storage_service import ThoughtStorageService

logger = get_logger(__name__)

ACTION_RESPONSE_TIMEOUT_SECONDS = 30


class PendingActionManager:
    """
    管理所有待处理的平台动作。
    负责跟踪已发送但尚未收到响应的动作，并处理其成功响应、失败响应或超时。
    """

    def __init__(
        self,
        action_log_service: ActionLogStorageService,
        thought_storage_service: ThoughtStorageService,
        event_storage_service: EventStorageService,
        conversation_service: ConversationStorageService,
    ) -> None:
        self._pending_actions: dict[str, tuple[asyncio.Future, str | None, str, dict[str, Any]]] = {}
        self.action_log_service = action_log_service
        self.thought_storage_service = thought_storage_service
        self.event_storage_service = event_storage_service
        self.conversation_service = conversation_service
        logger.info(f"{self.__class__.__name__} instance created.")

    async def add_and_wait_for_action(
        self,
        action_id: str,
        thought_doc_key: str | None,
        original_action_description: str,
        action_to_send: dict[str, Any],
    ) -> tuple[bool, Any]:
        """
        添加一个新的待处理动作，并等待其完成（或超时）。

        Returns:
            一个元组 (action_successful, result_payload)。
        """
        response_future = asyncio.Future()
        self._pending_actions[action_id] = (
            response_future,
            thought_doc_key,
            original_action_description,
            action_to_send,
        )
        try:
            return await asyncio.wait_for(response_future, timeout=ACTION_RESPONSE_TIMEOUT_SECONDS)
        except TimeoutError:
            if action_id in self._pending_actions:
                await self._handle_action_timeout(action_id)
            return False, {"error": f"动作 '{original_action_description}' 响应超时。"}
        finally:
            self._pending_actions.pop(action_id, None)

    async def _handle_action_timeout(self, action_id: str) -> None:
        if action_id not in self._pending_actions:
            return

        logger.warning(f"动作 '{action_id}' 超时未收到响应！")
        pending_event, thought_doc_key, description, _ = self._pending_actions.pop(action_id)
        if not pending_event.done():
            pending_event.set_exception(TimeoutError())

        timeout_timestamp = int(time.time() * 1000)
        await self.action_log_service.update_action_log_with_response(
            action_id=action_id,
            status="timeout",
            response_timestamp=timeout_timestamp,
            error_info="Action response timed out",
        )

        if thought_doc_key:
            timeout_message = f"你尝试执行动作 '{description}' 时，等待响应超时了。"
            update_payload = {
                "status": "TIMEOUT_FAILURE",
                "final_result_for_shimo": timeout_message,
                "error_message": "Action response timed out.",
            }
            await self.thought_storage_service.update_action_status_in_thought_document(
                thought_doc_key, action_id, update_payload
            )

    async def handle_response(self, response_event_data: dict[str, Any]) -> None:
        """处理来自适配器的动作响应事件。"""
        original_action_id = self._get_original_id_from_response(response_event_data)
        if not original_action_id:
            return

        if original_action_id not in self._pending_actions:
            logger.warning(f"收到未知的或已处理/超时的 action_response，ID: {original_action_id}。")
            return

        pending_future, thought_doc_key, description, sent_dict = self._pending_actions.pop(original_action_id)
        logger.info(f"已匹配到等待中的动作 '{original_action_id}' ({description})。")

        # 解析响应
        successful, status, error_msg, details = self._parse_response_content(response_event_data)
        original_action_type = sent_dict.get("event_type")
        if successful and original_action_type == "action.bot.get_profile" and details:
            logger.info(f"收到来自适配器 '{sent_dict.get('platform')}' 的档案同步报告，开始处理...")
            # 把处理报告这个脏活累活，单独丢给一个新方法去做！
            await self._process_bot_profile_report(details)
        final_result = self._create_final_result_message(description, successful, error_msg, details)
        response_timestamp = int(time.time() * 1000)
        response_time_ms = response_timestamp - sent_dict.get("timestamp", response_timestamp)

        # 更新日志
        await self.action_log_service.update_action_log_with_response(
            action_id=original_action_id,
            status=status,
            response_timestamp=response_timestamp,
            response_time_ms=response_time_ms,
            error_info=None if successful else error_msg,
            result_details=details,
        )

        # 设置Future结果
        if not pending_future.done():
            result_payload = details if successful else {"error": error_msg}
            pending_future.set_result((successful, result_payload))

        # 更新思考文档
        if thought_doc_key:
            update_payload = {
                "status": "COMPLETED_SUCCESS" if successful else "COMPLETED_FAILURE",
                "final_result_for_shimo": final_result,
                "error_message": "" if successful else error_msg,
                "response_received_at": response_timestamp,
            }
            await self.thought_storage_service.update_action_status_in_thought_document(
                thought_doc_key, original_action_id, update_payload
            )

        # 存为事件
        if successful:
            await self._save_successful_action_as_event(original_action_id, sent_dict, response_event_data)

    async def _process_bot_profile_report(self, report_data: dict[str, Any]) -> None:
        """
        处理从 Adapter 发来的“全身检查报告”。
        新版：使用 upsert 逻辑，确保即使会话档案不存在也能正确创建和更新。
        """
        if not isinstance(report_data, dict):
            logger.warning("收到的机器人档案报告不是一个有效的字典。")
            return

        bot_id = report_data.get("user_id")
        platform = report_data.get("platform")  # 我们需要平台信息来创建新文档
        groups_info = report_data.get("groups")

        if not bot_id or not groups_info or not isinstance(groups_info, dict):
            logger.warning(f"机器人档案报告缺少 bot_id、platform 或 groups 信息。报告内容: {report_data}")
            return

        logger.info(f"正在处理机器人(ID: {bot_id})的 {len(groups_info)} 个群聊档案更新...")

        update_tasks = []
        for group_id, group_profile in groups_info.items():
            if not isinstance(group_profile, dict):
                continue

            # 构造机器人在这个群里的档案信息
            bot_profile_in_conv = {
                "user_id": bot_id,
                "nickname": report_data.get("nickname"),
                "card": group_profile.get("card"),
                "title": group_profile.get("title"),
                "role": group_profile.get("role"),
                "updated_at": int(time.time() * 1000),
            }

            # 构造一个完整的、新的会话档案字典，以备不时之需（万一它不存在呢）
            # 我们用这个字典来执行 upsert 操作
            conversation_doc_to_upsert = {
                "conversation_id": group_id,
                "platform": platform,
                "bot_id": bot_id,
                "name": group_profile.get("group_name"),
                "type": "group",
                # 把我们的体检报告里的信息，填到这个新档案的 bot_profile_in_this_conversation 字段里
                "bot_profile_in_this_conversation": bot_profile_in_conv,
            }

            # 最后，调用那个万能的 upsert 方法！
            # 它会自己判断是该插入还是更新，完美！
            task = self.conversation_service.upsert_conversation_document(conversation_doc_to_upsert)
            update_tasks.append(task)

        if update_tasks:
            results = await asyncio.gather(*update_tasks, return_exceptions=True)
            success_count = sum(bool(r is not None and not isinstance(r, Exception)) for r in results)
            failure_count = len(results) - success_count
            logger.info(f"机器人档案同步完成。成功 upsert {success_count} 个会话，失败 {failure_count} 个。")
        else:
            logger.info("机器人档案报告中没有需要更新的群聊信息。")

    def _get_original_id_from_response(self, data: dict[str, Any]) -> str | None:
        content = data.get("content", [])
        if content and isinstance(content, list) and len(content) > 0:
            first_seg = content[0]
            if isinstance(first_seg, dict) and "data" in first_seg:
                return first_seg.get("data", {}).get("original_event_id")
        logger.error(f"无法从响应事件 {data.get('event_id')} 中解析出 original_event_id。")
        return None

    def _parse_response_content(self, data: dict[str, Any]) -> tuple[bool, str, str, dict | None]:
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
                error_msg = response_data.get("message", "适配器报告未知错误")
                return False, status, error_msg, details
        return False, "unknown_format", "响应格式不正确", None

    def _create_final_result_message(self, desc: str, succ: bool, err: str, det: dict | None) -> str:
        if succ:
            msg = f"动作 '{desc}' 已成功执行。"
            if det:
                msg += f" 详情: {json.dumps(det, ensure_ascii=False)}"
            return msg
        return f"动作 '{desc}' 执行失败: {err}"

    async def _save_successful_action_as_event(
        self, action_id: str, sent_dict: dict[str, Any], resp_data: dict[str, Any]
    ) -> None:
        event_to_save = sent_dict.copy()
        event_to_save["event_id"] = action_id
        event_to_save["timestamp"] = int(time.time() * 1000)
        event_to_save["status"] = "read"

        message_id = await self._get_sent_message_id_safe(resp_data)
        metadata = [{"type": "message_metadata", "data": {"message_id": message_id}}]
        event_to_save["content"] = metadata + event_to_save.get("content", [])

        event_to_save["user_info"] = {
            "platform": resp_data.get("platform", "unknown_platform"),
            "user_id": resp_data.get("bot_id", "unknown_user_id"),
            "user_nickname": config.persona.bot_name,
        }
        await self.event_storage_service.save_event_document(event_to_save)
        logger.info(f"成功的平台动作 '{action_id}' 已作为事件存入 events 表。")

    async def _get_sent_message_id_safe(self, event_data: dict[str, Any]) -> str:
        default_id = "unknow_message_id"
        if not isinstance(event_data, dict):
            logger.error(f"事件数据不是一个字典，无法从中安全地提取 sent_message_id。事件数据类型: {type(event_data)}")
            return default_id
        content_list = event_data.get("content")
        if isinstance(content_list, list) and len(content_list) > 0:
            first_item = content_list[0]
            if isinstance(first_item, dict):
                response_data = first_item.get("data", {})
                if isinstance(response_data, dict):
                    details_data = response_data.get("data", {})
                    if isinstance(details_data, dict):
                        sent_message_id = details_data.get("sent_message_id")
                        if sent_message_id is not None:
                            return str(sent_message_id)
        return default_id
