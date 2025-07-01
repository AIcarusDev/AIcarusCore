# src/action/action_handler.py
import asyncio
import json
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
from src.platform_builders.registry import platform_builder_registry

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
        """ 系统触发获取机器人档案的动作。
        这个方法是为了适配器上线安检，确保机器人档案可用。
        适配器上线时会调用这个方法来获取机器人档案。
        """
        logger.info(f"系统触发为适配器 '{adapter_id}' 获取机器人档案。")

        # 1. 去中介所找翻译官
        builder = platform_builder_registry.get_builder(adapter_id)
        if not builder:
            logger.error(f"找不到平台 '{adapter_id}' 的翻译官，无法发起上线安检！")
            return

        # 2. 告诉翻译官你想干嘛（通用指令）
        intent_data = {
            "full_action_name": "get_bot_profile",
            "params": {}
        }
        # 3. 让翻译官把通用指令翻译成平台事件
        action_event = builder.build_action_event(intent_data)

        if not action_event:
             # 如果这里报错，说明你的 builder 里面根本没有处理 get_bot_profile 的逻辑！
             logger.error(f"平台 '{adapter_id}' 的翻译官不会翻译 get_bot_profile 动作！")
             return

        # 4. 把翻译好的事件丢出去执行
        asyncio.create_task(
            self._execute_platform_action(
                action_to_send=action_event.to_dict(),
                thought_doc_key=None,
                original_action_description="系统：上线安检",
            )
        )
        logger.info(f"已通过 ActionHandler 为适配器 '{adapter_id}' 派发档案同步任务。")

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
        target_adapter_id = platform
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

        # 从“翻译官中介所”获取所有平台的“功能说明书”
        all_platform_schemas = platform_builder_registry.get_all_schemas_for_llm()

        # 别忘了把内部工具的 schema 也加上！
        # 这里的逻辑需要你确保内部工具的 schema 也能被获取到，我们先假设可以
        # internal_tools_schema = self.action_registry.get_provider('internal').get_schema() # 假设有这个方法
        # all_tools_for_llm = all_platform_schemas + internal_tools_schema
        all_tools_for_llm = all_platform_schemas # 先只用平台的

        decision_maker = ActionDecisionMaker(self.action_llm_client)
        # 把“功能说明书”喂给决策者
        decision = await decision_maker.make_decision(
            action_description,
            action_motivation,
            current_thought_context,
            relevant_adapter_messages_context,
            tools_schema=all_tools_for_llm
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

    # _execute_chosen_action 方法需要大改！
    async def _execute_chosen_action(
        self,
        action_id: str,
        doc_key_for_updates: str,
        tool_name_chosen: str,
        tool_arguments: dict,
        action_description: str,
        action_motivation: str,
    ) -> tuple[bool, str, Any]:
        """执行由决策者选择的动作。现在它能区分平台动作和内部工具了。"""
        if not self.thought_storage_service:
            return False, "ThoughtStorageService not initialized", None

        # 1. 判断是平台动作还是内部工具
        if tool_name_chosen.startswith("platform."):
            # 这是平台动作！
            parts = tool_name_chosen.split('.', 2)
            if len(parts) < 3:
                err_msg = f"平台动作名称 '{tool_name_chosen}' 格式不正确。"
                logger.error(err_msg)
                return False, err_msg, None

            platform_id = parts[1]
            # action_type = parts[2] # 我们不再需要这个了

            # 去找对应的“翻译官”
            builder = platform_builder_registry.get_builder(platform_id)
            if not builder:
                err_msg = f"找不到平台 '{platform_id}' 的翻译官来执行动作。"
                logger.error(err_msg)
                return False, err_msg, None

            # 构造通用的意图数据
            intent_data = {
                "full_action_name": tool_name_chosen, # 把完整的动作名传进去
                "params": tool_arguments
            }

            # 让“翻译官”把通用指令翻译成平台事件
            action_event_to_send = builder.build_action_event(intent_data)

            if not action_event_to_send:
                err_msg = f"平台 '{platform_id}' 的翻译官不会翻译动作 '{tool_name_chosen}'。"
                logger.error(err_msg)
                return False, err_msg, None

            # 执行平台动作
            was_successful, result_payload = await self._execute_platform_action(
                action_to_send=action_event_to_send.to_dict(),
                thought_doc_key=doc_key_for_updates,
                original_action_description=action_description,
            )
            final_result = f"平台动作 '{tool_name_chosen}' 已提交。" if was_successful else (result_payload.get("error", "未知平台错误") if isinstance(result_payload, dict) else str(result_payload))
            return was_successful, final_result, result_payload

        elif tool_name_chosen.startswith("internal."): # 明确判断内部工具
            internal_action_name = tool_name_chosen.split('.', 1)[1]
            action_func = self.action_registry.get_action(internal_action_name)
            if not action_func:
                final_result = f"未在注册表中找到名为 '{internal_action_name}' 的内部工具。"
                await self.thought_storage_service.update_action_status_in_thought_document(
                    doc_key_for_updates, action_id, {"status": "TOOL_NOT_FOUND", "error_message": final_result, "final_result_for_shimo": final_result}
                )
                return False, final_result, None

            logger.info(f"从注册表找到内部工具 '{internal_action_name}'，准备执行。")
            try:
                # 把一些通用的上下文也传给内部工具，万一它需要呢
                tool_arguments['action_description'] = action_description
                tool_arguments['action_motivation'] = action_motivation

                tool_result_data = await action_func(**tool_arguments)

                if tool_name_chosen == "search_web" and tool_result_data and self.summary_llm_client:
                    summarizer = ToolResultSummarizer(self.summary_llm_client)
                    final_result = await summarizer.summarize(
                        original_query=tool_arguments.get("query", action_description),
                        original_motivation=action_motivation,
                        tool_output=tool_result_data,
                    )
                elif tool_result_data:
                    result_str = json.dumps(tool_result_data, ensure_ascii=False, indent=2)
                    final_result = f"工具 '{tool_name_chosen}' 执行成功，返回了以下数据：\n```json\n{result_str}\n```"
                else:
                    final_result = f"工具 '{tool_name_chosen}' 执行成功，但没有返回任何数据。"

                await self.thought_storage_service.update_action_status_in_thought_document(
                    doc_key_for_updates, action_id, {"status": "COMPLETED_SUCCESS", "final_result_for_shimo": final_result}
                )
                return True, final_result, tool_result_data
            except Exception as e_exec:
                final_result = f"执行内部工具 '{tool_name_chosen}' 时出错: {e_exec}"
                logger.error(final_result, exc_info=True)
                await self.thought_storage_service.update_action_status_in_thought_document(
                    doc_key_for_updates, action_id, {"status": "EXECUTION_ERROR", "error_message": final_result, "final_result_for_shimo": final_result}
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
