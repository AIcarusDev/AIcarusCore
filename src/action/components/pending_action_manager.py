# src/action/components/pending_action_manager.py
import asyncio
import json
import time
from typing import TYPE_CHECKING, Any

from aicarus_protocols import find_seg_by_type
from src.common.custom_logging.logging_config import get_logger
from src.common.utils import build_conversation_entity_uid, parse_focus_path
from src.database import (
    ActionLogStorageService,
    ThoughtStorageService,
)
from src.database.models import ActionLogDocument
from src.database.services.event_storage_service import EventStorageService
from src.domain.models import ActionMetadata, ActionResult

if TYPE_CHECKING:
    from src.action.action_handler import ActionHandler

logger = get_logger(__name__)

ACTION_RESPONSE_TIMEOUT_SECONDS = 30


class PendingActionManager:
    """管理所有待处理的平台动作."""

    def __init__(
        self,
        action_log_service: ActionLogStorageService,
        thought_storage_service: ThoughtStorageService,
        event_storage_service: EventStorageService,
        action_handler_instance: "ActionHandler",
    ) -> None:
        # _pending_actions 现在存储 Future[ActionResult] 和 ActionMetadata
        self._pending_actions: dict[
            str,
            tuple[asyncio.Future[ActionResult], str | None, str, dict[str, Any], ActionMetadata],
        ] = {}
        self.action_log_service = action_log_service
        self.thought_storage_service = thought_storage_service
        self.event_storage_service = event_storage_service
        self.action_handler = action_handler_instance
        logger.info(f"{self.__class__.__name__} instance created (领域驱动改造版).")

    # 方法签名改变，接收 ActionMetadata，返回 ActionResult
    async def add_and_wait_for_action(
        self,
        action_id: str,
        thought_doc_key: str | None,
        original_action_description: str,
        action_to_send: dict[str, Any],
        metadata: ActionMetadata,
    ) -> ActionResult:
        """添加一个待处理的动作，并等待其响应，返回一个 ActionResult 领域对象."""
        response_future: asyncio.Future[ActionResult] = asyncio.Future()
        self._pending_actions[action_id] = (
            response_future,
            thought_doc_key,
            original_action_description,
            action_to_send,
            metadata,  # 存储元数据
        )
        try:
            # Future 现在会返回一个完整的 ActionResult 对象
            return await asyncio.wait_for(response_future, timeout=ACTION_RESPONSE_TIMEOUT_SECONDS)
        except TimeoutError:
            if action_id in self._pending_actions:
                await self._handle_action_timeout(action_id)
            return ActionResult(
                action_id=action_id,
                is_success=False,
                error_message=f"动作 '{original_action_description}' 响应超时。",
            )
        finally:
            self._pending_actions.pop(action_id, None)

    async def _handle_action_timeout(self, action_id: str) -> None:
        """处理动作超时的情况."""
        if action_id not in self._pending_actions:
            return
        logger.warning(f"动作 '{action_id}' 超时未收到响应！")
        pending_future, _, _, _, _ = self._pending_actions.pop(action_id)
        if not pending_future.done():
            # 超时也设置一个 ActionResult
            timeout_result = ActionResult(
                action_id=action_id, is_success=False, error_message="动作响应超时。"
            )
            pending_future.set_result(timeout_result)
        await self.action_log_service.update_action_log_with_response(
            action_id=action_id,
            status="timeout",
            response_timestamp=int(time.time() * 1000),
            error_info="Action response timed out",
        )

    async def handle_response(self, response_event_data: dict[str, Any]) -> None:
        """处理收到的动作响应事件."""
        original_action_id = self._get_original_id_from_response(response_event_data)
        if not original_action_id:
            return

        if original_action_id not in self._pending_actions:
            logger.warning(f"收到未知的或已处理/超时的 action_response，ID: {original_action_id}。")
            return

        pending_future, thought_doc_key, description, sent_dict, metadata = (
            self._pending_actions.pop(original_action_id)
        )
        logger.info(f"已匹配到等待中的动作 '{original_action_id}' ({description})。")

        successful, status, error_msg, details = self._parse_response_content(response_event_data)

        # 在这里“铸造” ActionResult 领域模型
        action_result = ActionResult(
            action_id=original_action_id,
            is_success=successful,
            payload=details,
            error_message=None if successful else error_msg,
        )

        if not pending_future.done():
            pending_future.set_result(action_result)

        if successful:
            await self._handle_successful_action_side_effects(sent_dict, details)

        await self._gather_and_execute_db_updates(
            action_result=action_result,
            status=status,
            sent_dict=sent_dict,
            thought_doc_key=thought_doc_key,
            description=description,
            metadata=metadata,
            response_event_data=response_event_data,
        )

    async def _handle_successful_action_side_effects(
        self, sent_dict: dict[str, Any], details: dict | None
    ) -> None:
        original_action_type = sent_dict.get("event_type")
        if not original_action_type:
            return
        if original_action_type.endswith(".get_list"):
            await self._proactively_create_conversation_docs_from_list(details, sent_dict)
        if original_action_type.endswith(".handle_friend_request"):
            params_seg = find_seg_by_type(sent_dict.get("content", []), "action_params")
            if not (
                params_seg
                and isinstance(params_seg.data, dict)
                and (params := params_seg.data)
                and (user_id := params.get("user_id"))
                and (platform := sent_dict.get("platform"))
            ):
                logger.error("处理 handle_friend_request 后续时，缺少 user_id 或 platform。")
                return
            entity_uid = f"{platform}_{user_id}"
            approved = params.get("approve", False)
            remark = params.get("remark") if approved else None
            await self.action_handler.entity_service.finalize_friend_request(
                entity_uid=entity_uid, approved=approved, remark=remark
            )
            logger.info(f"好友请求处理完毕，已通过服务更新实体 '{entity_uid}' 的数据库状态。")
        if original_action_type.endswith(".leave_conversation"):
            await self._handle_post_leave_conversation(sent_dict)

    async def _handle_post_leave_conversation(self, sent_dict: dict[str, Any]) -> None:
        csm = self.action_handler.chat_session_manager
        if not (csm and csm.current_focus_path):
            return
        params_seg = find_seg_by_type(sent_dict.get("content", []), "action_params")
        if not (params_seg and isinstance(params_seg.data, dict)):
            return
        params = params_seg.data
        left_group_id = params.get("group_id")
        platform_id = sent_dict.get("platform")
        if not (left_group_id and platform_id):
            logger.error(
                "leave_conversation 成功但无法从 sent_dict 中提取 platform_id 或 group_id。"
            )
            return
        left_conv_entity_uid = build_conversation_entity_uid(
            platform_id, "group", str(left_group_id)
        )
        current_focus_path_str = csm.current_focus_path.get("target_path")
        level, focus_platform, focus_conv_part = parse_focus_path(current_focus_path_str)
        if level != "cellular":
            return
        try:
            focus_conv_type, focus_actual_id = focus_conv_part.split(".", 1)
            current_focus_entity_uid = build_conversation_entity_uid(
                focus_platform, focus_conv_type, focus_actual_id
            )
        except (ValueError, IndexError):
            return
        if left_conv_entity_uid == current_focus_entity_uid:
            logger.warning(
                f"检测到 AI 已成功离开当前所在的会话 '{left_conv_entity_uid}'。"
                f"将强制执行 'return' 操作。"
            )
            session_to_leave = csm.sessions.get(left_conv_entity_uid)
            left_conv_name = (
                session_to_leave.conversation_name if session_to_leave else left_conv_entity_uid
            )
            return_params = {"motivation": f"已成功退出会话 '{left_conv_name}'，因此返回到平台。"}
            await csm.focus_manager.handle_focus_control(command="return", params=return_params)
            logger.info(f"已成功触发对 '{left_conv_entity_uid}' 的强制 'return' 操作。")

    # ======================== [ 核心改造点 5 ] ========================
    # 方法签名改变，接收 ActionResult 和 ActionMetadata
    async def _gather_and_execute_db_updates(
        self,
        action_result: ActionResult,
        status: str,
        sent_dict: dict[str, Any],
        thought_doc_key: str | None,
        description: str,
        metadata: ActionMetadata,
        response_event_data: dict[str, Any],
    ) -> None:
        """打包并执行所有与数据库更新相关的异步任务."""
        response_timestamp = int(time.time() * 1000)
        response_time_ms = response_timestamp - sent_dict.get("timestamp", response_timestamp)
        update_doc = ActionLogDocument(
            _key=action_result.action_id,
            action_type="",  # type is not needed for update
            timestamp=0,  # timestamp is not needed for update
            platform="",  # platform is not needed for update
            bot_id="",  # bot_id is not needed for update
            status=status,
            response_timestamp=response_timestamp,
            response_time_ms=response_time_ms,
            error_info=action_result.error_message,
            result_details=action_result.payload,
        )

        tasks_to_gather = [
            # 传递 ActionLogDocument 对象
            self.action_log_service.update_action_log_with_response(
                action_id=action_result.action_id,
                updates=update_doc.to_dict(),  # 传递字典
            )
        ]
        if thought_doc_key:
            # 使用新的辅助函数
            result_message = self._create_final_result_message(description, action_result)
            tasks_to_gather.append(
                self.thought_storage_service.save_action_result_to_thought(
                    thought_key=thought_doc_key, result_text=result_message
                )
            )
        if action_result.is_success:
            tasks_to_gather.append(
                self._save_successful_action_as_event(
                    action_result.action_id, sent_dict, response_event_data, metadata=metadata
                )
            )
        if tasks_to_gather:
            await asyncio.gather(*tasks_to_gather)

    async def _proactively_create_conversation_docs_from_list(
        self, details: dict | None, sent_dict: dict
    ) -> None:
        if not details or not isinstance(details, dict):
            return
        list_type = sent_dict.get("content", [{}])[0].get("data", {}).get("list_type")
        platform_id = sent_dict.get("platform")
        if not list_type or not platform_id:
            logger.warning("无法从 get_list 的原始请求中获取足够信息来创建会话实体。")
            return
        items = details.get("friends", []) if list_type == "friend" else details.get("groups", [])
        if not items or not isinstance(items, list):
            return
        logger.info(
            f"收到 get_list({list_type}) 的成功响应，"
            f"准备为 {len(items)} 个项目主动创建/更新会话实体。"
        )
        if not self.action_handler.entity_service:
            logger.error("EntityGraphService 未注入到 ActionHandler，无法主动创建会话实体。")
            return
        entity_service = self.action_handler.entity_service
        conv_type = "private" if list_type == "friend" else "group"
        creation_tasks = []
        for item in items:
            if not isinstance(item, dict):
                continue
            conv_id = item.get("user_id") if list_type == "friend" else item.get("group_id")
            conv_name = item.get("nickname") if list_type == "friend" else item.get("group_name")
            if not conv_id:
                continue
            task = entity_service.get_or_create_conversation_entity(
                conversation_id=str(conv_id),
                platform=platform_id,
                conv_type=conv_type,
                name=conv_name,
            )
            creation_tasks.append(task)
        if creation_tasks:
            await asyncio.gather(*creation_tasks)
            logger.info(f"已完成对 {len(creation_tasks)} 个项目的会话实体主动更新。")

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

    # 方法签名改变，接收 ActionResult
    def _create_final_result_message(self, description: str, result: ActionResult) -> str:
        """根据 ActionResult 创建最终的结果消息."""
        if result.is_success:
            msg = f"动作 '{description}' 已成功执行。"
            if result.payload:
                try:
                    # 尝试格式化 payload，如果失败则使用原始字符串
                    payload_str = json.dumps(result.payload, ensure_ascii=False, indent=2)
                    msg += f" 详情: {payload_str}"
                except (TypeError, ValueError):
                    msg += f" 详情: {result.payload!s}"
            return msg
        return f"动作 '{description}' 执行失败: {result.error_message}"

    # 方法签名改变，接收 ActionMetadata
    async def _save_successful_action_as_event(
        self,
        action_id: str,
        sent_dict: dict[str, Any],
        resp_data: dict[str, Any],
        metadata: ActionMetadata,
    ) -> None:
        """将成功的动作存储为事件."""
        event_to_save = sent_dict.copy()
        
        # --- 关键修复！---
        # 从 event_type 中解析出 platform_id 并添加到字典中
        event_type_full = event_to_save.get("event_type", "")
        platform = event_type_full.split(".")[1] if "." in event_type_full else "unknown"
        event_to_save["platform"] = platform
        # --- 修复结束 ---

        event_to_save["event_id"] = action_id
        event_to_save["timestamp"] = int(time.time() * 1000)
        event_to_save["status"] = "read"
        conv_info = event_to_save.get("conversation_info")
        original_action_type = event_to_save.get("event_type", "")
        if original_action_type.endswith(".send_message"):
            if conv_info and isinstance(conv_info, dict):
                conv_type = conv_info.get("type", "unknown")
                event_to_save["event_type"] = f"message.{platform}.{conv_type}"
        # 从元数据中获取动机
        if metadata.motivation and metadata.motivation.strip():
            event_to_save["motivation"] = metadata.motivation
        message_id = await self._get_sent_message_id_safe(resp_data)
        event_to_save["content"] = [
            {"type": "message_metadata", "data": {"message_id": message_id}},
            *event_to_save.get("content", []),
        ]
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
        await self.event_storage_service.save_event_document(event_to_save)
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
