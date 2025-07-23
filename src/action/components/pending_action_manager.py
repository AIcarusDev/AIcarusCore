# src/action/components/pending_action_manager.py
import asyncio
import json
import time
from typing import Any

from src.common.custom_logging.logging_config import get_logger
from src.database import ActionLogStorageService, ConversationStorageService, ThoughtStorageService
from src.database.services.event_storage_service import EventStorageService
from aicarus_protocols import find_seg_by_type

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from src.action.action_handler import ActionHandler

logger = get_logger(__name__)

ACTION_RESPONSE_TIMEOUT_SECONDS = 30


class PendingActionManager:
    """管理所有待处理的平台动作.

    负责跟踪已发送但尚未收到响应的动作，并处理其成功响应、失败响应或超时。
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
        self.event_storage_service = event_storage_service
        self.conversation_service = conversation_service
        self.action_handler = action_handler_instance
        logger.info(f"{self.__class__.__name__} instance created.")

    async def add_and_wait_for_action(
        self,
        action_id: str,
        thought_doc_key: str | None,
        original_action_description: str,
        action_to_send: dict[str, Any],
        motivation: str | None = None
    ) -> tuple[bool, Any]:
        """添加一个新的待处理动作，并等待其完成（或超时）.

        Returns:
            一个元组 (action_successful, result_payload)。
        """
        response_future = asyncio.Future()
        self._pending_actions[action_id] = (
            response_future,
            thought_doc_key,
            original_action_description,
            action_to_send,
            motivation
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
        """处理动作超时的情况.

        如果动作在指定时间内没有响应，将记录超时并设置相应的 Future.
        """
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

    async def handle_response(self, response_event_data: dict[str, Any]) -> None:
        """处理来自适配器的动作响应事件."""
        original_action_id = self._get_original_id_from_response(response_event_data)
        if not original_action_id:
            return

        if original_action_id not in self._pending_actions:
            logger.warning(f"收到未知的或已处理/超时的 action_response，ID: {original_action_id}。")
            return

        pending_future, thought_doc_key, description, sent_dict, motivation = self._pending_actions.pop(
            original_action_id
        )
        logger.info(f"已匹配到等待中的动作 '{original_action_id}' ({description})。")

        # 1. 首先，解析响应的核心内容
        successful, status, error_msg, details = self._parse_response_content(response_event_data)
        original_action_type = sent_dict.get("event_type")

        # 2. 立即唤醒被阻塞的任务 (通常是 MessageBuilder)
        #    让 MessageBuilder 可以继续去发送下一条消息，或者结束自己的工作。
        if not pending_future.done():
            result_payload = details if successful else {"error": error_msg}
            pending_future.set_result((successful, result_payload))
            logger.debug(f"动作 '{original_action_id}' 的 Future 已被设置，阻塞的任务（如 MessageBuilder）已被唤醒。")

        # 【DEBUG注入点 A】
        logger.info(f"【DEBUG-PAM】动作 '{original_action_id}' 匹配成功，准备处理其特殊含义。")

        # 检查是否是 send_message 动作
        if successful and original_action_type and original_action_type.endswith(".send_message"):
            conversation_info = sent_dict.get("conversation_info")
            
            # 【DEBUG注入点 B】
            logger.info(f"【DEBUG-PAM】检测到 send_message 动作，conversation_info: {conversation_info}")

            if conversation_info and isinstance(conversation_info, dict):
                conv_id = conversation_info.get("conversation_id")
                
                if conv_id and self.action_handler.chat_session_manager:
                    if session := self.action_handler.chat_session_manager.sessions.get(str(conv_id)):
                        logger.critical(f"【DEBUG-PAM】找到 session！即将为动作 '{original_action_id}' 调用 session.signal_echo_received()！")
                        await session.signal_echo_received(original_action_id)
                    else:
                        logger.warning(f"【DEBUG-PAM】有 conv_id 但找不到对应的 session！无法发送信号。")
                else:
                    logger.warning(f"【DEBUG-PAM】conv_id 为空或 manager 不存在，无法发送信号。")
        
        # 4. 最后，在后台完成所有收尾工作（写日志、更新思考等），这些不应该阻塞 MessageBuilder
        response_timestamp = int(time.time() * 1000)
        response_time_ms = response_timestamp - sent_dict.get("timestamp", response_timestamp)

        # 异步执行所有数据库写入操作
        tasks_to_gather = []

        # 任务A: 更新动作日志
        tasks_to_gather.append(
            self.action_log_service.update_action_log_with_response(
                action_id=original_action_id,
                status=status,
                response_timestamp=response_timestamp,
                response_time_ms=response_time_ms,
                error_info=None if successful else error_msg,
                result_details=details,
            )
        )

        # 任务B: 如果有 thought_doc_key，保存结果到思考文档
        if thought_doc_key:
            _final_result_message = self._create_final_result_message(
                description, successful, error_msg, details
            )
            tasks_to_gather.append(
                self.thought_storage_service.save_action_result_to_thought(
                    thought_key=thought_doc_key, result_text=_final_result_message
                )
            )

        # 任务C: 处理安检报告
        if successful and original_action_type == "action.qq.get_bot_profile" and details:
            tasks_to_gather.append(self._process_bot_profile_report(details))

        # 任务D: 存为事件
        if successful:
            tasks_to_gather.append(
                self._save_successful_action_as_event(
                    original_action_id, sent_dict, response_event_data,
                    motivation=motivation
                )
            )

        # 并发执行所有收尾工作
        if tasks_to_gather:
            await asyncio.gather(*tasks_to_gather)

    async def _process_bot_profile_report(self, report_data: dict[str, Any]) -> None:
        """处理从 Adapter 发来的“全身检查报告”.

        新版：使用 upsert 逻辑，确保即使会话档案不存在也能正确创建和更新。
        """
        if not isinstance(report_data, dict):
            logger.warning("收到的祂的档案报告不是一个有效的字典。")
            return

        bot_id = report_data.get("user_id")
        platform = report_data.get("platform")  # 我们需要平台信息来创建新文档
        groups_info = report_data.get("groups")

        if not bot_id or not groups_info or not isinstance(groups_info, dict):
            logger.warning(
                f"祂的档案报告缺少 bot_id、platform 或 groups 信息。报告内容: {report_data}"
            )
            return

        logger.info(f"正在处理祂(ID: {bot_id})的 {len(groups_info)} 个群聊档案更新...")

        tasks_with_context = {}
        for group_id, group_profile in groups_info.items():
            if not isinstance(group_profile, dict):
                continue

            # 构造祂在这个群里的档案信息
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

            # 它会自己判断是该插入还是更新，完美！
            task = self.conversation_service.upsert_conversation_document(
                conversation_doc_to_upsert
            )
            tasks_with_context[group_id] = task

        if tasks_with_context:
            results = await asyncio.gather(*tasks_with_context.values(), return_exceptions=True)

            success_count = 0
            failure_details = []

            # 遍历结果，现在我们可以知道哪个群出错了
            # 使用 zip 的 strict=True (Python 3.10+) 来确保长度匹配，更安全
            for (group_id, _), result in zip(tasks_with_context.items(), results, strict=True):
                if isinstance(result, Exception):
                    failure_details.append(f"  - 群聊 {group_id}: {result!r}")
                else:
                    success_count += 1

            failure_count = len(results) - success_count

            log_message = (
                f"祂的档案同步完成。成功 upsert {success_count} 个会话，失败 {failure_count} 个。"
            )
            if failure_details:
                log_message += "\n失败详情:\n" + "\n".join(failure_details)

            if failure_count > 0:
                logger.warning(log_message)
            else:
                logger.info(log_message)
        else:
            logger.info("祂的档案报告中没有需要更新的群聊信息。")

    def _get_original_id_from_response(self, data: dict[str, Any]) -> str | None:
        """从响应事件中解析出 original_event_id.

        如果无法解析，将返回 None.
        """
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

    def _create_final_result_message(
        self, desc: str, succ: bool, err: str, det: dict | None
    ) -> str:
        """创建最终的结果消息.

        根据动作的成功与否，构建一个清晰的结果消息.
        如果有详细信息，则附加到消息末尾.
        """
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
        motivation: str | None = None
    ) -> None:
        """保存成功的动作作为事件到数据库中.

        这将确保动作的结果被记录下来，以便后续查询和分析.
        """
        event_to_save = sent_dict.copy()
        event_to_save["event_id"] = action_id
        event_to_save["timestamp"] = int(time.time() * 1000)
        event_to_save["status"] = "read"

        # 检查我们收到的 motivation 是不是一个有效的字符串
        if motivation and isinstance(motivation, str) and motivation.strip():
            # 如果是，就把它加到我们要存入数据库的 event_to_save 字典里
            event_to_save["motivation"] = motivation
            logger.debug(f"已为事件 '{action_id}' 添加了动机。")

        message_id = await self._get_sent_message_id_safe(resp_data)
        metadata = [{"type": "message_metadata", "data": {"message_id": message_id}}]
        event_to_save["content"] = metadata + event_to_save.get("content", [])

        # 获取真实的用户信息
        real_user_info = None
        conv_info = event_to_save.get("conversation_info")
        if conv_info and isinstance(conv_info, dict):
            conv_id = conv_info.get("conversation_id")
            if conv_id and self.action_handler.chat_session_manager:
                if session := self.action_handler.chat_session_manager.sessions.get(str(conv_id)):
                    # 从当前会话中获取机器人自己的档案
                    bot_profile = await session.get_bot_profile()
                    real_user_info = {
                        "platform": session.platform,
                        "user_id": bot_profile.get("user_id"),
                        "user_nickname": bot_profile.get("nickname"),
                        "user_cardname": bot_profile.get("card"),
                        "role": bot_profile.get("role")
                    }

        # 如果成功获取到真实信息，就用它；否则，使用之前的伪造信息作为后备
        if real_user_info:
            event_to_save["user_info"] = real_user_info
        else:
            event_to_save["user_info"] = {
                "platform": resp_data.get("platform", "unknown_platform"),
                "user_id": resp_data.get("bot_id", "unknown_user_id"),
                "user_nickname": "AIcarus (Self)",
            }

        await self.event_storage_service.save_event_document(event_to_save)
        logger.info(f"成功的平台动作 '{action_id}' 已作为事件存入 events 表。")

    async def _get_sent_message_id_safe(self, event_data: dict[str, Any]) -> str:
        """安全地从事件数据中提取 sent_message_id.

        如果无法提取，将返回一个默认值.
        """
        default_id = "unknow_message_id"
        if not isinstance(event_data, dict):
            logger.error(
                f"事件数据不是一个字典，无法从中安全地提取 sent_message_id。"
                f"事件数据类型: {type(event_data)}"
            )
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
