# src/action/action_handler.py (小色猫·女王修复最终版)
import asyncio
import json
import time
import uuid
from typing import TYPE_CHECKING, Any

# 导入我们的小玩具挂钩和它的提供者！
from src.action.action_provider import ActionProvider
from src.action.components.action_registry import ActionRegistry
from src.action.components.llm_client_factory import LLMClientFactory
from src.action.components.pending_action_manager import PendingActionManager
from src.action.components.tool_result_summarizer import ToolResultSummarizer
from src.common.custom_logging.logging_config import get_logger
from src.config import config
from src.core_communication.action_sender import ActionSender
from src.database import ActionLogStorageService, ConversationStorageService, EventStorageService, ThoughtStorageService
from src.llmrequest.llm_processor import Client as ProcessorClient
from src.platform_builders.registry import platform_builder_registry

if TYPE_CHECKING:
    pass

logger = get_logger(__name__)
ACTION_RESPONSE_TIMEOUT_SECONDS = 30


class ActionHandler:
    """
    行动女王 (V6.0 修复版)！
    我现在既能动态玩弄所有平台，也能佩戴我的内部小玩具了，哼！
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

        # --- ❤❤❤ 看！我把我的小玩具挂钩(ActionRegistry)装回来了！❤❤❤ ---
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

    # --- ❤❤❤ register_provider 方法也回来了！现在 main.py 不会再对我尖叫了！❤❤❤ ---
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
            return
        logger.info("正在为行动处理模块按需初始化LLM客户端...")
        factory = LLMClientFactory()
        try:
            if not self.action_llm_client:
                self.action_llm_client = factory.create_client(purpose_key="action_decision")
            if not self.summary_llm_client:
                self.summary_llm_client = factory.create_client(purpose_key="information_summary")
        except RuntimeError as e:
            logger.critical(f"为 ActionHandler 初始化LLM客户端失败: {e}")
            raise

    async def handle_action_response(self, response_event_data: dict[str, Any]) -> None:
        if self.pending_action_manager:
            await self.pending_action_manager.handle_response(response_event_data)
        else:
            logger.error("PendingActionManager 未初始化，无法处理动作响应。")

    async def system_get_bot_profile(self, adapter_id: str) -> None:
        logger.info(f"系统触发为适配器 '{adapter_id}' 获取机器人档案。")
        builder = platform_builder_registry.get_builder(adapter_id)
        if not builder:
            logger.error(f"找不到平台 '{adapter_id}' 的翻译官，无法发起上线安检！")
            return

        action_event = builder.build_action_event(action_name="get_bot_profile", params={})

        if not action_event:
            logger.error(f"平台 '{adapter_id}' 的翻译官不会翻译 get_bot_profile 动作！")
            return

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
            return False, {"error": "内部错误：核心服务不可用。"}

        event_type = action_to_send.get("event_type", "")
        parts = event_type.split(".")
        platform = parts[1] if len(parts) > 1 else "unknown_platform"

        core_action_id = action_to_send.setdefault("event_id", str(uuid.uuid4()))
        timestamp = int(time.time() * 1000)
        action_to_send["timestamp"] = timestamp

        await self.action_log_service.save_action_attempt(
            action_id=core_action_id,
            action_type=event_type,
            timestamp=timestamp,
            platform=platform,
            bot_id=action_to_send.get("bot_id", config.persona.bot_name),
            conversation_id=action_to_send.get("conversation_info", {}).get("conversation_id", "unknown_conv_id"),
            content=action_to_send.get("content", []),
        )

        is_direct_reply_action = original_action_description in [
            "发送专注模式回复",
            "internal_tool_call",
            "系统：上线安检",
        ]
        if not is_direct_reply_action and self.thought_storage_service and thought_doc_key:
            await self.thought_storage_service.update_action_status_in_thought_document(
                thought_doc_key,
                core_action_id,
                {"status": "EXECUTING_AWAITING_RESPONSE", "sent_to_adapter_at": timestamp},
            )

        try:
            send_success = await self.action_sender.send_action_to_adapter_by_id(platform, action_to_send)
            if not send_success:
                return False, {"error": f"发送到适配器 '{platform}' 失败。"}
        except Exception as e:
            return False, {"error": f"发送平台动作时发生意外异常: {e}"}

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
        logger.info(f"--- [Action ID: {action_id}] 女王开始处理行动流程 ---")
        await self.initialize_llm_clients()

        if not self.thought_storage_service or not self.action_llm_client or not self.summary_llm_client:
            error_msg = "核心服务 (ThoughtStorageService, ActionLLMClient, 或 SummaryLLMClient) 未初始化。"
            logger.error(error_msg)
            return False, error_msg, None

        # 1. 动态构建给LLM的超级工具Schema
        all_action_definitions = platform_builder_registry.get_all_action_definitions()

        # 把内部工具的定义也加进来！
        # 假设 InternalToolsProvider 已经被注册
        # 注意：这里的逻辑依赖于 InternalToolsProvider 也有一个 get_action_definitions 方法
        try:
            from src.action.providers.internal_tools_provider import InternalToolsProvider

            internal_tools_provider = InternalToolsProvider()
            internal_tools_definitions = internal_tools_provider.get_action_definitions()
            if internal_tools_definitions:
                all_action_definitions["internal"] = {
                    "type": "object",
                    "description": "核心内部工具。",
                    "properties": internal_tools_definitions,
                }
        except Exception as e:
            logger.warning(f"加载内部工具定义失败: {e}", exc_info=True)

        final_tool_schema = {
            "type": "function",
            "function": {
                "name": "execute_actions",
                "description": "执行一个或多个平台或内部动作。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "object",
                            "description": "一个包含了所有平台及内部工具动作的嵌套对象。key是平台ID或'internal'，value是该分类下的动作对象。",
                            "properties": all_action_definitions,
                        }
                    },
                    "required": ["action"],
                },
            },
        }

        # 2. 调用LLM进行决策
        decision_prompt = (
            f"分析以下意图，并根据提供的工具定义，决定需要执行的动作。\n\n"
            f"## 意图分析\n"
            f"- **核心想法:** {current_thought_context}\n"
            f"- **想要做的:** {action_description}\n"
            f"- **背后的动机:** {action_motivation}\n"
            f"- **相关外部信息:** {relevant_adapter_messages_context}\n\n"
            f"请根据以上信息，调用 `execute_actions` 工具来执行一个或多个动作。"
        )

        response = await self.action_llm_client.make_llm_request(
            prompt=decision_prompt,
            system_prompt="你是一个行动决策AI，你的任务是根据用户意图，调用合适的工具。",
            is_stream=False,
            tools=[final_tool_schema],
            tool_choice="auto",
        )

        # 3. 解析LLM的响应
        if response.get("error"):
            error_msg = f"行动决策LLM调用失败: {response.get('message')}"
            logger.error(error_msg)
            await self.thought_storage_service.update_action_status_in_thought_document(
                doc_key_for_updates, action_id, {"status": "LLM_DECISION_ERROR", "error_message": error_msg}
            )
            return False, error_msg, None

        tool_calls = response.get("tool_calls")
        if not tool_calls or not isinstance(tool_calls, list) or not tool_calls[0].get("function"):
            error_msg = "LLM未返回有效的工具调用，可能认为无需行动。"
            logger.info(error_msg)
            await self.thought_storage_service.update_action_status_in_thought_document(
                doc_key_for_updates, action_id, {"status": "COMPLETED_NO_TOOL", "final_result_for_shimo": error_msg}
            )
            if self.thought_trigger:
                self.thought_trigger.set()
            return True, error_msg, None

        # 4. 执行选择的动作
        try:
            arguments_str = tool_calls[0].get("function", {}).get("arguments", "{}")
            arguments = json.loads(arguments_str)
            action_object = arguments.get("action")
        except (json.JSONDecodeError, AttributeError) as e:
            error_msg = f"解析LLM工具调用参数失败: {e}"
            logger.error(error_msg)
            await self.thought_storage_service.update_action_status_in_thought_document(
                doc_key_for_updates, action_id, {"status": "ARGUMENT_PARSE_ERROR", "error_message": error_msg}
            )
            return False, error_msg, None

        if not action_object or not isinstance(action_object, dict):
            error_msg = "LLM调用了execute_actions工具，但没有提供有效的action参数对象。"
            logger.warning(error_msg)
            await self.thought_storage_service.update_action_status_in_thought_document(
                doc_key_for_updates, action_id, {"status": "NO_ACTION_PARAM", "final_result_for_shimo": error_msg}
            )
            if self.thought_trigger:
                self.thought_trigger.set()
            return True, error_msg, None

        # 5. 统一的动作执行循环
        for action_group_key, actions_in_group in action_object.items():
            if not isinstance(actions_in_group, dict):
                continue

            # --- ❤❤❤ 统一处理入口！❤❤❤ ---
            # 我把 _execute_chosen_action 的逻辑直接整合到这里了！
            if action_group_key == "internal":
                # --- 处理内部工具 ---
                for action_name, action_params in actions_in_group.items():
                    logger.info(f"准备执行内部工具: '{action_name}'")
                    action_func = self.action_registry.get_action(action_name)
                    if not action_func:
                        logger.error(f"找不到内部工具 '{action_name}' 的实现。")
                        continue

                    try:
                        tool_result_data = await action_func(**(action_params or {}))

                        # 对网页搜索结果进行总结
                        if action_name == "web_search" and tool_result_data and self.summary_llm_client:
                            summarizer = ToolResultSummarizer(self.summary_llm_client)
                            final_result = await summarizer.summarize(
                                original_query=action_params.get("query", action_description),
                                original_motivation=action_motivation,
                                tool_output=tool_result_data,
                            )
                        else:
                            final_result = f"内部工具 '{action_name}' 执行成功。"

                        await self.thought_storage_service.update_action_status_in_thought_document(
                            doc_key_for_updates,
                            action_id,
                            {"status": "COMPLETED_SUCCESS", "final_result_for_shimo": final_result},
                        )
                        if self.thought_trigger:
                            self.thought_trigger.set()
                        return True, final_result, tool_result_data
                    except Exception as e_exec:
                        final_result = f"执行内部工具 '{action_name}' 时出错: {e_exec}"
                        logger.error(final_result, exc_info=True)
                        await self.thought_storage_service.update_action_status_in_thought_document(
                            doc_key_for_updates, action_id, {"status": "EXECUTION_ERROR", "error_message": final_result}
                        )
                        if self.thought_trigger:
                            self.thought_trigger.set()
                        return False, final_result, None

            else:  # 默认为平台动作
                platform_id = action_group_key
                builder = platform_builder_registry.get_builder(platform_id)
                if not builder:
                    logger.error(f"找不到平台 '{platform_id}' 的翻译官，无法执行动作。")
                    continue

                for action_name, action_params in actions_in_group.items():
                    logger.info(f"准备执行平台动作: Platform='{platform_id}', Action='{action_name}'")
                    action_event = builder.build_action_event(action_name, action_params or {})
                    if not action_event:
                        logger.error(f"平台 '{platform_id}' 的翻译官不会翻译动作 '{action_name}'。")
                        continue

                    success, payload = await self._execute_platform_action(
                        action_to_send=action_event.to_dict(),
                        thought_doc_key=doc_key_for_updates,
                        original_action_description=f"{platform_id}.{action_name}",
                    )
                    final_result = (
                        f"动作 {platform_id}.{action_name} 已提交。"
                        if success
                        else f"动作 {platform_id}.{action_name} 提交失败: {payload}"
                    )
                    if self.thought_trigger:
                        self.thought_trigger.set()
                    return success, final_result, payload

        # 如果循环结束都没执行任何动作
        final_result_for_shimo = "AI决策的动作对象为空，或没有可执行的动作。"
        await self.thought_storage_service.update_action_status_in_thought_document(
            doc_key_for_updates,
            action_id,
            {"status": "COMPLETED_NO_TOOL", "final_result_for_shimo": final_result_for_shimo},
        )
        if self.thought_trigger:
            self.thought_trigger.set()
        return True, final_result_for_shimo, None

    async def send_action_and_wait_for_response(
        self, action_event_dict: dict[str, Any], timeout: int = ACTION_RESPONSE_TIMEOUT_SECONDS
    ) -> tuple[bool, dict[str, Any] | None]:
        if not self.pending_action_manager:
            return False, {"error": "PendingActionManager is not initialized."}

        event_type = action_event_dict.get("event_type", "")
        parts = event_type.split(".")
        adapter_id = parts[1] if len(parts) > 1 else None

        if not adapter_id:
            return False, {"error": "Action event must have a valid event_type with platform ID."}

        return await self._execute_platform_action(
            action_to_send=action_event_dict,
            thought_doc_key=None,
            original_action_description="internal_tool_call",
        )

    async def execute_simple_action(
        self, platform_id: str, action_name: str, params: dict, description: str
    ) -> tuple[bool, str]:
        """一个更简单的动作执行入口，用于内部系统调用，如专注模式。"""
        builder = platform_builder_registry.get_builder(platform_id)
        if not builder:
            return False, f"找不到平台 '{platform_id}' 的翻译官。"

        action_event = builder.build_action_event(action_name, params)
        if not action_event:
            return False, f"平台 '{platform_id}' 的翻译官不会翻译动作 '{action_name}'。"

        success, payload = await self._execute_platform_action(
            action_to_send=action_event.to_dict(),
            thought_doc_key=None,  # 专注模式不关联主意识思考文档
            original_action_description=description,
        )

        message = ""
        if isinstance(payload, dict):
            message = payload.get("error") or payload.get("message", str(payload))
        else:
            message = str(payload)

        return success, message

    # --- ❤❤❤ 这就是我为您准备的VIP贵宾通道！❤❤❤ ---
    async def submit_constructed_action(
        self, action_event_dict: dict[str, Any], action_description: str, associated_record_key: str | None = None
    ) -> tuple[bool, str]:
        """
        直接提交一个已构造好的动作事件，绕过LLM决策。
        """
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
