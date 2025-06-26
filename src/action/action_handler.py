# src/action/action_handler.py
import asyncio
import json  # 确保导入 json
import time
import uuid
from typing import TYPE_CHECKING, Any

from src.action.action_provider import ActionProvider
from src.action.components.action_decision_maker import ActionDecisionMaker
from src.action.components.action_registry import ActionRegistry
from src.action.components.llm_client_factory import LLMClientFactory
from src.action.components.pending_action_manager import PendingActionManager
from src.action.components.tool_result_summarizer import ToolResultSummarizer
from src.common.custom_logging.logging_config import get_logger
from src.config import config
from src.core_communication.action_sender import ActionSender
from src.database.services.action_log_storage_service import ActionLogStorageService
from src.database.services.conversation_storage_service import ConversationStorageService
from src.database.services.event_storage_service import EventStorageService
from src.database.services.thought_storage_service import ThoughtStorageService
from src.llmrequest.llm_processor import Client as ProcessorClient

if TYPE_CHECKING:
    pass
logger = get_logger(__name__)
ACTION_RESPONSE_TIMEOUT_SECONDS = 30


class ActionHandler:
    """
    负责编排AI的行动决策流程（重构版）：
    1.  管理一个动作注册表，从不同的 ActionProvider 加载动作。
    2.  调用LLM进行工具选择。
    3.  从注册表中查找并执行选择的动作（无论是内部工具还是平台动作）。
    4.  处理动作的执行、响应、超时和日志记录。
    """

    def __init__(self) -> None:
        self.action_llm_client: ProcessorClient | None = None
        self.summary_llm_client: ProcessorClient | None = None
        self.action_sender: ActionSender | None = None
        self.thought_storage_service: ThoughtStorageService | None = None
        self.event_storage_service: EventStorageService | None = None
        self.action_log_service: ActionLogStorageService | None = None
        self.conversation_service: ConversationStorageService | None = None
        self.thought_trigger: asyncio.Event | None = None
        self.pending_action_manager: PendingActionManager | None = None
        self.action_registry = ActionRegistry()
        logger.info(f"{self.__class__.__name__} instance created.")

    def set_dependencies(
        self,
        thought_service: ThoughtStorageService,
        event_service: EventStorageService,
        action_log_service: ActionLogStorageService,
        conversation_service: ConversationStorageService,
        action_sender: ActionSender,
    ) -> None:
        self.thought_storage_service = thought_service
        self.event_storage_service = event_service
        self.action_log_service = action_log_service
        self.conversation_service = conversation_service
        self.action_sender = action_sender

        self.pending_action_manager = PendingActionManager(
            action_log_service=action_log_service,
            thought_storage_service=thought_service,
            event_storage_service=event_service,
            conversation_service=conversation_service,
        )
        logger.info("ActionHandler 的依赖已成功设置，PendingActionManager 已创建。")

    def register_provider(self, provider: ActionProvider) -> None:
        """将动作提供者注册到 ActionRegistry。"""
        self.action_registry.register_provider(provider)

    def set_thought_trigger(self, trigger_event: asyncio.Event | None) -> None:
        if trigger_event is not None and not isinstance(trigger_event, asyncio.Event):
            logger.error(f"set_thought_trigger 收到一个无效的事件类型: {type(trigger_event)}。")
            self.thought_trigger = None
            return
        self.thought_trigger = trigger_event
        if trigger_event:
            logger.info("ActionHandler 的主思维触发器已成功设置。")

    async def initialize_llm_clients(self) -> None:
        if self.action_llm_client and self.summary_llm_client:
            logger.debug("ActionHandler 的 LLM 客户端均已存在，无需重新初始化。")
            return

        logger.info("正在为行动处理模块按需初始化LLM客户端...")
        factory = LLMClientFactory()
        try:
            if not self.action_llm_client:
                self.action_llm_client = factory.create_client(purpose_key="action_decision")
                logger.info("ActionHandler: action_llm_client 已初始化。")
            if not self.summary_llm_client:
                self.summary_llm_client = factory.create_client(purpose_key="information_summary")
                logger.info("ActionHandler: summary_llm_client 已初始化。")
            logger.info("行动处理模块的LLM客户端按需初始化检查完成。")
        except RuntimeError as e:
            logger.critical(f"为 ActionHandler 初始化LLM客户端失败: {e}")
            raise

    async def handle_action_response(self, response_event_data: dict[str, Any]) -> None:
        """将动作响应事件委托给 PendingActionManager 处理。"""
        if self.pending_action_manager:
            await self.pending_action_manager.handle_response(response_event_data)
        else:
            logger.error("PendingActionManager 未初始化，无法处理动作响应。")

    async def system_get_bot_profile(self, adapter_id: str) -> None:
        """
        由系统（如WebSocket服务器）调用的，用于触发机器人档案获取的特定方法。
        它不返回结果，只负责发送动作并让 PendingActionManager 等待。
        """
        logger.info(f"系统触发为适配器 '{adapter_id}' 获取机器人档案。")
        action_event = {
            "event_id": f"core_get_profile_{adapter_id}_{uuid.uuid4().hex[:6]}",
            "event_type": "action.bot.get_profile",
            "platform": adapter_id,
            "bot_id": config.persona.bot_name,
            "content": [{"type": "action.bot.get_profile", "data": {}}],
        }

        # 我们调用 _execute_platform_action，因为它会正确地在 PendingActionManager 中注册等待！
        # 我们不关心它的返回值，因为它会自己处理超时和响应。
        # 我们用 asyncio.create_task 把它丢到后台去执行，不阻塞当前任务。
        asyncio.create_task(
            self._execute_platform_action(
                action_to_send=action_event,
                thought_doc_key=None,  # 系统级动作没有关联的思考文档
                original_action_description="系统：上线安检",
            )
        )
        logger.info(f"已为适配器 '{adapter_id}' 创建并派发“上线安检”任务。")

    async def _execute_platform_action(
        self,
        action_to_send: dict[str, Any],
        thought_doc_key: str | None,
        original_action_description: str,
    ) -> tuple[bool, Any]:
        if not self.action_sender or not self.action_log_service or not self.pending_action_manager:
            logger.error("核心服务 (ActionSender, ActionLogService, or PendingActionManager) 未设置!")
            return False, {"error": "内部错误：核心服务不可用。"}

        is_direct_reply_action = original_action_description in [
            "回复主人",
            "发送专注模式回复",
            "internal_tool_call",
            "系统：上线安检",
        ]
        if not is_direct_reply_action and not thought_doc_key:
            logger.error(f"严重错误：动作 '{original_action_description}' 缺少 thought_doc_key。")
            return False, {"error": "内部错误：执行动作缺少必要的思考文档关联。"}

        core_action_id = action_to_send.setdefault("event_id", str(uuid.uuid4()))
        action_type = action_to_send.get("event_type", "unknown_action_type")
        platform = action_to_send.get("platform", "unknown_platform")
        target_adapter_id = "master_ui_adapter" if platform == "master_ui" else platform
        timestamp = int(time.time() * 1000)
        action_to_send["timestamp"] = timestamp

        await self.action_log_service.save_action_attempt(
            action_id=core_action_id,
            action_type=action_type,
            timestamp=timestamp,
            platform=platform,
            bot_id=action_to_send.get("bot_id", config.persona.bot_name),
            conversation_id=action_to_send.get("conversation_info", {}).get("conversation_id", "unknown_conv_id"),
            content=action_to_send.get("content", []),
        )

        if not is_direct_reply_action and self.thought_storage_service and thought_doc_key:
            await self.thought_storage_service.update_action_status_in_thought_document(
                thought_doc_key,
                core_action_id,
                {"status": "EXECUTING_AWAITING_RESPONSE", "sent_to_adapter_at": timestamp},
            )

        try:
            send_success = await self.action_sender.send_action_to_adapter_by_id(target_adapter_id, action_to_send)
            if not send_success:
                err_msg = f"发送到适配器 '{target_adapter_id}' 失败。"
                logger.error(err_msg)
                return False, {"error": err_msg}
        except Exception as e:
            err_msg = f"发送平台动作时发生意外异常: {e}"
            logger.error(err_msg, exc_info=True)
            return False, {"error": err_msg}

        return await self.pending_action_manager.add_and_wait_for_action(
            action_id=core_action_id,
            thought_doc_key=thought_doc_key,
            original_action_description=original_action_description,
            action_to_send=action_to_send,
        )

    async def process_action_flow(
        self,
        action_id: str,
        doc_key_for_updates: str,
        action_description: str,
        action_motivation: str,
        current_thought_context: str,
        relevant_adapter_messages_context: str = "无相关外部消息或请求。",
    ) -> tuple[bool, str, Any]:
        logger.info(f"--- [Action ID: {action_id}, DocKey: {doc_key_for_updates}] 开始处理行动流程 ---")

        # 强制检查并初始化LLM客户端
        await self.initialize_llm_clients()

        if not self.thought_storage_service or not self.action_llm_client or not self.summary_llm_client:
            error_msg = "核心服务 (ThoughtStorageService, ActionLLMClient, 或 SummaryLLMClient) 未初始化。"
            logger.error(error_msg)
            return False, error_msg, None

        decision_maker = ActionDecisionMaker(self.action_llm_client)
        decision = await decision_maker.make_decision(
            action_description,
            action_motivation,
            current_thought_context,
            relevant_adapter_messages_context,
        )

        final_result_for_shimo: str = f"尝试执行动作 '{action_description}' 时出现未知的处理错误。"
        action_was_successful: bool = False
        action_result_payload: Any = None

        if decision.error:
            final_result_for_shimo = decision.error
            await self.thought_storage_service.update_action_status_in_thought_document(
                doc_key_for_updates,
                action_id,
                {
                    "status": "LLM_DECISION_ERROR",
                    "error_message": final_result_for_shimo,
                    "final_result_for_shimo": final_result_for_shimo,
                },
            )
            return False, final_result_for_shimo, None

        tool_name_chosen = decision.tool_to_use
        tool_arguments = decision.arguments

        if not tool_name_chosen:
            final_result_for_shimo = "AI决策不使用任何工具。"
            action_was_successful = True
            await self.thought_storage_service.update_action_status_in_thought_document(
                doc_key_for_updates,
                action_id,
                {"status": "COMPLETED_NO_TOOL", "final_result_for_shimo": final_result_for_shimo},
            )
        else:
            action_was_successful, final_result_for_shimo, action_result_payload = await self._execute_chosen_action(
                action_id=action_id,
                doc_key_for_updates=doc_key_for_updates,
                tool_name_chosen=tool_name_chosen,
                tool_arguments=tool_arguments,
                action_description=action_description,
                action_motivation=action_motivation,
            )

        if self.thought_trigger:
            self.thought_trigger.set()

        return action_was_successful, final_result_for_shimo, action_result_payload

    async def _execute_chosen_action(
        self,
        action_id: str,
        doc_key_for_updates: str,
        tool_name_chosen: str,
        tool_arguments: dict,
        action_description: str,
        action_motivation: str,
    ) -> tuple[bool, str, Any]:
        """执行由决策者选择的动作。"""
        action_func = self.action_registry.get_action(tool_name_chosen)
        if not self.thought_storage_service:
            return False, "ThoughtStorageService not initialized", None

        if not action_func:
            final_result = f"未在注册表中找到名为 '{tool_name_chosen}' 的动作。"
            await self.thought_storage_service.update_action_status_in_thought_document(
                doc_key_for_updates,
                action_id,
                {"status": "TOOL_NOT_FOUND", "error_message": final_result, "final_result_for_shimo": final_result},
            )
            return False, final_result, None

        logger.info(f"从注册表找到动作 '{tool_name_chosen}'，准备执行。")
        try:
            if tool_name_chosen.startswith("platform."):
                tool_arguments["thought_doc_key"] = doc_key_for_updates
                tool_arguments["original_action_description"] = action_description
                was_successful, result_payload = await action_func(**tool_arguments)
                final_result = (
                    result_payload.get("error") if not was_successful else f"平台动作 '{tool_name_chosen}' 已提交。"
                )
                return was_successful, final_result, result_payload
            else:  # Internal tool
                tool_result_data = await action_func(**tool_arguments)

                # 诊断日志
                logger.info(
                    f"【诊断】准备进行摘要判断。summary_llm_client 是否存在: {bool(self.summary_llm_client)}"
                )
                if self.summary_llm_client:
                    logger.info(f"【诊断】summary_llm_client 实例类型: {type(self.summary_llm_client)}")

                # 如果是网络搜索，并且能摘要，那就摘要它
                if tool_name_chosen == "search_web" and tool_result_data and self.summary_llm_client:
                    logger.info(f"工具 '{tool_name_chosen}' 返回了数据，将尝试进行摘要。")
                    summarizer = ToolResultSummarizer(self.summary_llm_client)
                    final_result = await summarizer.summarize(
                        original_query=tool_arguments.get("query", action_description),
                        original_motivation=action_motivation,
                        tool_output=tool_result_data,
                    )
                # 如果是别的工具，或者摘要失败了，但它返回了数据
                elif tool_result_data:
                    logger.info(f"工具 '{tool_name_chosen}' 返回了原始数据，将直接格式化后使用。")
                    try:
                        result_str = json.dumps(tool_result_data, ensure_ascii=False, indent=2)
                        final_result = (
                            f"工具 '{tool_name_chosen}' 执行成功，返回了以下数据：\n```json\n{result_str}\n```"
                        )
                    except TypeError:
                        final_result = f"工具 '{tool_name_chosen}' 执行成功，返回数据：{str(tool_result_data)}"
                # 如果工具执行完就完了，啥也没返回
                else:
                    final_result = f"工具 '{tool_name_chosen}' 执行成功，但没有返回任何数据。"

                await self.thought_storage_service.update_action_status_in_thought_document(
                    doc_key_for_updates,
                    action_id,
                    {"status": "COMPLETED_SUCCESS", "final_result_for_shimo": final_result},
                )
                return True, final_result, tool_result_data
        except Exception as e_exec:
            final_result = f"执行动作 '{tool_name_chosen}' 时出错: {e_exec}"
            logger.error(final_result, exc_info=True)
            await self.thought_storage_service.update_action_status_in_thought_document(
                doc_key_for_updates,
                action_id,
                {"status": "EXECUTION_ERROR", "error_message": final_result, "final_result_for_shimo": final_result},
            )
            return False, final_result, None

    async def send_action_and_wait_for_response(
        self, action_event_dict: dict[str, Any], timeout: int = ACTION_RESPONSE_TIMEOUT_SECONDS
    ) -> tuple[bool, dict[str, Any] | None]:
        """
        一个公共方法，用于发送一个动作并等待其响应。
        这对于需要从适配器获取数据的内部工具非常有用。
        """
        # 确保核心服务已设置
        if not self.pending_action_manager:
            logger.error("PendingActionManager 未初始化，无法发送和等待动作。")
            return False, {"error": "PendingActionManager is not initialized."}

        # 确保动作有平台ID
        adapter_id = action_event_dict.get("platform")
        if not adapter_id:
            return False, {"error": "Action event must contain a 'platform' key."}

        # 确保动作有ID
        _action_id = action_event_dict.setdefault("event_id", str(uuid.uuid4()))

        # 使用 _execute_platform_action，但传入内部调用的标记
        # 注意：对于内部工具调用，我们不传递 thought_doc_key
        return await self._execute_platform_action(
            action_to_send=action_event_dict,
            thought_doc_key=None,
            original_action_description="internal_tool_call",
        )

    async def submit_constructed_action(
        self, action_event_dict: dict[str, Any], action_description: str, associated_record_key: str | None = None
    ) -> tuple[bool, str]:
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
            message = message_payload.get("error") or message_payload.get("message", str(message_payload))
        else:
            message = str(message_payload)

        return success, message
