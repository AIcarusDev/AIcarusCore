# src/action/action_handler.py
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
    """处理所有与动作相关的逻辑，包括动作决策、发送和响应处理.

    这个类负责协调不同的动作提供者，管理动作发送和响应，
    并提供一个统一的接口供其他模块使用.

    Attributes:
        action_llm_client: 用于行动决策的 LLM 客户端.
        summary_llm_client: 用于信息摘要的 LLM 客户端.
        web_search_agent_client: 用于网页搜索的 LLM 客户端.
        action_sender: 动作发送器，用于将动作发送到适配器.
        thought_storage_service: 思维存储服务，用于存储和检索思维文档.
        event_storage_service: 事件存储服务，用于存储和检索事件数据.
        action_log_service: 动作日志存储服务，用于记录动作日志.
        conversation_service: 对话存储服务，用于管理对话数据.
        thought_trigger: 主思维触发器，用于在处理完动作后唤醒主思维.
        pending_action_manager: 管理待处理动作的管理器，处理动作响应和状态跟踪.
        chat_session_manager: 聊天会话管理器，用于管理聊天会话状态
        action_registry: 动作注册表，用于注册和查询可用的动作提供者.
        _background_tasks: 存储所有后台任务的集合，用于管理和清理.
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
        self._background_tasks: set[asyncio.Task] = set()
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
        """设置 ActionHandler 的依赖服务.

        Args:
            thought_service: 思维存储服务实例.
            event_service: 事件存储服务实例.
            action_log_service: 动作日志存储服务实例.
            conversation_service: 对话存储服务实例.
            action_sender: 动作发送器实例.
            chat_session_manager: 聊天会话管理器实例.
            core_logic: 核心逻辑处理器实例.
        """
        self.thought_storage_service = thought_service
        self.action_log_service = action_log_service
        self.action_sender = action_sender
        self.person_service = person_service
        self.chat_session_manager = chat_session_manager
        self.core_logic = core_logic
        self.pending_action_manager = PendingActionManager(
            action_log_service=action_log_service,
            thought_storage_service=thought_service,
            event_storage_service=event_service,
            conversation_service=conversation_service,
        )
        logger.info("ActionHandler 的依赖已成功设置。")

    def set_thought_trigger(self, trigger_event: asyncio.Event | None) -> None:
        """设置主思维触发器."""
        self.thought_trigger = trigger_event
        if trigger_event:
            logger.info("ActionHandler 的主思维触发器已成功设置。")

    async def initialize_llm_clients(self) -> None:
        """按需初始化LLM客户端."""
        # 在新架构下，ActionHandler只依赖于web_search_agent_client
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
        """统一的行动处理流程，负责解析LLM决策并执行.

        现在它不再返回任何值，因为它会自我管理结果的保存和思考的触发.
        """
        logger.info(f"--- [Action ID: {action_id}] 开始处理行动流程 ---")

        try:
            # 1. 检查是否为“不行动”决策
            if "do_nothing" in action_json.get("core", {}):
                motivation = action_json["core"]["do_nothing"].get("motivation", "决定保持沉默")
                logger.info(f"AI 决定不行动，动机: {motivation}")
                if self.thought_storage_service:
                    await self.thought_storage_service.save_action_result_to_thought(
                        thought_key=doc_key_for_updates,
                        result_text=f"决定不行动，原因：{motivation}",
                    )
                return  # 直接结束

            # 2. 解析出需要执行的动作
            # 注意：当前设计依然是一次思考只执行一个平台或核心的第一个动作
            platform_actions = action_json.get("napcat_qq", {})
            core_actions = action_json.get("core", {})
            actions_to_process = platform_actions or {
                k: v for k, v in core_actions.items() if k != "web_search"
            }

            # 如果没有动作需要处理，直接返回
            if not actions_to_process:
                logger.info("AI决策的动作对象为空，无需执行。")
                return
            # 如果有多个动作，取第一个动作作为主要动作
            platform_id = "napcat_qq" if platform_actions else "core"
            action_name, params = next(iter(actions_to_process.items()))

            # 3. 根据动作类型分发执行
            if platform_id == "napcat_qq" and action_name == "send_message":
                await self._execute_send_message_flow(doc_key_for_updates, params)

            else:  # 其他所有平台动作
                await self._execute_platform_action_flow(
                    platform_id, action_name, params, doc_key_for_updates
                )

        finally:
            # 4. 处理完所有动作后，触发思考
            if self.thought_trigger:
                logger.info(f"行动流程处理完毕 (Action ID: {action_id})，触发思考。")
                self.thought_trigger.set()

    def _handle_background_task_completion(self, task: asyncio.Task) -> None:
        """一个通用的回调函数，用于处理所有后台任务的完成事件。
        它会从管理集合中移除任务，并检查任务是否发生了异常。
        """
        self._background_tasks.discard(task)
        if task.exception():
            # 如果任务在执行过程中抛出了异常，我们在这里捕获并记录它
            logger.error(
                f"一个后台任务（名称: '{task.get_name()}'）执行时发生异常: {task.exception()}",
                exc_info=task.exception(),
            )

    async def _execute_send_message_flow(self, doc_key_for_updates: str, params: dict) -> None:
        """专门处理 send_message 流程."""
        from src.action.components.message_builder import MessageBuilder

        # 1. 获取会话信息
        thought_doc = await self.thought_storage_service.get_thought_document_by_key(
            doc_key_for_updates
        )
        if not thought_doc or not (focus_path := thought_doc.get("source_id")):
            logger.error("无法执行 send_message：无法从思想点中获取会话上下文。")
            return

        conv_id = focus_path.split(".")[-1]
        session = self.chat_session_manager.sessions.get(conv_id)
        if not session:
            logger.error(f"无法执行 send_message：找不到会话 '{conv_id}' 的档案。")
            return

        # 2. 创建 MessageBuilder 并直接启动发送流程
        message_builder = MessageBuilder(session, motivation=params.get("motivation"))

        # 3. 直接、纯粹地执行发送任务。
        send_task = asyncio.create_task(
            message_builder.process_steps(params.get("steps", [])),
            name=f"SendMessage-{session.conversation_id}",
        )
        # 将任务添加到后台任务集合中，以便管理和清理
        self._background_tasks.add(send_task)
        send_task.add_done_callback(self._handle_background_task_completion)

        logger.info(f"[{session.conversation_id}] 消息发送流程已提交到后台执行。")

    async def _execute_core_web_search(self, params: dict) -> str:
        """【已重构】执行核心的网页搜索动作，并直接返回结果字符串。"""
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

    async def _execute_platform_action_flow(
        self, platform_id: str, action_name: str, params: dict, doc_key_for_updates: str
    ) -> None:
        """执行一个平台动作的完整流程：构建->发送->等待响应."""
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
            # 可以在这里保存一个失败结果到思想点
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

        # 执行动作并等待，这个方法内部会处理结果的回写
        await self._execute_platform_action(
            action_to_send=action_event.to_dict(),
            thought_doc_key=doc_key_for_updates,
            original_action_description=f"{platform_id}.{action_name}",
        )

    async def execute_simple_action(
        self, platform_id: str, action_name: str, params: dict, bot_id: str, description: str
    ) -> tuple[bool, Any]:
        """一个更简单的动作执行入口，用于内部系统调用，如专注模式."""
        builder = platform_builder_registry.get_builder(platform_id)
        if not builder:
            return False, {"error": f"找不到平台 '{platform_id}' 的翻译官。"}

        # 将 bot_id 传递给 builder
        action_event = builder.build_action_event(action_name, params, bot_id=bot_id)
        if not action_event:
            return False, {"error": f"平台 '{platform_id}' 的翻译官不会翻译动作 '{action_name}'。"}

        success, message_payload = await self._execute_platform_action(
            action_to_send=action_event.to_dict(),
            thought_doc_key=None,
            original_action_description=description,
        )

        # 把 action_id 注入到返回的 payload 中
        if isinstance(message_payload, dict):
            message_payload["action_id"] = action_event.event_id

        return success, message_payload

    async def _execute_platform_action(
        self,
        action_to_send: dict[str, Any],
        thought_doc_key: str | None,
        original_action_description: str,
    ) -> tuple[bool, Any]:
        """底层动作执行器：发送动作到适配器并等待响应."""
        if not self.action_sender or not self.action_log_service or not self.pending_action_manager:
            return False, {"error": "内部错误：核心服务不可用。"}

        event_type = action_to_send.get("event_type", "")
        platform = event_type.split(".")[1] if "." in event_type else "unknown"
        core_action_id = action_to_send.setdefault("event_id", str(uuid.uuid4()))
        timestamp = int(time.time() * 1000)
        action_to_send["timestamp"] = timestamp

        # 确保 bot_id 存在于动作中
        bot_id_for_log = action_to_send.get("bot_id")

        if not bot_id_for_log:
            # 如果真的没有，这是一个严重错误，我们必须记录下来
            logger.error(
                f"严重逻辑错误：动作事件中缺少 bot_id！无法记录日志。事件: {action_to_send}"
            )
            bot_id_for_log = "error_missing_bot_id"  # 在日志中明确记录错误

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
        # 记录动作日志
        try:
            send_success = await self.action_sender.send_action_to_adapter_by_id(
                platform, action_to_send
            )
            if not send_success:
                return False, {"error": f"发送到适配器 '{platform}' 失败。"}
        except Exception as e:
            return False, {"error": f"发送平台动作时发生意外异常: {e}"}

        # 这个方法会阻塞直到收到响应或超时，并处理结果的回写
        success, result_payload = await self.pending_action_manager.add_and_wait_for_action(
            action_id=core_action_id,
            thought_doc_key=thought_doc_key,
            original_action_description=original_action_description,
            action_to_send=action_to_send,
        )

        # 将 action_id 注入
        if isinstance(result_payload, dict):
            result_payload["action_id"] = core_action_id

        return success, result_payload

    async def system_get_bot_profile(self, adapter_id: str) -> None:
        """系统触发获取祂档案的动作，适用于平台适配器."""
        logger.info(f"系统触发为适配器 '{adapter_id}' 获取祂的档案。")
        builder = platform_builder_registry.get_builder(adapter_id)
        if not builder:
            logger.error(f"找不到平台 '{adapter_id}' 的翻译官，无法发起上线安检！")
            return

        action_event = builder.build_action_event(
            action_name="get_bot_profile",
            params={},
            bot_id="pending_inspection",  # 这里用一个特殊的标识表示待安检状态
        )

        if not action_event:
            logger.error(f"平台 '{adapter_id}' 的翻译官不会翻译 get_bot_profile 动作！")
            return

        task = asyncio.create_task(
            self._execute_platform_action(
                action_to_send=action_event.to_dict(),
                thought_doc_key=None,
                original_action_description="系统：上线安检",
            ),
            name=f"BotProfileInspection-{adapter_id}",
        )

        self._background_tasks.add(task)

        task.add_done_callback(self._background_tasks.discard)

        logger.info(f"已通过 ActionHandler 为适配器 '{adapter_id}' 派发档案同步任务。")

    async def submit_constructed_action(
        self,
        action_event_dict: dict[str, Any],
        action_description: str,
        associated_record_key: str | None = None,
    ) -> tuple[bool, str]:
        """直接提交一个已构造好的动作事件，绕过LLM决策."""
        if not self.action_sender or not self.action_log_service:
            critical_error_msg = "核心服务 (ActionSender 或动作日志服务) 未设置!"
            logger.critical(critical_error_msg)
            return False, critical_error_msg

        if "event_id" not in action_event_dict:
            return False, "动作事件缺少 'event_id'"

        success, message_payload = await self._execute_platform_action(
            action_to_send=action_event_dict,
            thought_doc_key=associated_record_key,
            original_action_description=action_description,
        )

        message = ""
        if isinstance(message_payload, dict):
            message = message_payload.get("error") or message_payload.get(
                "message", str(message_payload)
            )
        else:
            message = str(message_payload)

        return success, message
