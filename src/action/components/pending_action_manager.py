# 文件: src/action/components/pending_action_manager.py (手滑修复版 V1.1)
import asyncio
import json
import time
from typing import TYPE_CHECKING, Any

from src.common.custom_logging.logging_config import get_logger
from src.database import (
    ActionLogStorageService,
    ConversationStorageService,
    EnrichedConversationInfo,
    ThoughtStorageService,
)
from src.database.services.event_storage_service import EventStorageService

if TYPE_CHECKING:
    from src.action.action_handler import ActionHandler

logger = get_logger(__name__)

ACTION_RESPONSE_TIMEOUT_SECONDS = 30

# 未来我们可以将并发数限制在一个合理的范围，比如50，以避免瞬间冲击数据库
# 但目前先不设置这个限制，等实际运行中再观察是否需要
# DB_UPSERT_CONCURRENCY_LIMIT = 50


class PendingActionManager:
    """管理所有待处理的平台动作 (竞速模式适配版)。
    它现在负责在收到 send_message 的回执时，通过 ActionHandler 向上通知 ChatSession。
    """

    def __init__(
        self,
        action_log_service: ActionLogStorageService,
        thought_storage_service: ThoughtStorageService,
        event_storage_service: EventStorageService,
        conversation_service: ConversationStorageService,
        action_handler_instance: "ActionHandler",
    ) -> None:
        self._pending_actions: dict[
            str, tuple[asyncio.Future, str | None, str, dict[str, Any], str | None]
        ] = {}
        self.action_log_service = action_log_service
        self.thought_storage_service = thought_storage_service
        self.event_storage_service = event_storage_service  # <-- 我明明存的是这个名字...
        self.conversation_service = conversation_service
        self.action_handler = action_handler_instance
        logger.info(f"{self.__class__.__name__} instance created.")

    async def add_and_wait_for_action(
        self,
        action_id: str,
        thought_doc_key: str | None,
        original_action_description: str,
        action_to_send: dict[str, Any],
        motivation: str | None = None,
    ) -> tuple[bool, Any]:
        response_future = asyncio.Future()
        self._pending_actions[action_id] = (
            response_future,
            thought_doc_key,
            original_action_description,
            action_to_send,
            motivation,
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
        pending_future, _, _, _, _ = self._pending_actions.pop(action_id)
        if not pending_future.done():
            pending_future.set_exception(TimeoutError())
        await self.action_log_service.update_action_log_with_response(
            action_id=action_id,
            status="timeout",
            response_timestamp=int(time.time() * 1000),
            error_info="Action response timed out",
        )

    async def handle_response(self, response_event_data: dict[str, Any]) -> None:
        original_action_id = self._get_original_id_from_response(response_event_data)
        if not original_action_id:
            return

        if original_action_id not in self._pending_actions:
            logger.warning(f"收到未知的或已处理/超时的 action_response，ID: {original_action_id}。")
            return

        pending_future, thought_doc_key, description, sent_dict, motivation = (
            self._pending_actions.pop(original_action_id)
        )
        logger.info(f"已匹配到等待中的动作 '{original_action_id}' ({description})。")
        successful, status, error_msg, details = self._parse_response_content(response_event_data)
        original_action_type = sent_dict.get("event_type")

        if not pending_future.done():
            result_payload = details if successful else {"error": error_msg}
            pending_future.set_result((successful, result_payload))

        if successful and original_action_type and original_action_type.endswith(".send_message"):
            # 如果是 send_message 动作，尝试从响应中提取会话信息
            if original_action_type.endswith(".get_list"):
                await self._proactively_create_conversation_docs_from_list(details, sent_dict)
            conversation_info = sent_dict.get("conversation_info")
            if conversation_info and isinstance(conversation_info, dict):
                conv_id = conversation_info.get("conversation_id")
                if conv_id and self.action_handler.chat_session_manager:
                    session = self.action_handler.chat_session_manager.sessions.get(str(conv_id))
                    if session:
                        logger.info(
                            f"检测到 send_message 动作的回声，正在为动作 '{original_action_id}' 调用 session.signal_echo_received()！"
                        )
                        await session.signal_echo_received(original_action_id)

        response_timestamp = int(time.time() * 1000)
        response_time_ms = response_timestamp - sent_dict.get("timestamp", response_timestamp)
        tasks_to_gather = [
            self.action_log_service.update_action_log_with_response(
                action_id=original_action_id,
                status=status,
                response_timestamp=response_timestamp,
                response_time_ms=response_time_ms,
                error_info=None if successful else error_msg,
                result_details=details,
            )
        ]
        if thought_doc_key:
            _final_result_message = self._create_final_result_message(
                description, successful, error_msg, details
            )
            tasks_to_gather.append(
                self.thought_storage_service.save_action_result_to_thought(
                    thought_key=thought_doc_key, result_text=_final_result_message
                )
            )
        if successful:
            tasks_to_gather.append(
                self._save_successful_action_as_event(
                    original_action_id, sent_dict, response_event_data, motivation=motivation
                )
            )
        if tasks_to_gather:
            await asyncio.gather(*tasks_to_gather)

    async def _proactively_create_conversation_docs_from_list(
        self, details: dict | None, sent_dict: dict
    ) -> None:
        """当 get_list 动作成功后，主动为列表中的每个项目创建或更新会话档案。"""
        if not details or not isinstance(details, dict):
            return

        list_type = sent_dict.get("content", [{}])[0].get("data", {}).get("list_type")
        platform_id = sent_dict.get("platform")
        bot_id = sent_dict.get("bot_id")

        if not list_type or not platform_id or not bot_id:
            logger.warning("无法从 get_list 的原始请求中获取足够信息来创建会话档案。")
            return

        items = details.get("friends", []) if list_type == "friend" else details.get("groups", [])
        if not items or not isinstance(items, list):
            return

        logger.info(
            f"收到 get_list({list_type}) 的成功响应，准备为 {len(items)} 个项目主动创建/更新会话档案。"
        )

        conversation_type = "private" if list_type == "friend" else "group"
        # 准备批量更新会话档案的任务
        # 这里我们使用 upsert 方法来确保不存在时创建，存在时更新
        upsert_tasks = []
        for item in items:
            if not isinstance(item, dict):
                continue

            conv_id = item.get("user_id") if list_type == "friend" else item.get("group_id")
            conv_name = item.get("nickname") if list_type == "friend" else item.get("group_name")

            if not conv_id:
                continue

            new_conv_info = EnrichedConversationInfo(
                conversation_id=str(conv_id),
                platform=platform_id,
                bot_id=bot_id,
                type=conversation_type,
                name=conv_name,
            )
            task = self.conversation_service.upsert_conversation_document(
                new_conv_info.to_db_document()
            )
            upsert_tasks.append(task)

        if upsert_tasks:
            await asyncio.gather(*upsert_tasks)
            logger.info(f"已完成对 {len(upsert_tasks)} 个项目的会话档案主动更新。")

    def _get_original_id_from_response(self, data: dict[str, Any]) -> str | None:
        content = data.get("content", [])
        if content and isinstance(content, list) and len(content) > 0:
            first_seg = content[0]
            if isinstance(first_seg, dict) and "data" in first_seg:
                return first_seg.get("data", {}).get("original_event_id")
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
                return False, status, response_data.get("message", "适配器报告未知错误"), details
        return False, "unknown_format", "响应格式不正确", None

    def _create_final_result_message(
        self, desc: str, succ: bool, err: str, det: dict | None
    ) -> str:
        if succ:
            msg = f"动作 '{desc}' 已成功执行。"
            if det:
                msg += f" 详情: {json.dumps(det, ensure_ascii=False)}"
            return msg
        return f"动作 '{desc}' 执行失败: {err}"

    async def _save_successful_action_as_event(
        self,
        action_id: str,
        sent_dict: dict[str, Any],
        resp_data: dict[str, Any],
        motivation: str | None = None,
    ) -> None:
        event_to_save = sent_dict.copy()
        event_to_save["event_id"] = action_id
        event_to_save["timestamp"] = int(time.time() * 1000)
        event_to_save["status"] = "read"
        conv_info = event_to_save.get("conversation_info")
        original_action_type = event_to_save.get("event_type", "")
        if original_action_type.endswith(".send_message"):
            platform = original_action_type.split(".")[1]
            if conv_info and isinstance(conv_info, dict):
                conv_type = conv_info.get("type", "unknown")
                event_to_save["event_type"] = f"message.{platform}.{conv_type}"
        if motivation and isinstance(motivation, str) and motivation.strip():
            event_to_save["motivation"] = motivation
        message_id = await self._get_sent_message_id_safe(resp_data)
        event_to_save["content"] = [
            {"type": "message_metadata", "data": {"message_id": message_id}}
        ] + event_to_save.get("content", [])
        real_user_info = None
        if conv_info and isinstance(conv_info, dict):
            conv_id = conv_info.get("conversation_id")
            if (
                conv_id
                and self.action_handler.chat_session_manager
                and (session := self.action_handler.chat_session_manager.sessions.get(str(conv_id)))
            ):
                bot_profile = await session.get_bot_profile()
                real_user_info = {
                    "platform": session.platform,
                    "user_id": bot_profile.get("user_id"),
                    "user_nickname": bot_profile.get("nickname"),
                    "user_cardname": bot_profile.get("card"),
                    "role": bot_profile.get("role"),
                }
        event_to_save["user_info"] = real_user_info or {
            "platform": resp_data.get("platform", "unknown_platform"),
            "user_id": resp_data.get("bot_id", "unknown_user_id"),
            "user_nickname": "AIcarus (Self)",
        }

        # =======================【 这 里 就 是 修 复 点 ！】=======================
        # 我之前在这里不小心写成了 self.event_storage，真是该打屁股！
        # 正确的名字应该是 self.event_storage_service！
        await self.event_storage_service.save_event_document(event_to_save)
        # ======================================================================

        logger.info(f"成功的平台动作 '{action_id}' 已作为事件存入 events 表。")

    async def _get_sent_message_id_safe(self, event_data: dict[str, Any]) -> str:
        default_id = "unknow_message_id"
        if not isinstance(event_data, dict):
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
