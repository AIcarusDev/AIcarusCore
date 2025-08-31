# 文件路径: src/services/action/action_handler.py
from __future__ import annotations

import asyncio
import time
import uuid
from typing import TYPE_CHECKING, Any, ClassVar

from src.common.custom_logging.logging_config import get_logger
from src.common.utils import parse_entity_uid
from src.domain.models import ActionMetadata, ActionResult
from src.os.apps.registry import platform_builder_registry
from src.os.communication.action_sender import ActionSender
from src.os.models import WindowStatus
from src.services.action.components.pending_action_manager import PendingActionManager
from src.services.action.services.sticker_service import StickerService
from src.services.database import (
    ActionLogStorageService,
    EntityGraphService,
    EventStorageService,
    ThoughtStorageService,
)

if TYPE_CHECKING:
    from src.mind.abilities.information_retrieval_service import InformationRetrievalService
    from src.mind.consciousness_flow import CoreLogic
    from src.os.apps.qq.qq_chat_session_manager import QQChatSessionManager
    from src.os.services.filesystem_service import FileSystemService
    from src.os.window_manager import WindowManager


logger = get_logger(__name__)

INFO_GATHERING_ACTIONS = {"get_list", "get_group_info", "get_bot_profile", "get_history"}


class ActionHandler:
    """处理所有与动作相关的逻辑，作为一个纯粹的调度中心."""

    NORMALIZATION_ACTIONS: ClassVar[dict[str, str]] = {
        "delete_friend": "user_id",
        "leave_conversation": "group_id",
    }

    def __init__(
        self,
        filesystem_service: FileSystemService,
        info_retrieval_service: InformationRetrievalService,
        thought_storage_service: ThoughtStorageService,
        event_storage_service: EventStorageService,
        action_log_service: ActionLogStorageService,
        action_sender: ActionSender,
        entity_service: EntityGraphService,
        sticker_service: StickerService,
    ) -> None:
        # 保存服务实例
        self.filesystem_service = filesystem_service
        self.info_retrieval_service = info_retrieval_service
        self.thought_storage_service = thought_storage_service
        self.event_storage_service = event_storage_service
        self.action_log_service = action_log_service
        self.action_sender = action_sender
        self.entity_service = entity_service
        self.sticker_service = sticker_service

        # 动态注入的依赖
        self.chat_session_manager: QQChatSessionManager | None = None
        self.core_logic: CoreLogic | None = None
        self.immediate_thought_trigger: asyncio.Event | None = None

        self.pending_action_manager = PendingActionManager(
            action_log_service=self.action_log_service,
            thought_storage_service=self.thought_storage_service,
            event_storage_service=self.event_storage_service,
            action_handler_instance=self,
        )
        logger.info(f"{self.__class__.__name__} instance created (等待动态依赖注入).")

    def set_dynamic_dependencies(
        self,
        chat_session_manager: QQChatSessionManager,
        core_logic: CoreLogic,
        trigger_event: asyncio.Event,
    ) -> None:
        """注入动态依赖 (在安检后)."""
        self.chat_session_manager = chat_session_manager
        self.core_logic = core_logic
        self.immediate_thought_trigger = trigger_event
        logger.info("ActionHandler 的动态依赖已成功设置。")

    async def handle_action_response(self, response_event_data: dict[str, Any]) -> None:
        """处理动作响应."""
        if self.pending_action_manager:
            await self.pending_action_manager.handle_response(response_event_data)
        else:
            logger.error("PendingActionManager 未初始化，无法处理动作响应。")

    async def _handle_do_nothing_action(self, action_json: dict, doc_key: str) -> None:
        """处理 do_nothing 动作."""
        motivation = action_json["core"]["do_nothing"].get("motivation", "决定保持沉默")
        logger.info(f"AI 决定不行动，动机: {motivation}")
        if self.core_logic and (session := self.core_logic._get_current_session()):
            session.no_action_count += 1
            logger.debug(
                f"[{session.conversation_id}] 连续不发言计数器已递增至: {session.no_action_count}"
            )
        if self.thought_storage_service:
            await self.thought_storage_service.save_action_result_to_thought(
                thought_key=doc_key, result_text=f"决定不行动，原因：{motivation}"
            )

    async def _handle_local_action(
        self, platform_id: str, action_name: str, params: dict, doc_key: str
    ) -> None:
        """处理本地执行的动作 (如 qq.scroll)."""
        result_text = ""
        if platform_id == "qq" and action_name == "scroll":
            # (此处的 _execute_local_scroll_action 逻辑保持不变)
            params_val = params.get("params")
            if not params_val or params_val not in ["up", "down"]:
                result_text = f"错误：收到无效的滚动方向 '{params_val}'。"
            elif not self.chat_session_manager:
                result_text = "错误：会话管理器未就绪，无法执行滚动。"
            elif platform_id not in self.chat_session_manager.platform_view_states:
                result_text = f"错误：找不到平台 '{platform_id}' 的视图状态。"
            else:
                state = self.chat_session_manager.platform_view_states[platform_id]
                current_offset = state.get("scroll_offset", 0)
                page_size = 10
                if params_val == "down":
                    state["scroll_offset"] = current_offset + page_size
                    action_desc = "向下"
                elif params_val == "up":
                    state["scroll_offset"] = max(0, current_offset - page_size)
                    action_desc = "向上"
                logger.info(f"平台 '{platform_id}' 视图已滚动, 新偏移量: {state['scroll_offset']}")
                result_text = f"成功地将列表 {action_desc} 滚动了一页。"

        if self.thought_storage_service:
            await self.thought_storage_service.save_action_result_to_thought(
                thought_key=doc_key, result_text=result_text
            )
        if self.immediate_thought_trigger:
            logger.info(f"本地动作 '{platform_id}.{action_name}' 完成，立即触发新一轮思考。")
            self.immediate_thought_trigger.set()

    async def process_action_flow(
        self,
        action_id: str,
        doc_key_for_updates: str,
        action_json: dict[str, Any],
        metadata: ActionMetadata,
    ) -> None:
        """统一的行动处理流程，负责分发任务到具体的处理器."""
        # (此方法内部逻辑保持不变)
        logger.info(f"[探灯B] ActionHandler 收到的 action_json: {action_json}")
        logger.info(
            f"-- [Action ID: {action_id}] 开始处理行动流程 (动机: {metadata.motivation[:50]}...) --"
        )
        if "do_nothing" in action_json.get("core", {}):
            await self._handle_do_nothing_action(action_json, doc_key_for_updates)
            return
        if not (platform_id := next(iter(action_json), None)) or not (
            actions_to_process := action_json.get(platform_id)
        ):
            logger.info("AI决策的动作对象为空或格式不正确，无需执行。")
            if self.core_logic and (session := self.core_logic._get_current_session()):
                session.no_action_count += 1
            return
        action_name, params = next(iter(actions_to_process.items()))
        if platform_id == "qq" and action_name == "scroll":
            await self._handle_local_action(platform_id, action_name, params, doc_key_for_updates)
        elif platform_id == "core":
            await self._handle_core_action_flow(action_name, params, doc_key_for_updates)
        elif platform_id == "qq" and action_name == "manage_stickers":
            if not self.sticker_service:
                logger.error("StickerService 未注入，无法处理 manage_stickers 动作。")
                return
            result_text = await self.sticker_service.manage_stickers(platform_id, params)
            if self.thought_storage_service:
                await self.thought_storage_service.save_action_result_to_thought(
                    thought_key=doc_key_for_updates, result_text=result_text
                )
            if self.immediate_thought_trigger:
                logger.info(
                    f"表情包管理动作 '{platform_id}.{action_name}' 完成，立即触发新一轮思考。"
                )
                self.immediate_thought_trigger.set()
        else:
            await self._execute_platform_action_flow(
                platform_id, action_name, params, doc_key_for_updates, metadata
            )
            if action_name in INFO_GATHERING_ACTIONS and self.immediate_thought_trigger:
                logger.info(
                    f"信息获取类平台动作 '{platform_id}.{action_name}' 完成，立即触发新一轮思考。"
                )
                self.immediate_thought_trigger.set()

    async def _handle_core_action_flow(self, action_name: str, params: dict, doc_key: str) -> None:
        """[核心修改] 处理 'core' 命名空间下的动作，分发到对应的服务."""
        result_text = ""
        try:
            # 分发到信息检索服务
            if action_name == "web_search":
                result_text = await self.info_retrieval_service.web_search(params)
            elif action_name == "summarize_url":
                result_text = await self.info_retrieval_service.summarize_url(params)
            # 分发到文件系统服务 (使用 to_thread 保证不阻塞事件循环)
            elif action_name == "list_files":
                result_text = await asyncio.to_thread(self.filesystem_service.list_files, params)
            elif action_name == "read_file":
                result_text = await asyncio.to_thread(self.filesystem_service.read_file, params)
            elif action_name == "write_file":
                result_text = await asyncio.to_thread(self.filesystem_service.write_file, params)
            elif action_name == "edit_file":
                result_text = await asyncio.to_thread(self.filesystem_service.edit_file, params)
            elif action_name == "get_aggregated_content":
                result_text = await asyncio.to_thread(
                    self.filesystem_service.get_aggregated_content, params
                )
            elif action_name == "delete_file":
                result_text = await asyncio.to_thread(self.filesystem_service.delete_file, params)
            else:
                logger.error(f"收到了一个未知的核心动作: '{action_name}'")
                result_text = f"错误：未知核心动作 '{action_name}'。"
        except Exception as e:
            logger.error(f"执行核心动作 '{action_name}' 时发生错误: {e}", exc_info=True)
            result_text = f"错误：执行核心动作 '{action_name}' 时发生内部错误。"

        if self.thought_storage_service:
            await self.thought_storage_service.save_action_result_to_thought(
                thought_key=doc_key, result_text=result_text
            )
        if self.immediate_thought_trigger:
            logger.info(f"核心动作 '{action_name}' 完成，立即触发新一轮思考。")
            self.immediate_thought_trigger.set()

    def _get_id_from_params(self, action_name: str, params: dict) -> str | None:
        """根据动作名称，从参数字典中提取目标ID字符串."""
        id_key = "user_id" if "friend" in action_name else "group_id"
        return params.get(id_key)

    def _get_id_from_session(self) -> str | None:
        """如果在会话上下文中，则从中提取原生ID作为回退."""
        if self.core_logic and (session := self.core_logic._get_current_session()):
            if parsed_tuple := parse_entity_uid(session.conversation_id):
                return parsed_tuple[2]
            logger.error(f"无法从当前会话的实体UID '{session.conversation_id}' 中解析出原生ID。")
        return None

    def _normalize_id_string(self, id_string: str, platform_id: str) -> str | None:
        """将一个可能是完整UID的字符串规范化为平台原生ID."""
        if parsed_tuple := parse_entity_uid(id_string):
            parsed_platform, _, native_id = parsed_tuple
            if parsed_platform != platform_id:
                logger.warning(
                    f"解析出的实体UID平台 '{parsed_platform}' 与当前动作平台 '{platform_id}' "
                    f"不匹配。将仍然使用其原生ID部分 '{native_id}'。"
                )
            return native_id
        return id_string

    def _resolve_target_id(self, action_name: str, params: dict, platform_id: str) -> str | None:
        """以清晰、可维护的方式解析出动作所需的目标原生ID."""
        raw_id = self._get_id_from_params(action_name, params)
        if not raw_id:
            raw_id = self._get_id_from_session()
        if not raw_id:
            id_key = "user_id" if "friend" in action_name else "group_id"
            logger.error(f"动作 '{action_name}' 缺少必要的 '{id_key}' 且不在有效的会话上下文中。")
            return None
        return self._normalize_id_string(raw_id, platform_id)

    async def _execute_platform_action_flow(
        self,
        platform_id: str,
        action_name: str,
        params: dict,
        doc_key_for_updates: str,
        metadata: ActionMetadata,
    ) -> None:
        """执行一个平台动作的完整流程，并传递元数据."""
        if not self.action_sender or platform_id not in self.action_sender.connected_adapters:
            error_msg = f"动作执行失败：平台 '{platform_id}' 理论上存在，但当前未连接。"
            logger.error(error_msg)
            if self.thought_storage_service:
                await self.thought_storage_service.save_action_result_to_thought(
                    thought_key=doc_key_for_updates, result_text=error_msg
                )
            return

        builder = platform_builder_registry.get_builder(platform_id)
        if not builder:
            logger.error(f"找不到平台 '{platform_id}' 的翻译官。")
            return
        if not self.entity_service:
            logger.error("EntityGraphService 未注入到 ActionHandler，无法获取祂的ID！")
            return
        if action_name in self.NORMALIZATION_ACTIONS:
            resolved_id = self._resolve_target_id(action_name, params, platform_id)
            if not resolved_id:
                error_msg = f"动作 '{action_name}' 执行失败：无法确定目标ID。"
                logger.error(error_msg)
                if self.thought_storage_service:
                    await self.thought_storage_service.save_action_result_to_thought(
                        thought_key=doc_key_for_updates, result_text=error_msg
                    )
                return
            id_key = self.NORMALIZATION_ACTIONS[action_name]
            params[id_key] = resolved_id
            logger.debug(f"已将动作 '{action_name}' 的目标ID归一化为: '{resolved_id}'")

        all_self_entities = await self.entity_service.get_all_self_entities()
        self_entity = next(
            (
                entity
                for entity in all_self_entities
                if entity.get("details", {}).get("platform") == platform_id
            ),
            None,
        )
        if not self_entity or not self_entity.get("details", {}).get("platform_id"):
            logger.error(f"无法为平台 '{platform_id}' 获取已安检的祂的客观实体ID。动作无法执行。")
            if self.thought_storage_service:
                await self.thought_storage_service.save_action_result_to_thought(
                    thought_key=doc_key_for_updates,
                    result_text=f"动作执行失败：我找不到自己在这个平台({platform_id})上的身份信息。",
                )
            return

        correct_bot_id = self_entity["details"]["platform_id"]
        action_event = builder.build_action_event(action_name, params, bot_id=correct_bot_id)
        if not action_event:
            logger.error(f"平台 '{platform_id}' 的翻译官不会翻译动作 '{action_name}'。")
            return

        await self._execute_platform_action(
            action_to_send=action_event.to_dict(),
            thought_doc_key=doc_key_for_updates,
            original_action_description=f"{platform_id}.{action_name}",
            metadata=metadata,
        )

    async def execute_simple_action(
        self,
        platform_id: str,
        action_name: str,
        params: dict,
        bot_id: str,
        description: str,
        motivation: str | None = None,
    ) -> ActionResult:
        """一个更简单的动作执行入口，供 MessageBuilder 等内部系统调用."""
        builder = platform_builder_registry.get_builder(platform_id)
        if not builder:
            return ActionResult(
                action_id="",
                is_success=False,
                error_message=f"找不到平台 '{platform_id}' 的翻译官。",
            )

        action_event = builder.build_action_event(action_name, params, bot_id=bot_id)
        if not action_event:
            return ActionResult(
                action_id="",
                is_success=False,
                error_message=f"平台 '{platform_id}' 的翻译官不会翻译动作 '{action_name}'。",
            )

        metadata = ActionMetadata(
            motivation=motivation or "由内部系统（如MessageBuilder）发起",
            source_event_id=None,
            source_thought_id=None,
        )

        return await self._execute_platform_action(
            action_to_send=action_event.to_dict(),
            thought_doc_key=None,
            original_action_description=description,
            metadata=metadata,
        )
    # [核心新增] 新方法，处理来自GUI的平台动作
    async def handle_aicos_gui_action(
        self,
        platform_id: str,
        action_name: str,
        params: dict,
        window_manager: WindowManager,
    ) -> None:
        """处理由 AIC-OS GUI 交互触发的平台特定动作 (如 send_message)."""
        if platform_id == "qq" and action_name == "send_message":
            await self._handle_gui_send_message(params, window_manager)
        else:
            logger.warning(
                f"ActionHandler 收到一个未知的 GUI 动作: {platform_id}.{action_name}"
            )

    async def _handle_gui_send_message(self, params: dict, window_manager: WindowManager) -> None:
        """从 GUI 动作参数中解析并发送消息."""
        target_window_id = params.get("target_window_id")
        steps = params.get("steps")
        motivation = params.get("motivation", "由AIC-OS MessageBuilder发起")

        if not target_window_id or not steps:
            logger.error("send_message 指令缺少 target_window_id 或 steps。")
            return

        window = window_manager.get_window(target_window_id)
        if (
            not window
            or window.window_class != "conversation"
            or window.status == WindowStatus.MINIMIZE
        ):
            logger.error(f"AI 试图向无效、非聊天或最小化的窗口 '{target_window_id}' 发送消息。")
            return

        conversation_uid = window.content_state.get("conversation_uid")
        if not conversation_uid:
            logger.error(f"窗口 '{target_window_id}' 缺少 conversation_uid 状态。")
            return

        parsed_info = parse_entity_uid(conversation_uid)
        if not parsed_info:
            logger.error(f"无法从持久化ID '{conversation_uid}' 中解析信息。")
            return

        platform, conv_type, native_id = parsed_info

        # 确保 chat_session_manager 存在
        if not self.chat_session_manager:
            logger.error("无法发送消息：QQChatSessionManager 未在 ActionHandler 中初始化。")
            return

        bot_id = self.chat_session_manager.self_bot_ids_map.get(platform)
        if not bot_id:
            logger.error(f"无法为平台 '{platform}' 找到对应的 bot_id。")
            return

        action_params_for_handler = {
            "conversation_id": native_id,
            "conversation_type": conv_type,
            "content": steps,
        }

        logger.info(
            f"准备通过 ActionHandler 发送消息至会话 '{conversation_uid}' (原生ID: {native_id})"
        )
        action_result = await self.execute_simple_action(
            platform_id=platform,
            action_name="send_message",
            params=action_params_for_handler,
            bot_id=bot_id,
            description="由 AIC-OS 发送",
            motivation=motivation,
        )

        if action_result.is_success:
            logger.info(f"消息已成功发送至会话 '{conversation_uid}'。回执: {action_result.payload}")
            window_manager.focus_window(target_window_id)
        else:
            logger.error(f"消息发送至会话 '{conversation_uid}' 失败: {action_result.error_message}")

    async def _execute_platform_action(
        self,
        action_to_send: dict[str, Any],
        thought_doc_key: str | None,
        original_action_description: str,
        metadata: ActionMetadata,
    ) -> ActionResult:
        """底层动作执行器：发送动作到适配器并等待响应."""
        core_action_id = action_to_send.setdefault("event_id", str(uuid.uuid4()))
        if not self.action_sender or not self.action_log_service or not self.pending_action_manager:
            return ActionResult(
                action_id=core_action_id,
                is_success=False,
                error_message="内部错误：核心服务不可用。",
            )

        event_type = action_to_send.get("event_type", "")
        platform = event_type.split(".")[1] if "." in event_type else "unknown"
        timestamp = int(time.time() * 1000)
        action_to_send["timestamp"] = timestamp
        bot_id_for_log = action_to_send.get("bot_id")
        if not bot_id_for_log:
            logger.error(
                f"严重逻辑错误：动作事件中缺少 bot_id！无法记录日志。事件: {action_to_send}"
            )
            bot_id_for_log = "error_missing_bot_id"

        from src.services.database.models import ActionLogDocument

        action_doc = ActionLogDocument(
            _key=core_action_id,
            action_type=event_type,
            timestamp=timestamp,
            bot_id=bot_id_for_log,
            platform=platform,
            status="pending",
        )
        await self.action_log_service.save_action_attempt(action_doc)

        try:
            send_success = await self.action_sender.send_action_to_adapter_by_id(
                platform, action_to_send
            )
            if not send_success:
                return ActionResult(
                    action_id=core_action_id,
                    is_success=False,
                    error_message=f"发送到适配器 '{platform}' 失败。",
                )
        except Exception as e:
            return ActionResult(
                action_id=core_action_id,
                is_success=False,
                error_message=f"发送平台动作时发生意外异常: {e}",
            )

        return await self.pending_action_manager.add_and_wait_for_action(
            action_id=core_action_id,
            thought_doc_key=thought_doc_key,
            original_action_description=original_action_description,
            action_to_send=action_to_send,
            metadata=metadata,
        )
