# src/prompt_builder/user_prompt_parts_builder.py
import asyncio
import json
from typing import TYPE_CHECKING, Any, Optional

from src.common.utils import build_conversation_entity_uid
from src.platform_builders.registry import platform_builder_registry

if TYPE_CHECKING:
    from src.core_logic.state_manager import AIStateManager
    from src.database.services.entity_graph_service import EntityGraphService
    from src.database.services.thought_storage_service import ThoughtStorageService
    from src.focus_chat_mode.chat_session import ChatSession
    from src.focus_chat_mode.chat_session_manager import ChatSessionManager


class UserPromptPartsBuilder:
    """负责构建填充 User Prompt 模板所需的所有部分."""

    def __init__(
        self,
        thought_storage_service: "ThoughtStorageService",
        entity_service: "EntityGraphService",
        chat_session_manager: "ChatSessionManager",
        state_manager: "AIStateManager",
    ) -> None:
        self.thought_storage = thought_storage_service
        self.entity_service = entity_service
        self.chat_session_manager = chat_session_manager
        self.state_manager = state_manager

    async def build(
        self,
        handover_result: dict | None,
        meta_info_block: str,
        external_info_block: str,
        platform_id: str,
        level: str,
        session: Optional["ChatSession"] = None,
    ) -> dict[str, Any]:
        """构建 User Prompt 的所有部分."""
        # --- Parallel Data Fetching ---
        action_response_task = self._build_action_response_desc(handover_result)
        friend_request_task = self._build_friend_request_block(platform_id, level)

        action_response_block, friend_request_block = await asyncio.gather(
            action_response_task, friend_request_task
        )

        # --- Synchronous Logic ---
        command_feedback_block = self._get_command_feedback_block(session)

        return {
            "action_response_block": action_response_block,
            "command_feedback_block": command_feedback_block,
            "meta_info_block": meta_info_block,
            "external_info_block": external_info_block,
            "friend_request_block": friend_request_block,
        }

    def _get_command_feedback_block(self, session: Optional["ChatSession"]) -> str:
        feedback_text = ""
        if session and session.last_command_feedback:
            feedback_text = session.last_command_feedback
            session.last_command_feedback = None
        elif self.chat_session_manager and self.chat_session_manager.global_command_feedback:
            feedback_text = self.chat_session_manager.global_command_feedback
            self.chat_session_manager.global_command_feedback = None

        return f"<command_feedback>\n{feedback_text}\n</command_feedback>" if feedback_text else ""

    async def _get_latest_action_context(
        self, handover_result: dict | None
    ) -> tuple[dict | None, str | None, dict | None]:
        latest_thought = await self.thought_storage.get_latest_thought_document()
        if not latest_thought:
            return None, None, None
        action_result_text = (
            handover_result.get("result_text")
            if handover_result
            else latest_thought.get("action_result")
        )
        action_payload = latest_thought.get("action_payload") or {}
        return latest_thought, action_result_text, action_payload

    def _parse_action_details_from_payload(
        self, action_payload: dict
    ) -> tuple[str | None, str | None, dict | None]:
        try:
            action_part = action_payload.get("action", {})
            if not action_part or not isinstance(action_part, dict):
                return None, None, None
            known_platform_keys = platform_builder_registry.get_all_builders().keys()
            if (
                all(key not in known_platform_keys for key in action_part)
                and "core" not in action_part
            ):
                action_part = {"core": action_part}
            platform_key = next(iter(action_part), None)
            platform_actions = action_part.get(platform_key, {}) if platform_key else {}
            action_name = next(iter(platform_actions), None)
            return (
                platform_key,
                action_name,
                platform_actions.get(action_name, {}) if action_name else {},
            )
        except (StopIteration, AttributeError):
            return None, None, None

    def _create_action_description_prefix(
        self, platform_key: str | None, action_name: str | None, action_params: dict | None
    ) -> str:
        """构建动作反馈前缀."""
        if not action_name:
            return "你刚才的行动成功了，返回了以下信息："
        if action_name == "web_search" and action_params and (query := action_params.get("query")):
            return f"你刚才执行了网页搜索，搜索的关键词是“{query}”，得到了以下结果："
        return f"你刚才执行了动作 “{platform_key}.{action_name}”，得到了以下结果："

    async def _post_process_get_list_result(self, result_text: str, platform_key: str) -> str:
        try:
            self_entity = await self.entity_service.get_self_entity_by_platform(platform_key)
            self_platform_id = (
                str(self_entity.get("details", {}).get("platform_id")) if self_entity else None
            )
            result_list = json.loads(result_text)
            if not isinstance(result_list, list):
                return result_text
            list_type = (
                "friend"
                if "user_id" in result_list[0]
                else "group"
                if "group_id" in result_list[0]
                else "unknown"
            )
            builder = platform_builder_registry.get_builder(platform_key)
            keys_to_keep = (
                builder.get_list_keys_to_keep(list_type)
                if builder
                else {"user_id", "group_id", "nickname", "remark", "group_name"}
            )
            cleaned_list = []
            for item in result_list:
                if not isinstance(item, dict):
                    continue
                item_type, item_id = (
                    ("private", str(item.get("user_id")))
                    if "user_id" in item
                    else (
                        ("group", str(item.get("group_id"))) if "group_id" in item else (None, None)
                    )
                )
                if not item_id or (
                    item_type == "private" and self_platform_id and item_id == self_platform_id
                ):
                    continue
                cleaned_item = {k: v for k, v in item.items() if k in keys_to_keep}
                entity_uid = build_conversation_entity_uid(platform_key, item_type, item_id)
                if item_type == "private":
                    cleaned_item["user_id"] = entity_uid
                else:
                    cleaned_item["group_id"] = entity_uid
                cleaned_list.append(cleaned_item)
            return json.dumps(cleaned_list, indent=4, ensure_ascii=False)
        except (json.JSONDecodeError, TypeError, IndexError):
            return result_text
        except Exception:
            return result_text

    async def _build_action_response_desc(self, handover_result: dict | None) -> str:
        """构建动作响应描述."""
        _, action_result_text, action_payload = await self._get_latest_action_context(
            handover_result
        )
        if not action_result_text or "决定不行动" in action_result_text:
            return ""
        platform_key, action_name, action_params = self._parse_action_details_from_payload(
            action_payload
        )
        action_desc = self._create_action_description_prefix(
            platform_key, action_name, action_params
        )
        if action_name == "get_list" and platform_key:
            action_result_text = await self._post_process_get_list_result(
                action_result_text, platform_key
            )
        return f"<action_response>\n{action_desc}\n{action_result_text}\n</action_response>"

    async def _build_friend_request_block(self, platform_id: str, level: str) -> str:
        if level not in {"platform", "cellular"}:
            return ""
        from src.common.time_utils import format_relative_time

        requests = await self.entity_service.get_pending_friend_requests(platform_id)
        if not requests:
            return (
                "<friend_request>\n你在该平台暂时没有来自他人的未处理好友请求。\n</friend_request>"
            )
        lines = ["<friend_request>", "你在该平台有以下来自他人的未处理好友请求："]
        for req in sorted(requests, key=lambda r: r.get("timestamp", 0), reverse=True):
            time_str = format_relative_time(req.get("timestamp", 0))
            lines.append(
                f"- 来自“{req.get('nickname', '未知用户')}”(ID: {req.get('user_id')})的请求, "
                f"flag: `{req.get('flag')}` ({time_str}): "
                f"“验证消息：{req.get('comment', '无验证消息')}”"
            )
        lines.append("</friend_request>")
        return "\n".join(lines)
