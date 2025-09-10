# 文件路径: src/services/action/action_handler.py
from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from aicarus_protocols import UserInfo as ProtocolUserInfo
from src.common.custom_logging.logging_config import get_logger
from src.domain.models import ActionMetadata, ActionResult
from src.os.apps.interfaces import IApp
from src.os.communication.action_sender import ActionSender
from src.os.models import WindowStatus
from src.services.action.components.message_builder import MessageBuilder
from src.services.action.components.pending_action_manager import PendingActionManager
from src.services.database import (
    ActionLogStorageService,
    EntityGraphService,
    EventStorageService,
    ThoughtStorageService,
)
from src.services.database.models import ActionLogDocument

if TYPE_CHECKING:
    from src.bootstrap.container import ServiceContainer
    from src.mind.abilities.information_retrieval_service import InformationRetrievalService
    from src.os.application_manager import ApplicationManager
    from src.os.apps.qq.sticker_service import QQStickerService
    from src.os.services.filesystem_service import FileSystemService
    from src.os.state_generator import AICOSStateGenerator
    from src.os.window_manager import WindowManager


logger = get_logger(__name__)


class ActionHandler:
    """处理所有需要与外部适配器进行异步通信的动作.

    纯粹的“外部动作”调度中心。
    """

    def __init__(
        self,
        filesystem_service: FileSystemService,
        info_retrieval_service: InformationRetrievalService,
        thought_storage_service: ThoughtStorageService,
        event_storage_service: EventStorageService,
        action_log_service: ActionLogStorageService,
        action_sender: ActionSender,
        entity_service: EntityGraphService,
        qq_sticker_service: QQStickerService,
    ) -> None:
        self.aicos_state_generator: AICOSStateGenerator | None = None
        self.application_manager: ApplicationManager | None = None
        self.filesystem_service = filesystem_service
        self.info_retrieval_service = info_retrieval_service
        self._cycle_trigger: Callable[[], None] | None = None
        self.thought_storage_service = thought_storage_service
        self.event_storage_service = event_storage_service
        self.action_log_service = action_log_service
        self.action_sender = action_sender
        self.entity_service = entity_service
        self.qq_sticker_service = qq_sticker_service

        self.pending_action_manager = PendingActionManager()
        logger.info(f"{self.__class__.__name__} instance created (Refactored).")

    def set_state_generator(self, state_generator: AICOSStateGenerator) -> None:
        """注入 AICOSStateGenerator 实例以解决循环依赖."""
        self.aicos_state_generator = state_generator

    def set_application_manager(self, application_manager: ApplicationManager) -> None:
        """注入 ApplicationManager 实例以解决循环依赖."""
        self.application_manager = application_manager

    async def handle_action_response(self, response_event_data: dict[str, Any]) -> None:
        """处理动作响应，直接委托给 PendingActionManager."""
        await self.pending_action_manager.handle_response(response_event_data)

    async def process_action_flow(
        self,
        action_id: str,
        doc_key_for_updates: str,
        action_json: dict[str, Any],
        metadata: ActionMetadata,
    ) -> None:
        """统一的外部行动处理流程.

        现在只处理 innate 和需要发往适配器的 platform 动作。
        """
        namespace = next(iter(action_json), None)
        if not namespace:
            return

        actions_to_process = action_json[namespace]
        action_name, params = next(iter(actions_to_process.items()))

        if namespace == "innate":
            await self._handle_innate_action(action_name, params, doc_key_for_updates)
        else:
            await self._execute_platform_action_flow(
                namespace, action_name, params, doc_key_for_updates, metadata
            )

    async def _handle_innate_action(self, action_name: str, params: dict, doc_key: str) -> None:
        """处理所有固有的、本地执行的核心能力."""
        if not self.aicos_state_generator:
            logger.error(
                "ActionHandler 未能获取到 AICOSStateGenerator 实例，无法执行 connect 动作。"
            )
            result_text = "错误：系统内部状态管理器未准备好，无法连接。"
            await self.thought_storage_service.save_action_result_to_thought(
                thought_key=doc_key, result_text=result_text
            )
            return

        result_text = ""
        try:
            if action_name == "connect":
                device_name = params.get("device_name")
                if device_name == "AIC-OS":
                    self.aicos_state_generator.is_connected = True
                    result_text = "成功！已连接到 AIC-OS 设备。"
                    logger.info("AIC-OS 状态已切换为已连接。")
                else:
                    result_text = f"错误：无法连接到未知的设备 '{device_name}'。"
            elif action_name == "web_search":
                result_text = await self.info_retrieval_service.web_search(params)
            elif action_name == "summarize_url":
                result_text = await self.info_retrieval_service.summarize_url(params)
            elif action_name == "list_files":
                result_text = await asyncio.to_thread(self.filesystem_service.list_files, params)
            elif action_name == "read_file":
                path_str = params.get("path")
                safe_path = self.filesystem_service.resolve_safe_path(path_str)
                if not safe_path:
                    result_text = f"错误：路径 '{path_str}' 不安全或无效。"
                else:
                    result_text = await asyncio.to_thread(
                        self.filesystem_service.read_file, safe_path, path_str
                    )
            elif action_name == "write_file":
                result_text = await asyncio.to_thread(self.filesystem_service.write_file, params)
            elif action_name == "edit_file":
                result_text = await asyncio.to_thread(self.filesystem_service.edit_file, params)
            elif action_name == "get_aggregated_content":
                result_text = await asyncio.to_thread(
                    self.filesystem_service.get_aggregated_content, params
                )
            elif action_name == "delete_workspace_file":  # 修正方法名
                result_text = await asyncio.to_thread(self.filesystem_service.delete_file, params)
            else:
                logger.error(f"收到了一个未知的核心动作: '{action_name}'")
                result_text = f"错误：未知核心动作 '{action_name}'。"
        except Exception as e:
            logger.error(f"执行核心动作 '{action_name}' 时发生错误: {e}", exc_info=True)
            result_text = f"错误：执行核心动作 '{action_name}' 时发生内部错误。"

        await self.thought_storage_service.save_action_result_to_thought(
            thought_key=doc_key, result_text=result_text
        )

    async def request_media_from_adapters(self, missing_hashes_map: dict[str, str]) -> list[dict]:
        """向多个Adapter并发请求缺失的媒体文件.

        Args:
            missing_hashes_map: 一个字典，键是缺失的图片哈希，值是对应的 platform_id。

        Returns:
            一个成功获取的图片数据字典列表，每个字典包含 "hash", "base64", "mime_type"。
        """
        if not missing_hashes_map:
            return []

        tasks = []
        for hash_val, platform_id in missing_hashes_map.items():
            # bot_id 在这里不重要，因为我们是向平台本身请求资源，而不是代表某个bot
            # 但为了兼容 execute_simple_action, 我们需要提供一个
            # 我们可以从 ApplicationManager 获取
            bot_id = self.application_manager.get_self_bot_ids_map().get(platform_id, "unknown_bot")

            task = self.execute_simple_action(
                platform_id=platform_id,
                action_name="media.get",
                params={"hash": hash_val},
                bot_id=bot_id,
                description=f"回源请求媒体文件 {hash_val[:10]}"
            )
            tasks.append(task)

        logger.info(f"正在并发地向Adapter回源请求 {len(tasks)} 个媒体文件...")
        results = await asyncio.gather(*tasks)

        fetched_images = []
        for res in results:
            if res.is_success and isinstance(res.payload, dict):
                # 确认 payload 包含必要字段
                if all(k in res.payload for k in ["hash", "base64", "mime_type"]):
                    fetched_images.append(res.payload)
                else:
                    logger.warning(
                        f"Adapter为媒体请求 {res.action_id} 返回了不完整的payload: {res.payload}"
                    )
            elif not res.is_success:
                logger.error(f"媒体回源请求 {res.action_id} 失败: {res.error_message}")


        logger.info(f"成功从Adapter获取了 {len(fetched_images)} / {len(tasks)} 个媒体文件。")
        return fetched_images

    async def _execute_platform_action_flow(
        self,
        platform_id: str,
        action_name: str,
        params: dict,
        doc_key_for_updates: str,
        metadata: ActionMetadata,
    ) -> None:
        """执行一个平台动作的完整流程，负责构建Event并调用底层执行器."""
        if not self.action_sender or platform_id not in self.action_sender.connected_adapters:
            error_msg = f"动作执行失败：平台 '{platform_id}' 理论上存在，但当前未连接。"
            logger.error(error_msg)
            await self.thought_storage_service.save_action_result_to_thought(
                thought_key=doc_key_for_updates, result_text=error_msg
            )
            return

        if not self.application_manager:
            logger.error("ActionHandler 未能获取到 ApplicationManager 实例。")
            return
        builder = self.application_manager.get_builder_by_name(platform_id)
        if not builder:
            logger.error(f"找不到平台 '{platform_id}' 的翻译官。")
            return

        self_entity = await self.entity_service.get_self_entity_by_platform(platform_id)
        if not self_entity or not (bot_id := self_entity.get("details", {}).get("platform_id")):
            error_msg = f"动作执行失败：我找不到自己在这个平台({platform_id})上的身份信息。"
            logger.error(f"无法为平台 '{platform_id}' 获取已安检的自身实体ID。")
            await self.thought_storage_service.save_action_result_to_thought(
                thought_key=doc_key_for_updates, result_text=error_msg
            )
            return

        action_event = builder.build_action_event(action_name, params, bot_id=str(bot_id))
        if not action_event:
            logger.error(f"平台 '{platform_id}' 的翻译官不会翻译动作 '{action_name}'。")
            return

        action_event_dict = action_event.to_dict()
        action_event_dict["platform"] = platform_id

        action_result = await self._execute_platform_action(
            action_to_send=action_event_dict,
            original_action_description=f"{platform_id}.{action_name}",
        )

        await self._process_action_result(
            action_result,
            doc_key_for_updates,
            f"{platform_id}.{action_name}",
            action_event_dict,
            metadata,
        )

    async def handle_aicos_gui_action(
        self,
        platform_id: str,
        action_name: str,
        params: dict,
        window_manager: WindowManager,
        container: ServiceContainer,
        thought_key: str,
    ) -> None:
        """处理由 UI Dispatcher 转发来的、需要与适配器通信的 GUI 动作."""
        if platform_id == "qq":
            if action_name == "send_message":
                await self._handle_gui_send_message(params, window_manager, container)
            # 路由 manage_stickers 动作
            elif action_name == "manage_stickers" and self.application_manager:
                qq_builder = self.application_manager.get_builder_by_name("qq")
                if qq_builder and hasattr(qq_builder, "handle_sticker_action"):
                    await qq_builder.handle_sticker_action(params, container, thought_key)
        else:
            logger.warning(
                f"ActionHandler 收到一个未知的 GUI 动作转发: {platform_id}.{action_name}"
            )

    async def _handle_gui_send_message(
        self, params: dict, window_manager: WindowManager, container: ServiceContainer
    ) -> None:
        """从 GUI 动作参数中解析、构建并发送复杂消息."""
        target_conversation_uid = params.get("target_conversation_uid")
        steps = params.get("steps")
        motivation = params.get("motivation")

        if not target_conversation_uid:
            logger.error("send_message 指令缺少 target_conversation_uid 参数")
            return
        if not steps:
            logger.error("send_message 指令缺少 steps 参数")
            return
        if not motivation:
            logger.error("send_message 指令缺少 motivation 参数")
            return

        # 窗口和会话的有效性检查
        target_window = next(
            (
                w for w in window_manager.get_all_windows_sorted()
                if w.content_state.get("conversation_uid") == target_conversation_uid
                and w.status != WindowStatus.MINIMIZE
            ),
            None,
        )

        if not target_window:
            logger.error(
                f"执行错误：AI 试图向会话 '{target_conversation_uid}' 发送消息，"
                f"但其对应的窗口已不可见。动作已取消。"
            )
            return

        conversation_uid = target_conversation_uid

        # 获取 QQBuilder 和 Session
        if not self.application_manager:
            logger.error("ActionHandler 未能获取到 ApplicationManager 实例。")
            return
        qq_builder = self.application_manager.get_builder_by_name("qq")
        if not qq_builder or not isinstance(qq_builder, IApp):
            logger.error("严重错误：找不到 QQBuilder 或其未实现 IApp 接口。")
            return

        session = await qq_builder.get_session(conversation_uid, container)
        if not session:
            logger.error(f"无法为会话 '{conversation_uid}' 获取 Session 实例。")
            return

        # 使用 MessageBuilder 构建和发送消息
        logger.info(f"正在为会话 '{conversation_uid}' 实例化 MessageBuilder...")
        message_builder = MessageBuilder(session, motivation, self)
        sent_messages_info = await message_builder.process_steps(steps)

        if sent_messages_info:
            logger.info(
                f"总计 {len(sent_messages_info)} 条消息已通过 MessageBuilder "
                f"成功发送至会话 '{target_conversation_uid}'。"
            )
            window_manager.focus_window(target_window.name)

            # 遍历成功发送的消息，并将其作为事件存入数据库
            for action_result, sent_params in sent_messages_info:
                # 1. 构造 sent_dict
                sent_dict = {
                    "platform": session.platform,
                    "bot_id": session.bot_id,
                    "conversation_info": {
                        "type": sent_params["conversation_type"],
                        "conversation_id": sent_params["conversation_id"],
                    },
                    "content": sent_params["content"],
                    "event_type": "action.qq.send_message",  # 这是原始动作类型
                }
                # 2. 构造 metadata
                metadata = ActionMetadata(motivation=motivation)
                # 3. 调用存储方法
                await self._save_successful_action_as_event(
                    action_result.action_id,
                    sent_dict,
                    metadata
                )
        else:
            logger.error(f"通过 MessageBuilder 发送消息至会话 '{conversation_uid}' 失败。")

    async def execute_simple_action(
        self,
        platform_id: str,
        action_name: str,
        params: dict,
        bot_id: str,
        description: str,
        motivation: str | None = None,
    ) -> ActionResult:
        """一个便捷的内部动作执行入口，直接返回 ActionResult."""
        if not self.application_manager:
            logger.error("ActionHandler 未能获取到 ApplicationManager 实例。")
            return ActionResult(
                action_id="", is_success=False, error_message="ApplicationManager 未初始化。"
            )
        builder = self.application_manager.get_builder_by_name(platform_id)
        if not builder:
            return ActionResult(
                action_id="",
                is_success=False,
                error_message=f"找不到平台 '{platform_id}' 的构建器。",
            )

        action_event = builder.build_action_event(action_name, params, bot_id=bot_id)
        if not action_event:
            return ActionResult(
                action_id="", is_success=False, error_message=f"构建动作 '{action_name}' 失败。"
            )

        action_event_dict = action_event.to_dict()
        action_event_dict["platform"] = platform_id

        return await self._execute_platform_action(
            action_to_send=action_event_dict,
            original_action_description=description,
        )

    async def _log_action_attempt(self, action_id: str, action_data: dict[str, Any]) -> None:
        """记录一个动作的尝试."""
        # 从 action_data 中提取 bot_id
        bot_id = "unknown"
        if "bot_id" in action_data:
            bot_id = action_data["bot_id"]
        elif "sender" in action_data and "id" in action_data["sender"]:
            bot_id = action_data["sender"]["id"]

        action_doc = ActionLogDocument(
            _key=action_id,
            action_type=action_data.get("event_type", "unknown"),
            timestamp=int(time.time() * 1000),
            bot_id=str(bot_id),
            status="pending",
            platform=action_data.get("platform", "unknown"),
            action_details=action_data,
        )
        await self.action_log_service.save_action_attempt(action_doc)
        logger.info(f"动作尝试已记录: {action_id}")

    async def _execute_platform_action(
        self,
        action_to_send: dict[str, Any],
        original_action_description: str,
    ) -> ActionResult:
        """底层动作执行器：记录尝试、发送、等待并返回纯净结果."""
        core_action_id = action_to_send.setdefault("event_id", str(uuid.uuid4()))

        await self._log_action_attempt(core_action_id, action_to_send)

        try:
            send_success = await self.action_sender.send_action_to_adapter_by_id(
                action_to_send.get("platform", "unknown"), action_to_send
            )
            if not send_success:
                return ActionResult(
                    action_id=core_action_id,
                    is_success=False,
                    error_message=f"发送到适配器 '{action_to_send.get('platform')}' 失败。",
                )
        except Exception as e:
            return ActionResult(
                action_id=core_action_id,
                is_success=False,
                error_message=f"发送平台动作时发生意外异常: {e}",
            )

        return await self.pending_action_manager.add_and_wait_for_action(
            action_id=core_action_id,
            original_action_description=original_action_description,
        )

    async def _process_action_result(
        self,
        result: ActionResult,
        thought_doc_key: str | None,
        description: str,
        sent_dict: dict,
        metadata: ActionMetadata,
    ) -> None:
        """统一处理 ActionResult 的后续所有流程."""
        # 1. 更新动作日志
        await self.action_log_service.update_action_log_with_response(
            action_id=result.action_id,
            updates={
                "status": "success" if result.is_success else "failed",
                "response_timestamp": int(time.time() * 1000),
                "error_info": result.error_message,
                "result_details": result.payload,
            },
        )

        # 2. 将结果写入思考链
        if thought_doc_key:
            result_message = self._create_final_result_message(description, result)
            await self.thought_storage_service.save_action_result_to_thought(
                thought_key=thought_doc_key, result_text=result_message
            )

        # 3. 如果成功，处理副作用
        if result.is_success:
            await self._handle_successful_action_side_effects(sent_dict, result.payload)
            await self._save_successful_action_as_event(result.action_id, sent_dict, metadata)

    def _create_final_result_message(self, description: str, result: ActionResult) -> str:
        """辅助方法：根据 ActionResult 创建最终的结果消息."""
        if result.is_success:
            msg = f"动作 '{description}' 已成功执行。"
            if result.payload:
                try:
                    payload_str = json.dumps(result.payload, ensure_ascii=False, indent=2)
                    msg += f" 详情: {payload_str}"
                except (TypeError, ValueError):
                    msg += f" 详情: {result.payload!s}"
            return msg
        return f"动作 '{description}' 执行失败: {result.error_message}"

    async def _save_successful_action_as_event(
        self, action_id: str, sent_dict: dict[str, Any], metadata: ActionMetadata
    ) -> None:
        """将成功的动作（通常是send_message）存储为事件."""
        event_to_save = sent_dict.copy()
        event_type_full = event_to_save.get("event_type", "")

        # 只处理 send_message 类型的动作
        if not event_type_full.endswith(".send_message"):
            return

        platform = event_to_save.get("platform", "unknown")
        bot_id = event_to_save.get("bot_id", "unknown")

        # 获取机器人在该平台的完整档案，以构建 user_info
        self_entity = await self.entity_service.get_self_entity_by_platform(platform)
        if self_entity:
            details = self_entity.get("details", {})
            user_info = ProtocolUserInfo(
                user_id=details.get("platform_id", bot_id),
                user_nickname=details.get("nickname", "Bot"),
            )
            event_to_save["user_info"] = user_info.to_dict()
        else:
            # Fallback
            event_to_save["user_info"] = {"user_id": bot_id, "user_nickname": "Bot"}

        # 1. 转换 event_type
        # 从 "action.qq.send_message" 转换为 "message.qq.group" 或 "message.qq.private"
        conv_info = event_to_save.get("conversation_info")
        if conv_info and isinstance(conv_info, dict):
            conv_type = conv_info.get("type", "unknown")
            event_to_save["event_type"] = f"message.{platform}.{conv_type}"

        # 2. 填充/修正关键字段
        event_to_save["event_id"] = action_id
        event_to_save["timestamp"] = int(time.time() * 1000)
        event_to_save["status"] = "read"
        if metadata.motivation and metadata.motivation.strip():
            event_to_save["motivation"] = metadata.motivation

        # 3. 存入数据库
        await self.event_storage_service.save_event_document(event_to_save)
        logger.info(f"成功的发送消息动作 '{action_id}' 已作为事件存入 events 表。")

    async def _handle_successful_action_side_effects(
        self, sent_dict: dict[str, Any], details: dict | None
    ) -> None:
        """处理动作成功后的副作用."""
        original_action_type = sent_dict.get("event_type")
        if not original_action_type:
            return

        if original_action_type.endswith(".get_list"):
            await self._proactively_create_conversation_docs_from_list(details, sent_dict)

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
        if not self.entity_service:
            logger.error("EntityGraphService 未注入到 ActionHandler，无法主动创建会话实体。")
            return
        entity_service = self.entity_service
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
