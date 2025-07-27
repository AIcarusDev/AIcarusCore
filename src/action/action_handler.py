# 文件: src/action/action_handler.py (竞速模式适配版 V1.0)
import asyncio
import time
import uuid
from typing import TYPE_CHECKING, Any

from src.action.components.pending_action_manager import PendingActionManager
from src.common.custom_logging.logging_config import get_logger
from src.config import config
from src.core_communication.action_sender import ActionSender
from src.database import (
    ActionLogStorageService,
    ConversationStorageService,
    EventStorageService,
    PersonStorageService,
    ThoughtStorageService,
)
from src.llmrequest.llm_processor import Client as ProcessorClient
from src.platform_builders.registry import platform_builder_registry
from src.prompt_templates.web_search import WEB_SEARCH_SYSTEM_PROMPT, WEB_SEARCH_USER_PROMPT

if TYPE_CHECKING:
    from src.core_logic.consciousness_flow import CoreLogic
    from src.focus_chat_mode.chat_session_manager import ChatSessionManager

logger = get_logger(__name__)
ACTION_RESPONSE_TIMEOUT_SECONDS = 30


class ActionHandler:
    """处理所有与动作相关的逻辑.

    它现在是一个纯粹的动作执行器，不再负责触发思考循环.
    """

    def __init__(self) -> None:
        self.web_search_agent_client: ProcessorClient | None = None
        self.action_sender: ActionSender | None = None
        self.thought_storage_service: ThoughtStorageService | None = None
        self.action_log_service: ActionLogStorageService | None = None
        self.thought_trigger: asyncio.Event | None = None
        self.pending_action_manager: PendingActionManager | None = None
        self.chat_session_manager: ChatSessionManager | None = None
        self.core_logic: CoreLogic | None = None
        self.person_service: PersonStorageService | None = None
        logger.info(f"{self.__class__.__name__} instance created.")

    def set_dependencies(
        self,
        thought_service: ThoughtStorageService,
        event_service: EventStorageService,
        action_log_service: ActionLogStorageService,
        conversation_service: ConversationStorageService,
        action_sender: ActionSender,
        person_service: PersonStorageService,
        chat_session_manager: "ChatSessionManager",
        core_logic: "CoreLogic",
    ) -> None:
        """设置 ActionHandler 的依赖服务."""
        self.thought_storage_service = thought_service
        self.action_log_service = action_log_service
        self.action_sender = action_sender
        self.person_service = person_service
        self.chat_session_manager = chat_session_manager
        self.core_logic = core_logic
        # 关键：将 ActionHandler 自身的实例传递给 PendingActionManager
        self.pending_action_manager = PendingActionManager(
            action_log_service=action_log_service,
            thought_storage_service=thought_service,
            event_storage_service=event_service,
            conversation_service=conversation_service,
            action_handler_instance=self,  # 把自己传进去
        )
        logger.info("ActionHandler 的依赖已成功设置。")

    def set_thought_trigger(self, trigger_event: asyncio.Event | None) -> None:
        """设置主思维触发器 (在竞速模式下，此触发器主要由CoreLogic自身管理)."""
        self.thought_trigger = trigger_event
        if trigger_event:
            logger.info("ActionHandler 的主思维触发器已成功设置。")

    async def initialize_llm_clients(self) -> None:
        """按需初始化LLM客户端."""
        if self.web_search_agent_client:
            return
        from src.action.components.llm_client_factory import LLMClientFactory

        factory = LLMClientFactory()
        try:
            self.web_search_agent_client = factory.create_client(purpose_key="web_search_agent")
            logger.info("ActionHandler 的 web_search_agent_client 初始化成功。")
        except RuntimeError as e:
            logger.critical(f"为 ActionHandler 初始化LLM客户端失败: {e}")
            raise

    async def handle_action_response(self, response_event_data: dict[str, Any]) -> None:
        """处理来自适配器的动作响应."""
        if self.pending_action_manager:
            await self.pending_action_manager.handle_response(response_event_data)
        else:
            logger.error("PendingActionManager 未初始化，无法处理动作响应。")

    async def process_action_flow(
        self,
        action_id: str,
        doc_key_for_updates: str,
        action_json: dict[str, Any],
    ) -> None:
        """统一的行动处理流程.

        它现在不再触发思考，只负责执行动作并将结果写回思想点.
        """
        logger.info(f"[探灯B] ActionHandler 收到的 action_json: {action_json}")
        logger.info(f"--- [Action ID: {action_id}] 开始处理行动流程 ---")

        # 1. 检查是否为“不行动”决策
        if "do_nothing" in action_json.get("core", {}):
            motivation = action_json["core"]["do_nothing"].get("motivation", "决定保持沉默")
            logger.info(f"AI 决定不行动，动机: {motivation}")
            if self.thought_storage_service:
                await self.thought_storage_service.save_action_result_to_thought(
                    thought_key=doc_key_for_updates,
                    result_text=f"决定不行动，原因：{motivation}",
                )
            return

        # 2. 动态解析出需要执行的动作
        # 我们不再写死平台名，而是动态地查找
        core_actions = action_json.get("core", {})
        # 找到第一个不是'core'的键和值，作为平台动作
        platform_actions_tuple = next(
            ((key, value) for key, value in action_json.items() if key != "core"),
            (None, None),
        )
        platform_id_from_action, platform_actions = platform_actions_tuple

        actions_to_process = platform_actions or core_actions

        if not actions_to_process:
            logger.info("AI决策的动作对象为空，无需执行。")
            return

        platform_id = platform_id_from_action if platform_actions else "core"
        action_name, params = next(iter(actions_to_process.items()))

        if platform_id == "core" and action_name == "web_search":
            # 调用内部的“智能搜索代理”方法
            logger.info("检测到 web_search 动作，正在激活智能搜索代理...")
            result_text = await self._execute_core_web_search(params)

            # 将代理返回的高信息密度结果写回到思想点
            if self.thought_storage_service:
                await self.thought_storage_service.save_action_result_to_thought(
                    thought_key=doc_key_for_updates,
                    result_text=result_text,
                )

            # 搜索完成后，立即触发思考，让AI能够处理结果！
            if self.thought_trigger:
                logger.info(f"智能搜索代理完成任务 (Action ID: {action_id})，立即触发新一轮思考。")
                self.thought_trigger.set()

        # --- END: 修改结束 ---
        else:
            await self._execute_platform_action_flow(
                platform_id, action_name, params, doc_key_for_updates
            )

    async def _execute_core_web_search(self, params: dict) -> str:
        """执行核心的网页搜索动作，并直接返回结果字符串."""
        await self.initialize_llm_clients()
        query = params.get("query")
        motivation = params.get("motivation", "没有明确动机")

        if not query or not self.web_search_agent_client:
            result_text = "动作执行失败：LLM想搜索但没提供关键词，或者搜索代理客户端未初始化。"
            logger.warning(result_text)
            return result_text

        logger.info(f"正在调用搜索代理LLM，查询: '{query}'")
        system_prompt = WEB_SEARCH_SYSTEM_PROMPT.format(bot_name=config.persona.bot_name)
        user_prompt = WEB_SEARCH_USER_PROMPT.format(query=query, motivation=motivation)
        response = await self.web_search_agent_client.make_llm_request(
            prompt=user_prompt, system_prompt=system_prompt, is_stream=False, use_google_search=True
        )
        return response.get("text", "搜索失败或未返回任何信息。")

        # 4. 【移除】不再从此触发思考
        # if self.thought_trigger:
        #     logger.info(f"行动流程处理完毕 (Action ID: {action_id})，触发思考。")
        #     self.thought_trigger.set()

    async def _execute_platform_action_flow(
        self, platform_id: str, action_name: str, params: dict, doc_key_for_updates: str
    ) -> None:
        """执行一个平台动作的完整流程：构建->发送->等待响应."""
        if not self.action_sender or platform_id not in self.action_sender.connected_adapters:
            error_msg = f"动作执行失败：平台 '{platform_id}' 理论上存在，但当前未连接。"
            logger.error(error_msg)
            if self.thought_storage_service:
                await self.thought_storage_service.save_action_result_to_thought(
                    thought_key=doc_key_for_updates,
                    result_text=error_msg,
                )
            return

        builder = platform_builder_registry.get_builder(platform_id)
        if not builder:
            logger.error(f"找不到平台 '{platform_id}' 的翻译官。")
            return

        if not self.person_service:
            logger.error("PersonStorageService 未注入到 ActionHandler，无法获取祂的ID！")
            return

        self_account = await self.person_service.get_self_account_for_platform(platform_id)
        if not self_account or not self_account.get("platform_id"):
            logger.error(f"无法为平台 '{platform_id}' 获取已安检的祂的ID。动作无法执行。")
            await self.thought_storage_service.save_action_result_to_thought(
                thought_key=doc_key_for_updates,
                result_text=f"动作执行失败：我找不到自己在这个平台({platform_id})上的身份信息。",
            )
            return

        correct_bot_id = self_account["platform_id"]
        action_event = builder.build_action_event(action_name, params, bot_id=correct_bot_id)
        if not action_event:
            logger.error(f"平台 '{platform_id}' 的翻译官不会翻译动作 '{action_name}'。")
            return

        await self._execute_platform_action(
            action_to_send=action_event.to_dict(),
            thought_doc_key=doc_key_for_updates,
            original_action_description=f"{platform_id}.{action_name}",
        )

    async def execute_simple_action(
        self,
        platform_id: str,
        action_name: str,
        params: dict,
        bot_id: str,
        description: str,
        motivation: str | None = None,
    ) -> tuple[bool, Any]:
        """一个更简单的动作执行入口，供 MessageBuilder 等内部系统调用.

        它会返回执行结果和包含 action_id 的 payload.
        """
        builder = platform_builder_registry.get_builder(platform_id)
        if not builder:
            return False, {"error": f"找不到平台 '{platform_id}' 的翻译官。"}

        action_event = builder.build_action_event(action_name, params, bot_id=bot_id)
        if not action_event:
            return False, {"error": f"平台 '{platform_id}' 的翻译官不会翻译动作 '{action_name}'。"}

        success, message_payload = await self._execute_platform_action(
            action_to_send=action_event.to_dict(),
            thought_doc_key=None,
            original_action_description=description,
            motivation=motivation,
        )

        if isinstance(message_payload, dict):
            message_payload["action_id"] = action_event.event_id

        return success, message_payload

    async def _execute_platform_action(
        self,
        action_to_send: dict[str, Any],
        thought_doc_key: str | None,
        original_action_description: str,
        motivation: str | None = None,
    ) -> tuple[bool, Any]:
        """底层动作执行器：发送动作到适配器并等待响应."""
        if not self.action_sender or not self.action_log_service or not self.pending_action_manager:
            return False, {"error": "内部错误：核心服务不可用。"}

        event_type = action_to_send.get("event_type", "")
        platform = event_type.split(".")[1] if "." in event_type else "unknown"
        core_action_id = action_to_send.setdefault("event_id", str(uuid.uuid4()))
        timestamp = int(time.time() * 1000)
        action_to_send["timestamp"] = timestamp
        bot_id_for_log = action_to_send.get("bot_id")
        if not bot_id_for_log:
            logger.error(
                f"严重逻辑错误：动作事件中缺少 bot_id！无法记录日志。事件: {action_to_send}"
            )
            bot_id_for_log = "error_missing_bot_id"

        await self.action_log_service.save_action_attempt(
            action_id=core_action_id,
            action_type=event_type,
            timestamp=timestamp,
            bot_id=bot_id_for_log,
            platform=platform,
            conversation_id=action_to_send.get("conversation_info", {}).get(
                "conversation_id", "unknown_conv_id"
            ),
            content=action_to_send.get("content", []),
        )
        try:
            send_success = await self.action_sender.send_action_to_adapter_by_id(
                platform, action_to_send
            )
            if not send_success:
                return False, {"error": f"发送到适配器 '{platform}' 失败。"}
        except Exception as e:
            return False, {"error": f"发送平台动作时发生意外异常: {e}"}

        success, result_payload = await self.pending_action_manager.add_and_wait_for_action(
            action_id=core_action_id,
            thought_doc_key=thought_doc_key,
            original_action_description=original_action_description,
            action_to_send=action_to_send,
            motivation=motivation,
        )

        if isinstance(result_payload, dict):
            result_payload["action_id"] = core_action_id

        return success, result_payload
