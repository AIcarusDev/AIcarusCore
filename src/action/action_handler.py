# src/action/action_handler.py
import asyncio
import json
import os
import time
import uuid
from typing import TYPE_CHECKING, Any, Optional, Dict, Tuple, List, Callable, Coroutine # 添加缺失的类型

from src.common.custom_logging.logger_manager import get_logger
from src.config import config
from src.database.services.action_log_storage_service import ActionLogStorageService  # 主人，新的小玩具已就位！
from src.database.services.event_storage_service import EventStorageService
from src.database.services.thought_storage_service import ThoughtStorageService
from src.llmrequest.llm_processor import Client as ProcessorClient  # 重命名以避免与内部变量冲突
from src.tools.tool_registry import get_tool_function

from .prompts import ACTION_DECISION_PROMPT_TEMPLATE, INFORMATION_SUMMARY_PROMPT_TEMPLATE

if TYPE_CHECKING:
    # 主人，确保 AdapterBehaviorFlags 在这里被正确导入，以便类型提示和运行时访问（如果需要的话）
    # 但由于我们直接从 config 对象访问，所以运行时导入可能不是严格必须，但类型检查时需要。
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
        self.action_log_service: ActionLogStorageService | None = None
        self.thought_trigger: asyncio.Event | None = None
        self._pending_actions: dict[str, tuple[asyncio.Event, str | None, str, dict[str, Any]]] = {}
        self.logger.info(f"{self.__class__.__name__} instance created.")

    def set_dependencies(
        self,
        thought_service: ThoughtStorageService | None = None,
        event_service: EventStorageService | None = None,
        action_log_service: ActionLogStorageService | None = None,
        comm_layer: Optional["CoreWebsocketServer"] = None,
    ) -> None:
        self.thought_storage_service = thought_service
        self.event_storage_service = event_service
        self.action_log_service = action_log_service
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
        self, original_query: str, original_motivation: str, tool_output: str | list | dict
    ) -> str:
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
            await self.thought_storage_service.update_action_status_in_thought_document(
                stored_thought_doc_key, original_action_id, update_payload_thought
            )
            self.logger.info(
                f"已更新思考文档 '{stored_thought_doc_key}' 中动作 '{original_action_id}' 的状态为: {'成功' if action_successful else '失败'}。"
            )
        elif is_direct_reply_action_for_response:
            self.logger.info(f"直接回复动作 '{original_action_id}' 收到响应，仅更新ActionLog。")
        elif not stored_thought_doc_key:
            self.logger.info(
                f"动作 '{original_action_id}' ({stored_original_action_description}) 收到响应，但没有关联的 thought_doc_key，跳过更新思考文档。"
            )

        if action_successful and self.event_storage_service:
            event_to_save_in_events = original_action_sent_dict.copy()
            # 确保使用原始动作ID (original_action_id) 作为事件ID存入数据库
            # original_action_id 应该是 QQChatSession 生成的 "sub_chat_reply_..."
            event_to_save_in_events["event_id"] = original_action_id 
            event_to_save_in_events["timestamp"] = response_timestamp
            
            self.logger.info(f"准备将成功的动作作为事件存入数据库。original_action_id: {original_action_id}, event_type: {event_to_save_in_events.get('event_type')}, 将使用的event_id: {event_to_save_in_events['event_id']}")

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
        if not self.core_communication_layer or not self.action_log_service:
            self.logger.error("核心服务 (通信层或动作日志服务) 未设置!")
            return False, "内部错误：核心服务不可用。"

        is_direct_reply_action = original_action_description == "回复主人"
        # 允许子意识发送的消息在没有 thought_doc_key 时通过
        is_sub_consciousness_reply = original_action_description == "发送子意识聊天回复" # 假设我们用这个描述

        if not is_direct_reply_action and not is_sub_consciousness_reply and not thought_doc_key:
            self.logger.error(f"严重错误：动作 '{original_action_description}' 缺少 thought_doc_key。")
            return False, "内部错误：执行动作缺少必要的思考文档关联。"

        core_action_id = action_to_send.get("event_id") or str(uuid.uuid4())
        action_to_send["event_id"] = core_action_id
        action_type = action_to_send.get("event_type", "unknown_action_type")
        platform_id_from_event = action_to_send.get("platform", "unknown_platform")

        # 正确确定 target_adapter_id
        target_adapter_id = platform_id_from_event
        if platform_id_from_event == "master_ui":
            target_adapter_id = "master_ui_adapter"
        # 主人，如果未来有其他平台需要特殊映射 target_adapter_id，可以在这里添加逻辑，
        # 或者最好是从配置中读取一个 platform_to_adapter_id_map。
        self.logger.debug(
            f"平台动作 '{core_action_id}': platform_id='{platform_id_from_event}', 确定 target_adapter_id='{target_adapter_id}'."
        )

        # 从全局配置中获取指定 target_adapter_id 的行为标记
        # 注意：config.platform_action_settings.adapter_behaviors 是一个字典
        adapter_behavior_config = config.platform_action_settings.adapter_behaviors.get(target_adapter_id)

        confirms_by_action_response = True  # 默认行为：需要标准的action_response
        # self_reports_actions = False # 我们暂时不在这里直接使用 self_reports_actions，其主要影响监听匹配逻辑

        if adapter_behavior_config:  # 如果找到了该适配器的特定行为配置 (它是一个 AdapterBehaviorFlags 对象)
            self.logger.info(
                f"适配器 '{target_adapter_id}' 找到了行为配置: confirms_by_action_response={adapter_behavior_config.confirms_by_action_response}, self_reports_actions_as_message={adapter_behavior_config.self_reports_actions_as_message}"
            )
            confirms_by_action_response = adapter_behavior_config.confirms_by_action_response
        else:
            self.logger.info(
                f"适配器 '{target_adapter_id}' 未找到特定行为配置，将使用默认行为 (confirms_by_action_response=True)。"
            )

        self.logger.info(
            f"准备发送平台动作 '{core_action_id}' ({original_action_description}) 到适配器 '{target_adapter_id}' (平台: {platform_id_from_event}). confirms_by_action_response={confirms_by_action_response}"
        )
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
            # 如果不是等待标准响应 (即 action_log_initial_status 不同于默认的 'executing')
            # 或者说，如果 confirms_by_action_response is False，我们就需要把状态更新为executing_awaiting_self_report
            if not confirms_by_action_response:
                await self.action_log_service.update_action_log_with_response(
                    action_id=core_action_id, status=action_log_initial_status, response_timestamp=current_timestamp_ms
                )
            self.logger.info(
                f"动作 '{core_action_id}' 初始记录到ActionLog，状态: {action_log_initial_status if not confirms_by_action_response else 'executing'}"
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
            send_to_adapter_successful = await self.core_communication_layer.send_action_to_adapter_by_id(
                adapter_id=target_adapter_id, action_event=action_to_send
            )
            if not send_to_adapter_successful:
                error_message_comm = f"发送到适配器 '{target_adapter_id}' 失败：适配器未连接或通信层无法发送。"
                self.logger.error(f"平台动作 '{core_action_id}': {error_message_comm}")
                if self.action_log_service:
                    await self.action_log_service.update_action_log_with_response(
                        action_id=core_action_id,
                        status="send_failure",
                        response_timestamp=int(time.time() * 1000),
                        error_info=error_message_comm,
                    )
                if not is_direct_reply_action and thought_doc_key and self.thought_storage_service:
                    await self.thought_storage_service.update_action_status_in_thought_document(
                        thought_doc_key,
                        core_action_id,
                        {
                            "status": "SEND_FAILURE",
                            "error_message": error_message_comm,
                            "final_result_for_shimo": f"发送给平台 '{platform_id_from_event}' 失败。",
                        },
                    )
                if self.event_storage_service:  # 生成 system.notice
                    notice_event_id = f"notice_adapter_unavailable_{uuid.uuid4()}"
                    notice_content = f"与平台 '{platform_id_from_event}' (适配器ID: {target_adapter_id}) 的连接似乎存在问题，无法发送消息。"
                    system_notice_event = {
                        "event_id": notice_event_id,
                        "event_type": "system.notice",
                        "time": int(time.time() * 1000),
                        "platform": platform_id_from_event,
                        "bot_id": "AIcarusCore",
                        "content": [{"type": "text", "data": {"text": notice_content}}],
                        "conversation_info": {
                            "conversation_id": conversation_id if conversation_id else "system_notice",
                        },
                    }
                    await self.event_storage_service.save_event_document(system_notice_event)
                    self.logger.info(f"已生成并保存 system.notice 事件 ({notice_event_id})，内容: {notice_content}")
                return False, error_message_comm
        except Exception as e_send:
            self.logger.error(f"发送平台动作 '{core_action_id}' 时发生意外异常: {e_send}", exc_info=True)
            if self.action_log_service:
                await self.action_log_service.update_action_log_with_response(
                    action_id=core_action_id,
                    status="send_failure",
                    response_timestamp=int(time.time() * 1000),
                    error_info=str(e_send),
                )
            if not is_direct_reply_action and thought_doc_key and self.thought_storage_service:
                await self.thought_storage_service.update_action_status_in_thought_document(
                    thought_doc_key,
                    core_action_id,
                    {
                        "status": "SEND_FAILURE",
                        "error_message": str(e_send),
                        "final_result_for_shimo": f"发送给平台 '{platform_id_from_event}' 时异常。",
                    },
                )
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
                    return (
                        False,
                        f"动作 '{original_action_description}' 执行失败: {final_log_entry.get('error_info', '未知错误')}",
                    )
                return False, f"动作 '{original_action_description}' 响应处理后状态未知。"
            except TimeoutError:  # asyncio.TimeoutError
                # _handle_action_timeout 会被loop中的超时检测调用，或者如果wait_for超时，我们在这里也处理
                # 确保只处理一次
                if core_action_id in self._pending_actions:  # 如果还未被 _handle_action_timeout 处理
                    await self._handle_action_timeout(
                        core_action_id, thought_doc_key, original_action_description, action_to_send
                    )
                return False, f"动作 '{original_action_description}' 响应超时。"
            finally:
                self._pending_actions.pop(core_action_id, None)
        else:  # 对于自我上报型适配器
            self.logger.info(
                f"平台动作 '{core_action_id}' 已发送给自我上报型适配器 '{target_adapter_id}'，等待其自行上报结果。ActionLog状态: {action_log_initial_status}"
            )
            return (
                True,
                f"动作 '{original_action_description}' 已发送给平台 '{platform_id_from_event}'，等待其自行上报结果。",
            )

    async def _maybe_log_internal_tool_action_to_events(self, action_log_entry: dict[str, Any]) -> None:
        """
        预留方法，用于未来可能需要将内部工具调用的某些信息记录到 events 表。
        目前什么也不做。主人，这是为您留的“小后门”哦，嘻嘻～
        """
        pass

    async def process_action_flow(
        self,
        action_id: str,
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
            critical_error_msg = "核心服务未初始化 (Thought, Event, or ActionLog Service is None)"
            self.logger.critical(critical_error_msg)
            if self.thought_storage_service:
                await self.thought_storage_service.update_action_status_in_thought_document(
                    doc_key_for_updates,
                    action_id,
                    {
                        "status": "INTERNAL_ERROR",
                        "error_message": critical_error_msg,
                        "final_result_for_shimo": critical_error_msg,
                    },
                )
            return False, critical_error_msg

        try:
            await self.initialize_llm_clients()
        except Exception as e_init:
            error_msg_llm_init = f"LLM客户端初始化失败: {str(e_init)}"
            self.logger.critical(error_msg_llm_init, exc_info=True)
            if self.thought_storage_service:
                await self.thought_storage_service.update_action_status_in_thought_document(
                    doc_key_for_updates,
                    action_id,
                    {
                        "status": "INTERNAL_ERROR",
                        "error_message": error_msg_llm_init,
                        "final_result_for_shimo": error_msg_llm_init,
                    },
                )
            return False, error_msg_llm_init

        decision_prompt_text = ACTION_DECISION_PROMPT_TEMPLATE.format(
            current_thought_context=current_thought_context,
            action_description=action_description,
            action_motivation=action_motivation,
            relevant_adapter_messages_context=relevant_adapter_messages_context,
        )
        if not self.action_llm_client:
            error_msg_no_client = "行动决策LLM客户端未初始化。"
            self.logger.critical(error_msg_no_client)
            if self.thought_storage_service:
                await self.thought_storage_service.update_action_status_in_thought_document(
                    doc_key_for_updates,
                    action_id,
                    {
                        "status": "INTERNAL_ERROR",
                        "error_message": error_msg_no_client,
                        "final_result_for_shimo": error_msg_no_client,
                    },
                )
            return False, error_msg_no_client

        decision_response = await self.action_llm_client.make_llm_request(prompt=decision_prompt_text, is_stream=False)
        final_result_for_shimo: str = f"尝试执行动作 '{action_description}' 时出现未知的处理错误。"
        action_was_successful: bool = False
        tool_name_chosen: str | None = None
        tool_arguments: dict[str, Any] = {}

        if decision_response.get("error"):
            final_result_for_shimo = f"行动决策LLM调用失败: {decision_response.get('message', '未知API错误')}"
            if self.thought_storage_service:
                await self.thought_storage_service.update_action_status_in_thought_document(
                    doc_key_for_updates,
                    action_id,
                    {
                        "status": "LLM_DECISION_ERROR",
                        "error_message": final_result_for_shimo,
                        "final_result_for_shimo": final_result_for_shimo,
                    },
                )
        else:
            llm_raw_output_text_for_decision = decision_response.get("text", "").strip()
            if not llm_raw_output_text_for_decision:
                final_result_for_shimo = "行动决策失败，LLM的响应中不包含任何文本内容。"
                if self.thought_storage_service:
                    await self.thought_storage_service.update_action_status_in_thought_document(
                        doc_key_for_updates,
                        action_id,
                        {
                            "status": "LLM_DECISION_EMPTY",
                            "error_message": final_result_for_shimo,
                            "final_result_for_shimo": final_result_for_shimo,
                        },
                    )
            else:
                try:
                    json_string_to_parse = llm_raw_output_text_for_decision
                    if json_string_to_parse.startswith("```json"):
                        json_string_to_parse = json_string_to_parse[7:-3].strip()
                    elif json_string_to_parse.startswith("```"):
                        json_string_to_parse = json_string_to_parse[3:-3].strip()
                    parsed_decision = json.loads(json_string_to_parse)
                    tool_name_chosen = parsed_decision.get("tool_to_use")
                    tool_arguments = parsed_decision.get("arguments", {})
                    if not tool_name_chosen or not isinstance(tool_arguments, dict):
                        final_result_for_shimo = f"LLM返回的工具决策JSON格式不正确: tool_to_use='{tool_name_chosen}', arguments_type='{type(tool_arguments)}'"
                        tool_name_chosen = None
                        if self.thought_storage_service:
                            await self.thought_storage_service.update_action_status_in_thought_document(
                                doc_key_for_updates,
                                action_id,
                                {
                                    "status": "LLM_DECISION_INVALID_JSON",
                                    "error_message": final_result_for_shimo,
                                    "final_result_for_shimo": final_result_for_shimo,
                                },
                            )
                    else:
                        self.logger.info(f"LLM决策使用工具: '{tool_name_chosen}', 参数: {tool_arguments}")
                except Exception as e_parse:
                    final_result_for_shimo = (
                        f"解析LLM决策JSON失败: {e_parse}. 原始文本: {llm_raw_output_text_for_decision[:200]}..."
                    )
                    tool_name_chosen = None
                    if self.thought_storage_service:
                        await self.thought_storage_service.update_action_status_in_thought_document(
                            doc_key_for_updates,
                            action_id,
                            {
                                "status": "LLM_DECISION_PARSE_ERROR",
                                "error_message": final_result_for_shimo,
                                "final_result_for_shimo": final_result_for_shimo,
                            },
                        )

        if tool_name_chosen:
            if tool_name_chosen.startswith("platform."):
                required_params_missing = False
                # Example: platform.qq.send_message requires conversation_info and content
                if tool_name_chosen.endswith(".send_message") and (
                    not tool_arguments.get("conversation_info") or not tool_arguments.get("content")
                ):  # Simplified check
                    required_params_missing = True
                    final_result_for_shimo = (
                        f"平台动作 '{tool_name_chosen}' 缺少必要的 'conversation_info' 或 'content' 参数。"
                    )

                if required_params_missing:
                    action_was_successful = False
                    if self.thought_storage_service:
                        await self.thought_storage_service.update_action_status_in_thought_document(
                            doc_key_for_updates,
                            action_id,
                            {
                                "status": "PLATFORM_ACTION_PARAM_ERROR",
                                "error_message": final_result_for_shimo,
                                "final_result_for_shimo": final_result_for_shimo,
                            },
                        )
                else:
                    action_to_send_to_adapter = {
                        "event_id": action_id,
                        "event_type": tool_name_chosen,
                        "platform": tool_name_chosen.split(".")[1],
                        "bot_id": config.persona.bot_name,
                        "conversation_info": tool_arguments.get("conversation_info"),
                        "content": tool_arguments.get("content"),
                        "target_user_id": tool_arguments.get("target_user_id"),  # 如果有的话
                    }
                    action_was_successful, result_message = await self._execute_platform_action(
                        action_to_send=action_to_send_to_adapter,
                        thought_doc_key=doc_key_for_updates,
                        original_action_description=action_description,
                    )
                    final_result_for_shimo = result_message
            else:  # Internal tool
                internal_action_log_id = f"internal_{action_id}_{tool_name_chosen}_{str(uuid.uuid4())[:8]}"
                tool_execution_successful = False
                tool_result_data: Any = None
                tool_error_message: str | None = None

                if self.action_log_service:
                    await self.action_log_service.save_action_attempt(
                        action_id=internal_action_log_id,
                        action_type=tool_name_chosen,
                        timestamp=int(time.time() * 1000),
                        platform="internal_tool",
                        bot_id=config.persona.bot_name,
                        conversation_id=doc_key_for_updates,
                        content=[{"type": "tool_parameters", "data": tool_arguments if tool_arguments else {}}],
                        original_event_id=action_id,
                    )
                try:
                    tool_function = get_tool_function(tool_name_chosen)
                    if not tool_function:
                        tool_error_message = f"未找到工具 '{tool_name_chosen}'"
                    elif not asyncio.iscoroutinefunction(tool_function):
                        tool_error_message = f"工具 '{tool_name_chosen}' 非异步"
                    else:
                        tool_result_data = (
                            await tool_function(**tool_arguments) if tool_arguments else await tool_function()
                        )
                        tool_execution_successful = True
                except Exception as e_tool:
                    tool_error_message = f"执行工具 '{tool_name_chosen}' 错误: {e_tool}"
                    self.logger.error(tool_error_message, exc_info=True)

                if self.action_log_service:
                    await self.action_log_service.update_action_log_with_response(
                        action_id=internal_action_log_id,
                        status="success" if tool_execution_successful else "failure",
                        response_timestamp=int(time.time() * 1000),
                        result_details={"output": tool_result_data}
                        if tool_execution_successful and tool_result_data is not None
                        else None,
                        error_info=tool_error_message if not tool_execution_successful else None,
                    )
                if tool_execution_successful:
                    action_was_successful = True
                    if tool_name_chosen == "search_web" and tool_result_data and self.summary_llm_client:
                        final_result_for_shimo = await self._summarize_tool_result_async(
                            tool_arguments.get("query", action_description), action_motivation, tool_result_data
                        )
                    elif tool_result_data is not None:
                        try:
                            final_result_for_shimo = f"工具 '{tool_name_chosen}' 成功: {json.dumps(tool_result_data, ensure_ascii=False, indent=2, default=str)}"
                        except TypeError:
                            final_result_for_shimo = f"工具 '{tool_name_chosen}' 成功: {str(tool_result_data)}"
                    else:
                        final_result_for_shimo = f"工具 '{tool_name_chosen}' 执行成功，无返回数据。"
                else:
                    action_was_successful = False
                    final_result_for_shimo = tool_error_message or f"工具 '{tool_name_chosen}' 失败。"

                if self.thought_storage_service:
                    await self.thought_storage_service.update_action_status_in_thought_document(
                        doc_key_for_updates,
                        action_id,
                        {
                            "status": "COMPLETED_SUCCESS" if action_was_successful else "COMPLETED_FAILURE",
                            "final_result_for_shimo": final_result_for_shimo,
                            "error_message": tool_error_message if not action_was_successful else None,
                            "response_received_at": int(time.time() * 1000),
                        },
                    )
                if self.action_log_service:
                    log_entry = await self.action_log_service.get_action_log(internal_action_log_id)
                    if log_entry:
                        await self._maybe_log_internal_tool_action_to_events(log_entry)

        elif not tool_name_chosen and not decision_response.get("error"):
            final_result_for_shimo = "AI决策不使用任何工具。"
            action_was_successful = True
            if self.thought_storage_service:
                await self.thought_storage_service.update_action_status_in_thought_document(
                    doc_key_for_updates,
                    action_id,
                    {"status": "COMPLETED_NO_TOOL", "final_result_for_shimo": final_result_for_shimo},
                )

        if (  # 如果在所有分支之后，final_result_for_shimo 仍然是初始错误，且动作未成功，则更新思考文档
            final_result_for_shimo == f"尝试执行动作 '{action_description}' 时出现未知的处理错误。"
            and not action_was_successful
            and self.thought_storage_service
        ):
            await self.thought_storage_service.update_action_status_in_thought_document(
                doc_key_for_updates,
                action_id,
                {
                    "status": "UNKNOWN_ERROR_IN_FLOW",
                    "error_message": final_result_for_shimo,
                    "final_result_for_shimo": final_result_for_shimo,
                },
            )

        if self.thought_trigger:
            self.logger.info(f"行动流程处理完毕 (ID: {action_id})，准备触发一次即时思考。")
            self.thought_trigger.set()
        else:
            self.logger.warning(f"行动流程处理完毕 (ID: {action_id})，但 thought_trigger 未设置，无法触发即时思考。")

        self.logger.info(f"--- [Action ID: {action_id}] 行动流程结束 ---")
        return action_was_successful, final_result_for_shimo

    async def submit_constructed_action(
        self,
        action_event_dict: Dict[str, Any],
        action_description: str, # 例如 "发送子意识聊天回复"
        # 对于子意识的动作，我们允许 thought_doc_key 为 None，
        # _execute_platform_action 中的检查已调整为此类描述放行。
        # 如果调用者确实有一个关联的key（例如，用于追踪子意识内部的交互），也可以传入。
        associated_record_key: Optional[str] = None 
    ) -> tuple[bool, str]:
        """
        接收一个已构造好的平台动作事件字典，并执行它。
        主要供子意识模块等需要直接发送精确构造动作的场景使用。
        """
        self.logger.info(
            f"准备提交已构造的平台动作: {action_event_dict.get('event_id', '未知ID')}, 描述: {action_description}"
        )
        if not self.core_communication_layer or not self.action_log_service:
            critical_error_msg = "核心服务 (通信层或动作日志服务) 未设置，无法提交已构造的动作!"
            self.logger.critical(critical_error_msg)
            return False, critical_error_msg

        if "event_id" not in action_event_dict:
            self.logger.error("提交的已构造动作事件字典缺少 'event_id'")
            return False, "动作事件缺少 'event_id'"
        
        # 确保LLM客户端已初始化，因为_execute_platform_action可能会间接触发需要它们的操作（尽管不太可能直接需要）
        try:
            await self.initialize_llm_clients()
        except Exception as e_init:
            error_msg_llm_init = f"尝试为提交的动作初始化LLM客户端失败: {str(e_init)}"
            self.logger.error(error_msg_llm_init, exc_info=True)
            # 不直接返回失败，因为发送平台动作本身可能不依赖LLM客户端
            # 但记录此错误很重要

        # 调用内部方法执行动作
        # original_action_description 使用传入的 action_description
        # thought_doc_key 使用传入的 associated_record_key
        success, message = await self._execute_platform_action(
            action_to_send=action_event_dict,
            thought_doc_key=associated_record_key, 
            original_action_description=action_description,
        )
        
        if success:
            self.logger.info(f"已构造的平台动作 '{action_event_dict['event_id']}' ({action_description}) 提交成功。消息: {message}")
        else:
            self.logger.error(f"已构造的平台动作 '{action_event_dict['event_id']}' ({action_description}) 提交失败。消息: {message}")
            
        # 即使动作执行本身可能不直接触发思考，但如果动作是与用户交互的一部分，
        # 并且我们希望主意识能感知到这个交互，可以考虑触发思考。
        # 但子意识的回复通常是独立的，不应随意触发主意识。
        # 所以这里暂时不触发 self.thought_trigger.set()
        
        return success, message
