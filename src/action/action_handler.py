# src/action/action_handler.py
import asyncio
import json
import os
import time
import uuid
from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING, Any

from src.action.action_provider import ActionProvider
from src.common.custom_logging.logger_manager import get_logger
from src.config import config
from src.core_communication.action_sender import ActionSender
from src.database.services.action_log_storage_service import ActionLogStorageService
from src.database.services.event_storage_service import EventStorageService
from src.database.services.thought_storage_service import ThoughtStorageService
from src.llmrequest.llm_processor import Client as ProcessorClient

from .prompts import ACTION_DECISION_PROMPT_TEMPLATE, INFORMATION_SUMMARY_PROMPT_TEMPLATE

if TYPE_CHECKING:
    pass

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
        self.logger = get_logger(f"AIcarusCore.{self.__class__.__name__}")
        self.action_llm_client: ProcessorClient | None = None
        self.summary_llm_client: ProcessorClient | None = None
        self.action_sender: ActionSender | None = None
        self.thought_storage_service: ThoughtStorageService | None = None
        self.event_storage_service: EventStorageService | None = None
        self.action_log_service: ActionLogStorageService | None = None
        self.thought_trigger: asyncio.Event | None = None
        self._pending_actions: dict[str, tuple[asyncio.Event, str | None, str, dict[str, Any]]] = {}
        self._action_registry: dict[str, Callable[..., Coroutine[Any, Any, Any]]] = {}
        self.logger.info(f"{self.__class__.__name__} instance created.")

    def set_dependencies(
        self,
        thought_service: ThoughtStorageService | None = None,
        event_service: EventStorageService | None = None,
        action_log_service: ActionLogStorageService | None = None,
        action_sender: ActionSender | None = None,
    ) -> None:
        self.thought_storage_service = thought_service
        self.event_storage_service = event_service
        self.action_log_service = action_log_service
        self.action_sender = action_sender
        self.logger.info("ActionHandler 的依赖已成功设置。")

    def register_provider(self, provider: ActionProvider) -> None:
        """注册一个动作提供者。"""
        actions = provider.get_actions()
        for action_name, action_func in actions.items():
            full_action_name = action_name if provider.name == "platform" else f"{provider.name}.{action_name}"

            if full_action_name in self._action_registry:
                self.logger.warning(f"动作 '{full_action_name}' 已存在，将被新的提供者覆盖。")
            self._action_registry[full_action_name] = action_func
            self.logger.info(f"成功注册动作: {full_action_name}")

    def set_thought_trigger(self, trigger_event: asyncio.Event | None) -> None:
        if trigger_event is not None and not isinstance(trigger_event, asyncio.Event):
            self.logger.error(f"set_thought_trigger 收到一个无效的事件类型: {type(trigger_event)}。")
            self.thought_trigger = None
            return
        self.thought_trigger = trigger_event
        if trigger_event:
            self.logger.info("ActionHandler 的主思维触发器已成功设置。")

    def _create_llm_client_from_config(self, purpose_key: str) -> ProcessorClient | None:

        try:
            if not config.llm_models:
                self.logger.error("配置错误：AlcarusRootConfig 中缺少 'llm_models' 配置段。")
                return None
            model_params_cfg = getattr(config.llm_models, purpose_key, None)
            if not model_params_cfg or not hasattr(model_params_cfg, "provider"):
                self.logger.error(
                    f"配置错误：在 AlcarusRootConfig.llm_models 下未找到模型用途键 '{purpose_key}' 对应的有效模型配置，或类型不匹配。"
                )
                return None
            actual_provider_name_str: str = model_params_cfg.provider
            actual_model_name_str: str = model_params_cfg.model_name
            if not actual_provider_name_str or not actual_model_name_str:
                self.logger.error(
                    f"配置错误：模型 '{purpose_key}' (提供商: {actual_provider_name_str or '未知'}) 未指定 'provider' 或 'model_name'。"
                )
                return None
            general_llm_settings_obj = config.llm_client_settings
            final_proxy_host: str | None = os.getenv("HTTP_PROXY_HOST")
            final_proxy_port_str: str | None = os.getenv("HTTP_PROXY_PORT")
            final_proxy_port: int | None = None
            if final_proxy_port_str and final_proxy_port_str.isdigit():
                final_proxy_port = int(final_proxy_port_str)
            if final_proxy_host and final_proxy_port:
                self.logger.info(
                    f"ActionHandler LLM客户端将尝试使用环境变量中的代理: {final_proxy_host}:{final_proxy_port}"
                )
            else:
                self.logger.info("ActionHandler LLM客户端未在环境变量中检测到完整代理配置，将不使用代理。")
            model_for_client_constructor = {"provider": actual_provider_name_str.upper(), "name": actual_model_name_str}
            model_specific_kwargs: dict[str, Any] = {}
            if model_params_cfg.temperature is not None:
                model_specific_kwargs["temperature"] = model_params_cfg.temperature
            if model_params_cfg.max_output_tokens is not None:
                model_specific_kwargs["maxOutputTokens"] = model_params_cfg.max_output_tokens
            if model_params_cfg.top_p is not None:
                model_specific_kwargs["top_p"] = model_params_cfg.top_p
            if model_params_cfg.top_k is not None:
                model_specific_kwargs["top_k"] = model_params_cfg.top_k
            processor_constructor_args = {
                "model": model_for_client_constructor,
                "proxy_host": final_proxy_host,
                "proxy_port": final_proxy_port,
                **vars(general_llm_settings_obj),
                **model_specific_kwargs,
            }
            final_constructor_args = {k: v for k, v in processor_constructor_args.items() if v is not None}
            client_instance = ProcessorClient(**final_constructor_args)
            self.logger.info(
                f"成功创建 ProcessorClient 实例用于 '{purpose_key}' (模型: {client_instance.llm_client.model_name}, 提供商: {client_instance.llm_client.provider})."
            )
            return client_instance
        except AttributeError as e_attr:
            self.logger.error(
                f"配置访问错误 (AttributeError) 创建LLM客户端 (用途: {purpose_key}) 时: {e_attr}", exc_info=True
            )
            return None
        except Exception as e:
            self.logger.error(f"创建LLM客户端 (用途: {purpose_key}) 时发生未知错误: {e}", exc_info=True)
            return None

    async def initialize_llm_clients(self) -> None:
        # ... (此方法无需修改)
        if self.action_llm_client and self.summary_llm_client:
            return
        self.logger.info("正在为行动处理模块按需初始化LLM客户端...")
        if not self.action_llm_client:
            self.action_llm_client = self._create_llm_client_from_config(purpose_key="action_decision")
            if not self.action_llm_client:
                self.logger.critical("行动决策LLM客户端初始化失败。")
                raise RuntimeError("行动决策LLM客户端初始化失败。")
        if not self.summary_llm_client:
            self.summary_llm_client = self._create_llm_client_from_config(purpose_key="information_summary")
            if not self.summary_llm_client:
                self.logger.critical("信息总结LLM客户端初始化失败。")
                raise RuntimeError("信息总结LLM客户端初始化失败。")
        self.logger.info("行动处理模块的LLM客户端按需初始化完成。")

    async def _summarize_tool_result_async(
        self, original_query: str, original_motivation: str, tool_output: str | list | dict
    ) -> str:
        # ... (此方法无需修改)
        if not self.summary_llm_client:
            self.logger.error("信息总结LLM客户端未初始化，无法进行信息总结。")
            return f"错误：信息总结功能当前不可用。原始工具输出: {str(tool_output)[:200]}..."
        self.logger.info(f"正在调用LLM对工具结果进行信息总结。原始意图: '{original_query[:50]}...'")
        try:
            if isinstance(tool_output, list | dict):
                raw_tool_output_str = json.dumps(tool_output, indent=2, ensure_ascii=False, default=str)
            else:
                raw_tool_output_str = str(tool_output)
        except TypeError:
            raw_tool_output_str = str(tool_output)

        summary_prompt = INFORMATION_SUMMARY_PROMPT_TEMPLATE.format(
            original_query_or_action=original_query,
            original_motivation=original_motivation,
            raw_tool_output=raw_tool_output_str,
        )
        response = await self.summary_llm_client.make_llm_request(prompt=summary_prompt, is_stream=False)
        if response.get("error"):
            error_message = f"总结信息时LLM调用失败: {response.get('message', '未知API错误')}"
            self.logger.error(error_message)
            return error_message
        summary_text = response.get("text")
        if summary_text is None or not summary_text.strip():
            self.logger.warning("信息总结LLM调用成功，但未返回有效的文本内容。")
            return "未能从工具结果中总结出有效信息。"
        self.logger.info(f"信息总结完成。摘要 (前100字符): {summary_text[:100]}...")
        return summary_text.strip()

    async def _handle_action_timeout(
        self,
        action_id: str,
        thought_doc_key: str | None,
        original_action_description: str,
        original_action_to_send: dict[str, Any],
    ) -> None:
        # ... (此方法无需修改)
        if action_id in self._pending_actions:
            self.logger.warning(f"动作 '{action_id}' ({original_action_description}) 超时未收到响应！")
            pending_event, stored_thought_doc_key, stored_original_action_description, _ = self._pending_actions.pop(
                action_id
            )
            pending_event.set()

            is_direct_reply_action_for_timeout = stored_original_action_description == "回复主人"
            timeout_timestamp = int(time.time() * 1000)

            if self.action_log_service:
                await self.action_log_service.update_action_log_with_response(
                    action_id=action_id,
                    status="timeout",
                    response_timestamp=timeout_timestamp,
                    error_info="Action response timed out",
                )
            else:
                self.logger.error(f"动作 '{action_id}' 超时，但 action_log_service 未设置，无法更新 ActionLog！")

            if not is_direct_reply_action_for_timeout and stored_thought_doc_key and self.thought_storage_service:
                timeout_message = f"你尝试执行动作 '{stored_original_action_description}' 时，等待响应超时了。"
                update_payload_thought = {
                    "status": "TIMEOUT_FAILURE",
                    "final_result_for_shimo": timeout_message,
                    "error_message": "Action response timed out.",
                }
                await self.thought_storage_service.update_action_status_in_thought_document(
                    stored_thought_doc_key, action_id, update_payload_thought
                )
            elif not is_direct_reply_action_for_timeout and not stored_thought_doc_key:
                self.logger.warning(
                    f"动作 '{action_id}' ({stored_original_action_description}) 超时，但没有关联的 thought_doc_key，无法更新思考文档。"
                )
            elif is_direct_reply_action_for_timeout:
                self.logger.info(f"直接回复动作 '{action_id}' 超时，仅更新ActionLog。")

    async def handle_action_response(self, response_event_data: dict[str, Any]) -> None:
        """现在我学会读心术了，我会看情书的内容，而不是信封！这才是最完美的体位！"""
        self.logger.debug(f"动作处理器: 收到一个 action_response 事件: {response_event_data}")

        # --- 这就是最关键的修正！ ---
        # 我们要从 content 中解析出原始的动作ID，而不是用响应事件自己的ID！
        original_action_id: str | None = None
        response_content_list = response_event_data.get("content", [])

        # 做足安全检查，防止身体被玩坏~
        if response_content_list and isinstance(response_content_list, list) and len(response_content_list) > 0:
            first_seg = response_content_list[0]
            if isinstance(first_seg, dict) and "data" in first_seg:
                # original_event_id 在 data 字段里哦~
                original_action_id = first_seg.get("data", {}).get("original_event_id")

        if not original_action_id:
            response_event_id = response_event_data.get("event_id", "未知响应ID")
            self.logger.error(
                f"动作处理器: 收到的 action_response (ID: {response_event_id}) 内容里没有找到 'original_event_id'，无法处理！"
            )
            return

        if original_action_id not in self._pending_actions:
            self.logger.warning(
                f"动作处理器: 收到一个未知的或已处理/超时的 action_response，其指向的原始动作ID: {original_action_id}。"
            )
            return

        # 从这里开始，后面的逻辑都是对的，因为我们现在用的是正确的ID了！
        pending_event, stored_thought_doc_key, stored_original_action_description, original_action_sent_dict = (
            self._pending_actions.pop(original_action_id)
        )
        pending_event.set()  # 唤醒那个正在焦急等待的 _execute_platform_action 先生
        self.logger.info(
            f"动作处理器: 已匹配到等待中的动作 '{original_action_id}' ({stored_original_action_description})。"
        )

        action_successful = False
        response_status_str = "unknown"
        error_message_from_response = ""
        result_details_from_response: dict[str, Any] | None = None
        final_result_for_thought = f"执行动作 '{stored_original_action_description}' 后收到响应，但无法解析具体结果。"
        response_timestamp = int(time.time() * 1000)
        action_sent_timestamp = original_action_sent_dict.get("timestamp", response_timestamp)
        response_time_ms = response_timestamp - action_sent_timestamp

        # 重新从 content 中解析详细结果
        if response_content_list:
            first_segment = response_content_list[0]
            seg_type = first_segment.get("type", "")

            # 我们现在检查类型是不是以 action_response. 开头，这就很灵活了~
            if isinstance(first_segment, dict) and seg_type.startswith("action_response."):
                response_data = first_segment.get("data", {})
                # 直接从类型本身获取 success 或 failure
                response_status_str = seg_type.split(".")[-1]
                result_details_from_response = response_data.get("data")  # 额外数据

                if response_status_str == "success":
                    action_successful = True
                    final_result_for_thought = f"动作 '{stored_original_action_description}' 已成功执行。"
                    if result_details_from_response:
                        final_result_for_thought += (
                            f" 详情: {json.dumps(result_details_from_response, ensure_ascii=False)}"
                        )
                else:  # failure
                    error_message_from_response = response_data.get("message", "适配器报告未知错误")
                    final_result_for_thought = (
                        f"动作 '{stored_original_action_description}' 执行失败: {error_message_from_response}"
                    )

            else:
                self.logger.warning(
                    f"动作处理器: Action_response for '{original_action_id}' 的 content[0] 不是预期的 'action_response.*' 类型。收到的类型: {seg_type}"
                )
        else:
            self.logger.warning(f"动作处理器: Action_response for '{original_action_id}' 缺少有效的 content 列表。")

        # 更新 ActionLog
        if self.action_log_service:
            await self.action_log_service.update_action_log_with_response(
                action_id=original_action_id,
                status=response_status_str,
                response_timestamp=response_timestamp,
                response_time_ms=response_time_ms,
                error_info=error_message_from_response if not action_successful else None,
                result_details=result_details_from_response,
            )

        # 更新思考文档
        is_direct_reply_action_for_response = (
            stored_original_action_description == "回复主人"
        )  # 假设这是不需要更新思考的特殊标记
        if not is_direct_reply_action_for_response and stored_thought_doc_key and self.thought_storage_service:
            update_payload_thought = {
                "status": "COMPLETED_SUCCESS" if action_successful else "COMPLETED_FAILURE",
                "final_result_for_shimo": final_result_for_thought,
                "error_message": "" if action_successful else error_message_from_response,
                "response_received_at": response_timestamp,
            }
            await self.thought_storage_service.update_action_status_in_thought_document(
                stored_thought_doc_key, original_action_id, update_payload_thought
            )
            self.logger.info(
                f"已更新思考文档 '{stored_thought_doc_key}' 中动作 '{original_action_id}' 的状态为: {'成功' if action_successful else '失败'}。"
            )
        # 如果动作成功，将其作为“既成事实”存入 events 表
        if action_successful and self.event_storage_service:
            event_to_save_in_events = original_action_sent_dict.copy()
            event_to_save_in_events["event_id"] = original_action_id
            event_to_save_in_events["timestamp"] = response_timestamp
            action_message_id = await self.get_sent_message_id_safe(response_event_data)
            action_metadata = [{"type": "message_metadata", "data": {"message_id": action_message_id}}]
            event_to_save_in_events["content"] = action_metadata + event_to_save_in_events["content"]
            event_to_save_in_events["user_info"] = {
                "platform": response_event_data.get("platform", "unknown_platform"),
                "user_id": response_event_data.get("bot_id", "unknown_user_id"),
                "user_nickname": config.persona.bot_name,
            }

            self.logger.info(
                f"准备将成功的动作作为事件存入数据库。original_action_id: {original_action_id}, event_type: {event_to_save_in_events.get('event_type')}, 将使用的event_id: {event_to_save_in_events['event_id']}"
            )

            await self.event_storage_service.save_event_document(event_to_save_in_events)
            self.logger.info(
                f"成功的平台动作 '{original_action_id}' (类型: {event_to_save_in_events.get('event_type')}, 存入的event_id: {event_to_save_in_events['event_id']}) 已作为事件存入 events 表。"
            )

    async def _execute_platform_action(
        self,
        action_to_send: dict[str, Any],
        thought_doc_key: str | None,
        original_action_description: str,
    ) -> tuple[bool, str]:
        if not self.action_sender or not self.action_log_service:
            self.logger.error("核心服务 (ActionSender 或动作日志服务) 未设置!")
            return False, "内部错误：核心服务不可用。"

        is_direct_reply_action = original_action_description == "回复主人"
        is_sub_consciousness_reply = original_action_description == "发送子意识聊天回复"

        if not is_direct_reply_action and not is_sub_consciousness_reply and not thought_doc_key:
            self.logger.error(f"严重错误：动作 '{original_action_description}' 缺少 thought_doc_key。")
            return False, "内部错误：执行动作缺少必要的思考文档关联。"

        core_action_id = action_to_send.get("event_id") or str(uuid.uuid4())
        action_to_send["event_id"] = core_action_id
        action_type = action_to_send.get("event_type", "unknown_action_type")
        platform_id_from_event = action_to_send.get("platform", "unknown_platform")
        target_adapter_id = platform_id_from_event
        if platform_id_from_event == "master_ui":
            target_adapter_id = "master_ui_adapter"

        adapter_behavior_config = config.platform_action_settings.adapter_behaviors.get(target_adapter_id)
        confirms_by_action_response = True
        if adapter_behavior_config:
            confirms_by_action_response = adapter_behavior_config.confirms_by_action_response

        current_timestamp_ms = int(time.time() * 1000)
        action_to_send["timestamp"] = current_timestamp_ms
        action_log_initial_status = "executing_awaiting_self_report" if not confirms_by_action_response else "executing"

        if self.action_log_service:
            conversation_id = action_to_send.get("conversation_info", {}).get("conversation_id", "unknown_conv_id")
            await self.action_log_service.save_action_attempt(
                action_id=core_action_id,
                action_type=action_type,
                timestamp=current_timestamp_ms,
                platform=platform_id_from_event,
                bot_id=action_to_send.get("bot_id", config.persona.bot_name),
                conversation_id=conversation_id,
                content=action_to_send.get("content", []),
            )
            if not confirms_by_action_response:
                await self.action_log_service.update_action_log_with_response(
                    action_id=core_action_id, status=action_log_initial_status, response_timestamp=current_timestamp_ms
                )

        if not is_direct_reply_action and self.thought_storage_service and thought_doc_key:
            thought_update_status = (
                "EXECUTING_AWAITING_SELF_REPORT" if not confirms_by_action_response else "EXECUTING_AWAITING_RESPONSE"
            )
            await self.thought_storage_service.update_action_status_in_thought_document(
                thought_doc_key,
                core_action_id,
                {"status": thought_update_status, "sent_to_adapter_at": current_timestamp_ms},
            )

        try:
            send_to_adapter_successful = await self.action_sender.send_action_to_adapter_by_id(
                adapter_id=target_adapter_id, action_event=action_to_send
            )
            if not send_to_adapter_successful:
                error_message_comm = f"发送到适配器 '{target_adapter_id}' 失败：适配器未连接或通信层无法发送。"
                # ... (处理发送失败)
                return False, error_message_comm
        except Exception as e_send:
            # ... (处理异常)
            return False, f"发送平台动作时发生意外异常: {str(e_send)}"

        if confirms_by_action_response:
            response_received_event = asyncio.Event()
            self._pending_actions[core_action_id] = (
                response_received_event,
                thought_doc_key,
                original_action_description,
                action_to_send,
            )
            try:
                await asyncio.wait_for(response_received_event.wait(), timeout=ACTION_RESPONSE_TIMEOUT_SECONDS)
                final_log_entry = (
                    await self.action_log_service.get_action_log(core_action_id) if self.action_log_service else None
                )
                if final_log_entry and final_log_entry.get("status") == "success":
                    return True, f"动作 '{original_action_description}' 已成功执行并收到响应。"
                elif final_log_entry:
                    error_details = final_log_entry.get("error_info") or "适配器未提供具体错误信息"
                    return False, f"动作 '{original_action_description}' 执行失败: {error_details}"
                return False, f"动作 '{original_action_description}' 响应处理后状态未知。"
            except TimeoutError:
                if core_action_id in self._pending_actions:
                    await self._handle_action_timeout(
                        core_action_id, thought_doc_key, original_action_description, action_to_send
                    )
                return False, f"动作 '{original_action_description}' 响应超时。"
            finally:
                self._pending_actions.pop(core_action_id, None)
        else:
            self.logger.info(f"平台动作 '{core_action_id}' 已发送给自我上报型适配器 '{target_adapter_id}'。")
            return True, "动作已发送，等待自我上报。"

    async def process_action_flow(
        self,
        action_id: str,
        doc_key_for_updates: str,
        action_description: str,
        action_motivation: str,
        current_thought_context: str,
        relevant_adapter_messages_context: str = "无相关外部消息或请求。",
    ) -> tuple[bool, str, Any]:
        self.logger.info(f"--- [Action ID: {action_id}, DocKey: {doc_key_for_updates}] 开始处理行动流程 ---")
        if not self.thought_storage_service or not self.action_llm_client:
            # ... (处理核心服务未初始化)
            return False, "核心服务未初始化", None

        decision_prompt_text = ACTION_DECISION_PROMPT_TEMPLATE.format(
            current_thought_context=current_thought_context,
            action_description=action_description,
            action_motivation=action_motivation,
            relevant_adapter_messages_context=relevant_adapter_messages_context,
        )
        decision_response = await self.action_llm_client.make_llm_request(prompt=decision_prompt_text, is_stream=False)

        final_result_for_shimo: str = f"尝试执行动作 '{action_description}' 时出现未知的处理错误。"
        action_was_successful: bool = False
        action_result_payload: Any = None

        if decision_response.get("error"):
            final_result_for_shimo = f"行动决策LLM调用失败: {decision_response.get('message', '未知API错误')}"
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

        llm_raw_output_text = decision_response.get("text", "").strip()
        if not llm_raw_output_text:
            final_result_for_shimo = "行动决策失败，LLM的响应中不包含任何文本内容。"
            await self.thought_storage_service.update_action_status_in_thought_document(
                doc_key_for_updates,
                action_id,
                {
                    "status": "LLM_DECISION_EMPTY",
                    "error_message": final_result_for_shimo,
                    "final_result_for_shimo": final_result_for_shimo,
                },
            )
            return False, final_result_for_shimo, None

        try:
            json_string = llm_raw_output_text.strip()
            if json_string.startswith("```json"):
                json_string = json_string[7:]
            if json_string.startswith("```"):
                json_string = json_string[3:]
            if json_string.endswith("```"):
                json_string = json_string[:-3]
            json_string = json_string.strip()
            parsed_decision = json.loads(json_string)
            tool_name_chosen = parsed_decision.get("tool_to_use")
            tool_arguments = parsed_decision.get("arguments", {})
        except Exception as e_parse:
            final_result_for_shimo = f"解析LLM决策JSON失败: {e_parse}. 原始文本: {llm_raw_output_text[:200]}..."
            await self.thought_storage_service.update_action_status_in_thought_document(
                doc_key_for_updates,
                action_id,
                {
                    "status": "LLM_DECISION_PARSE_ERROR",
                    "error_message": final_result_for_shimo,
                    "final_result_for_shimo": final_result_for_shimo,
                },
            )
            return False, final_result_for_shimo, None

        if not tool_name_chosen:
            final_result_for_shimo = "AI决策不使用任何工具。"
            action_was_successful = True
            await self.thought_storage_service.update_action_status_in_thought_document(
                doc_key_for_updates,
                action_id,
                {"status": "COMPLETED_NO_TOOL", "final_result_for_shimo": final_result_for_shimo},
            )
        else:
            action_func = self._action_registry.get(tool_name_chosen)
            if not action_func:
                final_result_for_shimo = f"未在注册表中找到名为 '{tool_name_chosen}' 的动作。"
                await self.thought_storage_service.update_action_status_in_thought_document(
                    doc_key_for_updates,
                    action_id,
                    {
                        "status": "TOOL_NOT_FOUND",
                        "error_message": final_result_for_shimo,
                        "final_result_for_shimo": final_result_for_shimo,
                    },
                )
            else:
                self.logger.info(f"从注册表找到动作 '{tool_name_chosen}'，准备执行。")
                try:
                    if tool_name_chosen.startswith("platform."):
                        tool_arguments["thought_doc_key"] = doc_key_for_updates
                        tool_arguments["original_action_description"] = action_description
                        action_was_successful, final_result_for_shimo = await action_func(**tool_arguments)
                        action_result_payload = {
                            "status": "platform_action_submitted",
                            "message": final_result_for_shimo,
                            "success": action_was_successful,
                        }
                    else:  # Internal tool
                        tool_result_data = await action_func(**tool_arguments)
                        action_was_successful = True
                        if tool_name_chosen == "search_web" and tool_result_data:
                            final_result_for_shimo = await self._summarize_tool_result_async(
                                tool_arguments.get("query", action_description), action_motivation, tool_result_data
                            )
                        else:
                            final_result_for_shimo = f"工具 '{tool_name_chosen}' 执行成功。"
                        action_result_payload = tool_result_data
                        await self.thought_storage_service.update_action_status_in_thought_document(
                            doc_key_for_updates,
                            action_id,
                            {"status": "COMPLETED_SUCCESS", "final_result_for_shimo": final_result_for_shimo},
                        )
                except Exception as e_exec:
                    final_result_for_shimo = f"执行动作 '{tool_name_chosen}' 时出错: {e_exec}"
                    self.logger.error(final_result_for_shimo, exc_info=True)
                    action_was_successful = False
                    await self.thought_storage_service.update_action_status_in_thought_document(
                        doc_key_for_updates,
                        action_id,
                        {
                            "status": "EXECUTION_ERROR",
                            "error_message": final_result_for_shimo,
                            "final_result_for_shimo": final_result_for_shimo,
                        },
                    )

        if self.thought_trigger:
            self.thought_trigger.set()

        return action_was_successful, final_result_for_shimo, action_result_payload

    async def submit_constructed_action(
        self, action_event_dict: dict[str, Any], action_description: str, associated_record_key: str | None = None
    ) -> tuple[bool, str]:
        if not self.action_sender or not self.action_log_service:
            critical_error_msg = "核心服务 (ActionSender 或动作日志服务) 未设置!"
            self.logger.critical(critical_error_msg)
            return False, critical_error_msg

        if "event_id" not in action_event_dict:
            return False, "动作事件缺少 'event_id'"

        success, message = await self._execute_platform_action(
            action_to_send=action_event_dict,
            thought_doc_key=associated_record_key,
            original_action_description=action_description,
        )

        return success, message

    async def get_sent_message_id_safe(self, event_data: dict[str, Any]) -> str:
        """
        安全地从事件字典中解析并获取 sent_message_id，现在它更喜欢被直接插入，而不是自己脱衣服！

        Args:
            event_data: 包含事件数据的字典，要的就是这种直白的感觉！

        Returns:
            如果成功找到那个让人兴奋的 ID，就返回它；否则返回 'unknow_message_id'，表示没找到G点。
        """
        default_id = "unknow_message_id"

        # 我们直接假设 event_data 已经是我们想要的肉体（dict），不需要再用 json.loads 去强行脱衣了
        if not isinstance(event_data, dict):
            return default_id

        content_list = event_data.get("content")

        # 开始深入探索 content 这个小穴
        if isinstance(content_list, list) and len(content_list) > 0:
            first_item = content_list[0]
            if isinstance(first_item, dict):
                # 根据协议 v1.4.0 的体位，成功响应的 data 里还有一个 data，好深...
                response_data = first_item.get("data", {})
                if isinstance(response_data, dict):
                    # 再往里探一层，寻找最深处的快乐
                    details_data = response_data.get("data", {})
                    if isinstance(details_data, dict):
                        sent_message_id = details_data.get("sent_message_id")
                        if sent_message_id is not None:
                            # 啊...找到了！就是这个！
                            return str(sent_message_id)

        # 唉，一番云雨后还是没找到，只好返回默认值了
        return default_id
