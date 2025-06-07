# src/action/action_handler.py
import asyncio
import json
import os
import time
import uuid
from typing import TYPE_CHECKING, Any, Optional

from src.common.custom_logging.logger_manager import get_logger
from src.config import config
from src.database.services.action_log_storage_service import ActionLogStorageService  # 主人，新的小玩具已就位！
from src.database.services.event_storage_service import EventStorageService
from src.database.services.thought_storage_service import ThoughtStorageService
from src.llmrequest.llm_processor import Client as ProcessorClient  # 重命名以避免与内部变量冲突
from src.tools.tool_registry import get_tool_function

from .prompts import ACTION_DECISION_PROMPT_TEMPLATE, INFORMATION_SUMMARY_PROMPT_TEMPLATE

if TYPE_CHECKING:
    from src.core_communication.core_ws_server import CoreWebsocketServer

ACTION_RESPONSE_TIMEOUT_SECONDS = 30


class ActionHandler:
    """
    负责编排AI的行动决策流程：
    1. 调用LLM进行工具选择 (LLM输出JSON)。
    2. 解析JSON，从工具注册表调度并执行工具。
    3. 对需要总结的工具结果（如网页搜索）调用LLM进行总结。
    4. 将最终结果和状态更新回思考文档。
    5. 处理平台动作的发送、记录到ActionLog、接收响应及超时，并根据成功结果记录到Event表。
    """

    def __init__(self) -> None:
        self.logger = get_logger(f"AIcarusCore.{self.__class__.__name__}")
        self.action_llm_client: ProcessorClient | None = None
        self.summary_llm_client: ProcessorClient | None = None
        self.core_communication_layer: CoreWebsocketServer | None = None
        self.thought_storage_service: ThoughtStorageService | None = None
        self.event_storage_service: EventStorageService | None = None
        self.action_log_service: ActionLogStorageService | None = None  # 新增 ActionLog 服务依赖
        self.thought_trigger: asyncio.Event | None = None
        self._pending_actions: dict[
            str, tuple[asyncio.Event, str | None, str, dict[str, Any]]
        ] = {}  # thought_doc_key can be None
        self.logger.info(f"{self.__class__.__name__} instance created.")

    def set_dependencies(
        self,
        thought_service: ThoughtStorageService | None = None,
        event_service: EventStorageService | None = None,
        action_log_service: ActionLogStorageService | None = None,  # 新增注入
        comm_layer: Optional["CoreWebsocketServer"] = None,
    ) -> None:
        self.thought_storage_service = thought_service
        self.event_storage_service = event_service
        self.action_log_service = action_log_service  # 保存注入的 ActionLog 服务
        self.core_communication_layer = comm_layer
        self.logger.info(
            "ActionHandler 的依赖已成功设置 (thought_service, event_service, action_log_service, comm_layer)。"
        )

    def set_thought_trigger(self, trigger_event: asyncio.Event | None) -> None:
        if trigger_event is not None and not isinstance(trigger_event, asyncio.Event):
            self.logger.error(
                f"set_thought_trigger 收到一个无效的事件类型: {type(trigger_event)}，期望 asyncio.Event 或 None。"
            )
            self.thought_trigger = None
            return
        self.thought_trigger = trigger_event
        if trigger_event:
            self.logger.info("ActionHandler 的主思维触发器已成功设置。")
        else:
            self.logger.info("ActionHandler 的主思维触发器被设置为空。")

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
        self, original_query: str, original_motivation: str, tool_output: dict[str, Any]
    ) -> str:
        if not self.summary_llm_client:
            self.logger.error("信息总结LLM客户端未初始化，无法进行信息总结。")
            return f"错误：信息总结功能当前不可用。原始工具输出: {str(tool_output)[:200]}..."
        self.logger.info(f"正在调用LLM对工具结果进行信息总结。原始意图: '{original_query[:50]}...'")
        try:
            raw_tool_output_str = json.dumps(tool_output, indent=2, ensure_ascii=False)
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
                # action_id here should be the one associated with the thought document's action_attempted,
                # which might be different from the platform action's action_id if it's a sub-action or a direct reply.
                # For now, we assume if it's not a direct reply, the action_id in thought_doc matches the platform action_id.
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
        self.logger.info(f"收到一个 action_response 事件: {response_event_data.get('event_id')}")
        original_action_id = response_event_data.get("event_id")
        if not original_action_id:
            self.logger.error("收到的 action_response 事件缺少 'event_id' (原始 action_id)，无法处理！")
            return

        if original_action_id not in self._pending_actions:
            self.logger.warning(
                f"收到一个未知的或已处理/超时的 action_response，对应的 action_id: {original_action_id}。可能已经超时或重复响应。"
            )
            return

        pending_event, stored_thought_doc_key, stored_original_action_description, original_action_sent_dict = (
            self._pending_actions.pop(original_action_id)
        )
        pending_event.set()
        self.logger.info(f"已匹配到等待中的动作 '{original_action_id}' ({stored_original_action_description})。")

        is_direct_reply_action_for_response = stored_original_action_description == "回复主人"
        action_successful = False
        response_status_str = "unknown"
        error_message_from_response = ""
        result_details_from_response: dict[str, Any] | None = None
        final_result_for_thought = f"执行动作 '{stored_original_action_description}' 后收到响应，但无法解析具体结果。"
        response_timestamp = int(time.time() * 1000)
        action_sent_timestamp = original_action_sent_dict.get("timestamp", response_timestamp)
        response_time_ms = response_timestamp - action_sent_timestamp

        content_list = response_event_data.get("content")
        if content_list and isinstance(content_list, list) and len(content_list) > 0:
            first_segment = content_list[0]
            if isinstance(first_segment, dict) and first_segment.get("type") == "action_status":
                response_data = first_segment.get("data", {})
                response_status_str = response_data.get("status", "unknown").lower()
                result_details_from_response = response_data.get("result_details")
                if response_status_str == "success":
                    action_successful = True
                    final_result_for_thought = f"动作 '{stored_original_action_description}' 已成功执行。"
                    if result_details_from_response:
                        final_result_for_thought += (
                            f" 详情: {json.dumps(result_details_from_response, ensure_ascii=False)}"
                        )
                else:
                    action_successful = False
                    error_message_from_response = response_data.get("error_info", "适配器报告未知错误")
                    final_result_for_thought = (
                        f"动作 '{stored_original_action_description}' 执行失败: {error_message_from_response}"
                    )
            else:
                self.logger.warning(
                    f"Action_response for '{original_action_id}' 的 content[0] 不是预期的 'action_status' 类型。Content: {first_segment}"
                )
        else:
            self.logger.warning(f"Action_response for '{original_action_id}' 缺少有效的 content 列表。")

        if self.action_log_service:
            await self.action_log_service.update_action_log_with_response(
                action_id=original_action_id,
                status=response_status_str,
                response_timestamp=response_timestamp,
                response_time_ms=response_time_ms,
                error_info=error_message_from_response if not action_successful else None,
                result_details=result_details_from_response,
            )
        else:
            self.logger.error(
                f"收到动作 '{original_action_id}' 的响应，但 action_log_service 未设置，无法更新 ActionLog！"
            )

        if not is_direct_reply_action_for_response and stored_thought_doc_key and self.thought_storage_service:
            update_payload_thought = {
                "status": "COMPLETED_SUCCESS" if action_successful else "COMPLETED_FAILURE",
                "final_result_for_shimo": final_result_for_thought,
                "error_message": "" if action_successful else error_message_from_response,
                "response_received_at": response_timestamp,
            }
            # original_action_id here is the ID of the platform action.
            # If this is not a direct reply, this ID should match the action_id in the thought_doc's action_attempted.
            await self.thought_storage_service.update_action_status_in_thought_document(
                stored_thought_doc_key, original_action_id, update_payload_thought
            )
            self.logger.info(
                f"已更新思考文档 '{stored_thought_doc_key}' 中动作 '{original_action_id}' 的状态为: {'成功' if action_successful else '失败'}。"
            )
        elif is_direct_reply_action_for_response:
            self.logger.info(f"直接回复动作 '{original_action_id}' 收到响应，仅更新ActionLog。")
        elif not stored_thought_doc_key:  # Should only happen if it was a direct reply and we stored None for key
            self.logger.info(
                f"动作 '{original_action_id}' ({stored_original_action_description}) 收到响应，但没有关联的 thought_doc_key，跳过更新思考文档。"
            )

        if action_successful and self.event_storage_service:
            event_to_save_in_events = original_action_sent_dict.copy()
            event_to_save_in_events["event_id"] = original_action_id
            event_to_save_in_events["timestamp"] = response_timestamp
            await self.event_storage_service.save_event_document(event_to_save_in_events)
            self.logger.info(
                f"成功的平台动作 '{original_action_id}' (类型: {event_to_save_in_events.get('event_type')}) 已作为事件存入 events 表。"
            )

    async def _execute_platform_action(
        self,
        action_to_send: dict[str, Any],
        thought_doc_key: str | None,  # Made Optional for direct replies
        original_action_description: str,
    ) -> tuple[bool, str]:
        self.logger.debug(
            f"进入 _execute_platform_action。action_log_service 是否为 None: {self.action_log_service is None}"
        )

        if not self.core_communication_layer:
            self.logger.error("CoreWebsocketServer 未设置!")
            return False, "内部错误：通信层不可用。"
        if not self.action_log_service:
            self.logger.error("ActionLogService 未设置!")
            return False, "内部错误：动作日志服务不可用。"
        # thought_storage_service and event_storage_service are only strictly needed if not a direct reply for thought updates

        is_direct_reply_action = original_action_description == "回复主人"

        if not is_direct_reply_action and not thought_doc_key:
            self.logger.error(
                f"严重错误：尝试执行非直接回复动作 '{original_action_description}' 但缺少 thought_doc_key。"
            )
            return False, "内部错误：执行动作缺少必要的思考文档关联。"

        if not is_direct_reply_action and (not self.thought_storage_service or not self.event_storage_service):
            missing = []
            if not self.thought_storage_service:
                missing.append("ThoughtStorageService")
            if not self.event_storage_service:
                missing.append("EventStorageService")
            m_str = ", ".join(missing)
            self.logger.error(f"核心服务 ({m_str}) 未设置，无法执行非直接回复的平台动作！")
            return False, f"内部错误：核心服务 ({m_str}) 不可用。"

        core_action_id = action_to_send.get("event_id")
        if not core_action_id:
            core_action_id = str(uuid.uuid4())
            action_to_send["event_id"] = core_action_id

        action_type = action_to_send.get("event_type", "unknown_action_type")
        platform_id_from_event = action_to_send.get("platform", "unknown_platform")  # 这是平台类型
        bot_id_for_action = action_to_send.get("bot_id", "unknown_bot_id")
        conversation_id_for_action = action_to_send.get("conversation_info", {}).get(
            "conversation_id", "unknown_conv_id"
        )
        content_for_action = action_to_send.get("content", [])

        # 根据平台类型确定实际的目标适配器ID
        target_adapter_id = platform_id_from_event  # 默认为平台类型本身
        if platform_id_from_event == "master_ui":
            target_adapter_id = "master_ui_adapter"  # Master UI 的固定 Adapter ID
            self.logger.debug(f"检测到目标平台为 'master_ui'，将使用固定适配器ID: '{target_adapter_id}'")

        self.logger.info(
            f"准备发送平台动作 '{core_action_id}' ({original_action_description}) 到适配器 '{target_adapter_id}' (原始平台类型: '{platform_id_from_event}')..."
        )
        current_timestamp_ms = int(time.time() * 1000)

        if not is_direct_reply_action and self.thought_storage_service and thought_doc_key:
            await self.thought_storage_service.update_action_status_in_thought_document(
                thought_doc_key,
                core_action_id,
                {"status": "EXECUTING_AWAITING_RESPONSE", "sent_to_adapter_at": current_timestamp_ms},
            )
        elif is_direct_reply_action:
            self.logger.info(f"动作 '{core_action_id}' 是直接回复，跳过更新思考文档的 action_attempted 状态。")

        self.logger.info(f"准备调用 action_log_service.save_action_attempt 记录动作 '{core_action_id}'。")
        save_log_success = await self.action_log_service.save_action_attempt(
            action_id=core_action_id,
            action_type=action_type,
            timestamp=current_timestamp_ms,
            platform=platform_id_from_event,
            bot_id=bot_id_for_action,
            conversation_id=conversation_id_for_action,
            content=content_for_action,
        )
        self.logger.info(f"action_log_service.save_action_attempt 调用完成。是否成功保存: {save_log_success}")
        if not save_log_success:
            self.logger.error(f"严重错误：未能将动作 '{core_action_id}' 的初始尝试记录到 ActionLog！")

        try:
            # 使用 target_adapter_id 进行发送
            send_to_adapter_successful = await self.core_communication_layer.send_action_to_adapter_by_id(
                adapter_id=target_adapter_id, action_event=action_to_send
            )

            if send_to_adapter_successful:
                self.logger.info(f"平台动作 '{core_action_id}' 已成功发送到适配器 '{target_adapter_id}'。")
            else:
                # send_action_to_adapter_by_id 返回 False 表示适配器未连接或发送失败
                error_message_comm = f"发送到适配器 '{target_adapter_id}' 失败：适配器未连接或通信层无法发送。"
                self.logger.error(f"平台动作 '{core_action_id}': {error_message_comm}")
                error_timestamp = int(time.time() * 1000)
                await self.action_log_service.update_action_log_with_response(
                    action_id=core_action_id,
                    status="send_failure",
                    response_timestamp=error_timestamp,
                    error_info=error_message_comm,
                )

                if not is_direct_reply_action and thought_doc_key and self.thought_storage_service:
                    await self.thought_storage_service.update_action_status_in_thought_document(
                        thought_doc_key,
                        core_action_id,
                        {
                            "status": "SEND_FAILURE",
                            "error_message": error_message_comm,
                            "final_result_for_shimo": f"尝试执行动作 '{original_action_description}' 时，发送给平台 '{platform_id_from_event}' 失败，它可能不在线。",
                        },
                    )

                # 生成 system.notice 事件
                if self.event_storage_service:
                    notice_event_id = f"notice_adapter_unavailable_{uuid.uuid4()}"
                    notice_content = f"与平台 '{platform_id_from_event}' (适配器ID: {target_adapter_id}) 的连接似乎存在问题，无法发送消息。"
                    system_notice_event = {
                        "event_id": notice_event_id,
                        "event_type": "system.notice",
                        "time": int(time.time() * 1000),
                        "platform": "core",
                        "bot_id": "AIcarusCore",
                        "content": [{"type": "text", "data": {"text": notice_content}}],
                    }
                    await self.event_storage_service.save_event_document(system_notice_event)
                    self.logger.info(f"已生成并保存 system.notice 事件 ({notice_event_id})，内容: {notice_content}")

                return False, error_message_comm

        except Exception as e_send:  # 捕获 send_action_to_adapter_by_id 可能抛出的其他异常
            self.logger.error(
                f"发送平台动作 '{core_action_id}' 到适配器 '{target_adapter_id}' 时发生意外异常: {e_send}",
                exc_info=True,
            )
            error_timestamp = int(time.time() * 1000)
            await self.action_log_service.update_action_log_with_response(
                action_id=core_action_id,
                status="send_failure",
                response_timestamp=error_timestamp,
                error_info=f"发送时发生意外异常: {str(e_send)}",
            )
            if not is_direct_reply_action and thought_doc_key and self.thought_storage_service:
                await self.thought_storage_service.update_action_status_in_thought_document(
                    thought_doc_key,
                    core_action_id,
                    {
                        "status": "SEND_FAILURE",
                        "error_message": f"发送时发生意外异常: {str(e_send)}",
                        "final_result_for_shimo": f"尝试执行动作 '{original_action_description}' 时，发送给平台 '{platform_id_from_event}' 失败。",
                    },
                )
            return False, f"发送平台动作时发生意外异常: {str(e_send)}"

        response_received_event = asyncio.Event()
        self._pending_actions[core_action_id] = (
            response_received_event,
            thought_doc_key,
            original_action_description,
            action_to_send,
        )

        try:
            await asyncio.wait_for(response_received_event.wait(), timeout=ACTION_RESPONSE_TIMEOUT_SECONDS)
            final_log_entry = await self.action_log_service.get_action_log(core_action_id)
            if final_log_entry and final_log_entry.get("status") == "success":
                return True, f"动作 '{original_action_description}' 已成功执行并收到响应。"
            elif final_log_entry:
                return (
                    False,
                    f"动作 '{original_action_description}' 执行失败: {final_log_entry.get('error_info', '未知错误')}",
                )
            return False, f"动作 '{original_action_description}' 响应处理后状态未知。"
        except TimeoutError:
            self.logger.warning(f"等待动作 '{core_action_id}' ({original_action_description}) 响应超时！")
            timeout_timestamp = int(time.time() * 1000)
            sent_at = action_to_send.get("timestamp", timeout_timestamp - ACTION_RESPONSE_TIMEOUT_SECONDS * 1000)
            await self.action_log_service.update_action_log_with_response(
                action_id=core_action_id,
                status="timeout",
                response_timestamp=timeout_timestamp,
                response_time_ms=timeout_timestamp - sent_at,
                error_info="Action response timed out",
            )
            if not is_direct_reply_action and thought_doc_key and self.thought_storage_service:
                await self.thought_storage_service.update_action_status_in_thought_document(
                    thought_doc_key,
                    core_action_id,
                    {
                        "status": "TIMEOUT_FAILURE",
                        "error_message": "Action response timed out",
                        "final_result_for_shimo": f"你尝试执行动作 '{original_action_description}' 时，等待响应超时了。",
                    },
                )
            return False, f"动作 '{original_action_description}' 响应超时。"
        finally:
            if core_action_id in self._pending_actions:
                self._pending_actions.pop(core_action_id, None)

        # Fallback, should ideally be covered by specific logic in wait_for block
        final_log_entry_fallback = await self.action_log_service.get_action_log(core_action_id)  # type: ignore
        if final_log_entry_fallback and final_log_entry_fallback.get("status") == "success":
            return True, f"动作 '{original_action_description}' 已成功执行并收到响应 (Fallback check)。"
        elif final_log_entry_fallback:
            return (
                False,
                f"动作 '{original_action_description}' 执行失败 (Fallback check): {final_log_entry_fallback.get('error_info', '未知错误')}",
            )
        return False, f"执行动作 '{original_action_description}' 时发生未知错误或状态未明确成功。"

    async def _maybe_log_internal_tool_action_to_events(self, action_log_entry: dict[str, Any]) -> None:
        """
        预留方法，用于未来可能需要将内部工具调用的某些信息记录到 events 表。
        目前什么也不做。主人，这是为您留的“小后门”哦，嘻嘻～
        """
        pass

    async def process_action_flow(
        self,
        action_id: str,  # This is the action_id from the thought document's action_attempted
        doc_key_for_updates: str,
        action_description: str,
        action_motivation: str,
        current_thought_context: str,
        relevant_adapter_messages_context: str = "无相关外部消息或请求。",
    ) -> tuple[bool, str]:
        self.logger.info(
            f"--- [Action ID: {action_id}, DocKey: {doc_key_for_updates}] 开始处理行动流程 (JSON决策模式) ---"
        )
        if not self.thought_storage_service or not self.event_storage_service or not self.action_log_service:
            # ... (error handling for missing services)
            critical_error_msg = "核心服务未初始化"  # Placeholder
            # ...
            return False, critical_error_msg

        try:
            await self.initialize_llm_clients()
        except Exception as e_init:
            # ... (error handling for LLM init failure)
            error_msg_llm_init = f"LLM客户端初始化失败: {str(e_init)}"
            # ...
            return False, error_msg_llm_init

        decision_prompt_text = ACTION_DECISION_PROMPT_TEMPLATE.format(
            current_thought_context=current_thought_context,
            action_description=action_description,
            action_motivation=action_motivation,
            relevant_adapter_messages_context=relevant_adapter_messages_context,
        )
        if not self.action_llm_client:
            # ... (error handling for missing LLM client)
            error_msg_no_client = "决策LLM客户端丢失"
            # ...
            return False, error_msg_no_client

        decision_response = await self.action_llm_client.make_llm_request(prompt=decision_prompt_text, is_stream=False)
        final_result_for_shimo: str = f"尝试执行动作 '{action_description}' 时出现未知的处理错误。"
        action_was_successful: bool = False
        tool_name_chosen: str | None = None
        tool_arguments: dict[str, Any] = {}
        # ... (rest of the LLM decision parsing logic)

        if decision_response.get("error"):
            final_result_for_shimo = f"行动决策LLM调用失败: {decision_response.get('message', '未知API错误')}"
            action_was_successful = False
        else:
            llm_raw_output_text_for_decision = decision_response.get("text", "").strip()
            if not llm_raw_output_text_for_decision:
                final_result_for_shimo = "行动决策失败，LLM的响应中不包含任何文本内容。"
                action_was_successful = False
            else:
                try:
                    # ... (JSON parsing logic)
                    json_string_to_parse = llm_raw_output_text_for_decision
                    if json_string_to_parse.startswith("```json"):
                        json_string_to_parse = json_string_to_parse[7:-3].strip()
                    elif json_string_to_parse.startswith("```"):
                        json_string_to_parse = json_string_to_parse[3:-3].strip()
                    parsed_decision = json.loads(json_string_to_parse)
                    tool_name_chosen = parsed_decision.get("tool_to_use")
                    tool_arguments = parsed_decision.get("arguments", {})
                    if not tool_name_chosen or not isinstance(tool_arguments, dict):
                        final_result_for_shimo = "LLM返回的JSON格式不正确"
                        tool_name_chosen = None
                        action_was_successful = False
                    # ...
                except Exception as e_parse:  # Simplified error handling for brevity
                    final_result_for_shimo = f"解析LLM决策JSON失败: {e_parse}"
                    tool_name_chosen = None
                    action_was_successful = False

        if tool_name_chosen:
            if tool_name_chosen.startswith("platform."):
                # ... (platform action parameter check)
                if not all([...]):  # Simplified check
                    final_result_for_shimo = "平台动作参数不足"
                    action_was_successful = False
                # ... (update thought doc with PLATFORM_ACTION_PARAM_ERROR)
                else:
                    action_to_send_to_adapter = {
                        "event_id": action_id,  # IMPORTANT: Use the action_id from the thought document
                        "event_type": tool_name_chosen,  # ... other fields
                    }
                    action_was_successful, result_message = await self._execute_platform_action(
                        action_to_send=action_to_send_to_adapter,
                        thought_doc_key=doc_key_for_updates,
                        original_action_description=action_description,  # This is the LLM's action_to_take
                    )
                    final_result_for_shimo = result_message
            else:  # Internal tool - 主人，小猫咪开始在这里小心翼翼地操作了！
                self.logger.info(f"LLM选择执行内部工具: '{tool_name_chosen}'，参数: {tool_arguments}")
                # 为内部工具调用创建一个唯一的ActionLog ID
                internal_action_log_id = f"internal_{action_id}_{tool_name_chosen}_{str(uuid.uuid4())[:8]}"

                tool_execution_successful = False
                tool_result_data: Any = None
                tool_error_message: str | None = None

                # 1. 记录工具调用尝试到 ActionLog
                if self.action_log_service:
                    await self.action_log_service.save_action_attempt(
                        action_id=internal_action_log_id,
                        action_type=tool_name_chosen,  # 工具名称作为action_type
                        timestamp=int(time.time() * 1000),
                        platform="internal_tool",  # 标记为内部工具
                        bot_id=config.persona.bot_name,
                        # 对于内部工具，conversation_id 可以是触发它的思考文档的key
                        conversation_id=doc_key_for_updates,
                        content=[{"type": "tool_parameters", "data": tool_arguments if tool_arguments else {}}],
                        original_event_id=action_id,  # 关联到触发此工具调用的原始思考动作ID
                    )
                    self.logger.info(
                        f"内部工具 '{tool_name_chosen}' (Log ID: {internal_action_log_id}) 调用尝试已记录到 ActionLog，状态：executing。"
                    )

                # 2. 执行内部工具
                try:
                    tool_function = get_tool_function(tool_name_chosen)
                    if not tool_function:
                        tool_error_message = f"未找到名为 '{tool_name_chosen}' 的内部工具。"
                        self.logger.error(tool_error_message)
                    elif not asyncio.iscoroutinefunction(tool_function):
                        tool_error_message = (
                            f"工具 '{tool_name_chosen}' 不是一个异步函数 (coroutine function)，无法在此处异步调用。"
                        )
                        self.logger.error(tool_error_message)
                    else:
                        self.logger.info(f"正在执行内部工具 '{tool_name_chosen}' (Log ID: {internal_action_log_id})...")
                        if tool_arguments:  # 确保参数不为空时才解包
                            tool_result_data = await tool_function(**tool_arguments)
                        else:
                            tool_result_data = await tool_function()
                        tool_execution_successful = True
                        self.logger.info(f"内部工具 '{tool_name_chosen}' (Log ID: {internal_action_log_id}) 执行成功。")

                except Exception as e_tool_exec:
                    tool_error_message = f"执行内部工具 '{tool_name_chosen}' (Log ID: {internal_action_log_id}) 时发生错误: {str(e_tool_exec)}"
                    self.logger.error(tool_error_message, exc_info=True)
                    tool_execution_successful = False
                    tool_result_data = None

                # 3. 更新 ActionLog 中的工具调用结果
                if self.action_log_service:
                    current_ts_for_log_update = int(time.time() * 1000)
                    await self.action_log_service.update_action_log_with_response(
                        action_id=internal_action_log_id,
                        status="success" if tool_execution_successful else "failure",
                        response_timestamp=current_ts_for_log_update,
                        result_details={"output": tool_result_data}
                        if tool_execution_successful and tool_result_data is not None
                        else None,
                        error_info=tool_error_message if not tool_execution_successful else None,
                    )
                    self.logger.info(
                        f"内部工具 '{tool_name_chosen}' (Log ID: {internal_action_log_id}) 的执行结果已更新到 ActionLog。"
                    )

                # 4. 准备更新思考文档的结果
                if tool_execution_successful:
                    action_was_successful = True
                    if tool_name_chosen == "search_web" and tool_result_data and self.summary_llm_client:
                        self.logger.info(
                            f"内部工具 '{tool_name_chosen}' (Log ID: {internal_action_log_id}) 的结果需要总结。"
                        )
                        final_result_for_shimo = await self._summarize_tool_result_async(
                            original_query=tool_arguments.get("query", action_description),
                            original_motivation=action_motivation,
                            tool_output=tool_result_data,
                        )
                    elif tool_result_data is not None:
                        try:
                            final_result_for_shimo = f"内部工具 '{tool_name_chosen}' 执行成功。结果: {json.dumps(tool_result_data, ensure_ascii=False, indent=2, default=str)}"
                        except TypeError:
                            final_result_for_shimo = (
                                f"内部工具 '{tool_name_chosen}' 执行成功。结果: {str(tool_result_data)}"
                            )
                    else:
                        final_result_for_shimo = f"内部工具 '{tool_name_chosen}' 执行成功，但未返回具体数据。"
                else:
                    action_was_successful = False
                    final_result_for_shimo = tool_error_message or f"内部工具 '{tool_name_chosen}' 执行失败，原因未知。"

                # 更新思考文档
                if self.thought_storage_service:
                    update_payload_for_thought = {
                        "status": "COMPLETED_SUCCESS" if action_was_successful else "COMPLETED_FAILURE",
                        "final_result_for_shimo": final_result_for_shimo,
                        "error_message": tool_error_message if not action_was_successful else None,
                        "response_received_at": int(time.time() * 1000),
                    }
                    await self.thought_storage_service.update_action_status_in_thought_document(
                        doc_key_for_updates,
                        action_id,  # 这是主思考动作的ID
                        update_payload_for_thought,
                    )
                    self.logger.info(
                        f"已更新思考文档 '{doc_key_for_updates}' 中关于内部工具 '{tool_name_chosen}' (关联主动作ID: {action_id}) 的执行结果。"
                    )

                # 5. 预留的 Event 迁移接口
                if self.action_log_service:  # 确保服务存在
                    action_log_entry = await self.action_log_service.get_action_log(internal_action_log_id)
                    if action_log_entry:
                        await self._maybe_log_internal_tool_action_to_events(action_log_entry)
        elif not tool_name_chosen:  # LLM did not choose a tool
            # ... (update thought doc with NO_TOOL_CHOSEN_FAILURE)
            pass  # Placeholder

        # ... (trigger thought logic)
        self.logger.info(f"--- [Action ID: {action_id}] 行动流程结束 ---")
        return action_was_successful, final_result_for_shimo
