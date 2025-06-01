# src/action/action_handler.py
import asyncio
import json
import os
import time
import uuid
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

# --- For Core Communication ---
from aicarus_protocols import BaseMessageInfo, GroupInfo, MessageBase, Seg, UserInfo
from arango.database import StandardDatabase  # type: ignore

from src.common.custom_logging.logger_manager import get_logger
from src.config.alcarus_configs import (
    AlcarusRootConfig,
    LLMClientSettings,
    ModelParams,
    ProxySettings,
)
from src.config.config_manager import get_typed_settings
from src.core_communication.core_ws_server import CoreWebsocketServer  # To send actions
from src.database import arangodb_handler
from src.llmrequest.llm_processor import Client as ProcessorClient
from src.tools.failure_reporter import report_action_failure
from src.tools.web_searcher import search_web

if TYPE_CHECKING:
    pass

logger = get_logger("AIcarusCore.action_handler")
action_llm_client: ProcessorClient | None = None
summary_llm_client: ProcessorClient | None = None

core_communication_layer_for_actions: CoreWebsocketServer | None = None


def set_core_communication_layer_for_actions(comm_layer: CoreWebsocketServer) -> None:
    """Sets the communication layer instance for this module."""
    global core_communication_layer_for_actions
    core_communication_layer_for_actions = comm_layer
    logger.info("Action Handler: Core communication layer has been set.")


AVAILABLE_TOOLS_SCHEMA_FOR_GEMINI = [
    {
        "function_declarations": [
            {
                "name": "web_search",
                "description": "当需要从互联网查找最新信息、具体事实、定义、解释或任何当前未知的内容时使用此工具。例如，搜索特定主题、新闻、人物、地点、科学概念等。",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string", "description": "要搜索的关键词或问题。"}},
                    "required": ["query"],
                },
            },
            {
                "name": "report_action_failure",
                "description": "当一个明确提出的行动意图因为没有合适的工具、工具执行失败或其他原因而无法完成时，使用此工具来生成一个反馈信息。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "reason_for_failure_short": {
                            "type": "string",
                            "description": "对动作失败原因的简短说明，例如 '没有找到合适的工具来执行此操作' 或 '用户意图不清晰'。",
                        }
                    },
                    "required": ["reason_for_failure_short"],
                },
            },
            {
                "name": "send_reply_message_to_adapter",
                "description": "当需要通过适配器向用户发送回复消息时使用此工具。例如，回答用户的问题，或在执行完一个动作后通知用户。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "target_user_id": {"type": "string", "description": "目标用户的ID (如果是私聊回复)。"},
                        "target_group_id": {"type": "string", "description": "目标群组的ID (如果是群聊回复)。"},
                        "message_content_text": {"type": "string", "description": "要发送的纯文本消息内容。"},
                        "reply_to_message_id": {
                            "type": "string",
                            "description": "[可选] 如果是回复特定消息，请提供原始消息的ID。",
                        },
                    },
                    "required": ["message_content_text"],
                },
            },
            {
                "name": "handle_platform_request_internally",
                "description": "当收到平台请求（如好友请求、加群邀请）并且需要决定是否同意或拒绝时，使用此工具。这会触发内部逻辑来向适配器发送标准化的处理指令。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "request_type": {
                            "type": "string",
                            "description": "请求的类型，例如 'friend_add' 或 'group_join_application' 或 'group_invite_received'。",
                        },
                        "request_flag": {
                            "type": "string",
                            "description": "从原始平台请求中获取的、用于响应的唯一标识。",
                        },
                        "approve_action": {
                            "type": "boolean",
                            "description": "是否同意请求 (true 表示同意, false 表示拒绝)。",
                        },
                        "remark_or_reason": {
                            "type": "string",
                            "description": "[可选] 如果是同意好友请求，则为备注名；如果是拒绝群请求，则为拒绝理由。",
                        },
                    },
                    "required": ["request_type", "request_flag", "approve_action"],
                },
            },
        ]
    }
]

ACTION_DECISION_PROMPT_TEMPLATE = """你是一个智能行动辅助系统。你的主要任务是分析用户当前的思考、他们明确提出的行动意图以及背后的动机，以及最近收到的外部消息和请求。根据这些信息，你需要从下方提供的可用工具列表中，选择一个最合适的工具来帮助用户完成这个行动，或者判断行动是否无法完成。

请参考以下信息来进行决策：

可用工具列表（以JSON Schema格式描述）：
{tools_json_string}

用户当前的思考上下文：
"{current_thought_context}"

用户明确想做的动作（原始意图描述）：
"{action_description}"

用户的动机（原始行动动机）：
"{action_motivation}"

最近可能相关的外部消息或请求 (如果适用):
{relevant_adapter_messages_context}

你的决策应遵循以下步骤：
1.  仔细理解用户想要完成的动作、他们为什么想做这个动作，以及他们此刻正在思考什么，同时考虑是否有外部消息或请求需要响应。
2.  然后，查看提供的工具列表，判断是否有某个工具的功能与用户的行动意图或响应外部请求的需求相匹配。
    - 如果用户的意图是回复收到的消息，请使用 "send_reply_message_to_adapter" 工具。你需要从思考上下文中提取出原始消息的发送者ID (target_user_id)、群ID (target_group_id, 如果是群消息)、以及可能的原始消息ID (reply_to_message_id)。
    - 如果用户的意图是处理平台请求 (例如，思考中提到“同意XX的好友请求”)，请使用 "handle_platform_request_internally" 工具。你需要从思考上下文或最近的外部请求信息中找到对应的 request_type 和 request_flag。
3.  如果找到了能够满足用户意图的工具（例如 "web_search", "send_reply_message_to_adapter", "handle_platform_request_internally"），请选择它，并为其准备好准确的调用参数。你的输出需要是一个包含 "tool_calls" 列表的JSON对象字符串。这个列表中的每个对象都描述了一个工具调用，应包含 "id"（可以是一个唯一的调用标识，例如 "call_工具名_随机串"），"type" 固定为 "function"，以及 "function" 对象（包含 "name": "工具的实际名称" 和 "arguments": "一个包含所有必需参数的JSON字符串"）。
4.  如果经过分析，你认为用户提出的动作意图非常模糊，或者现有的任何工具都无法实现它，或者这个意图本质上不需要外部工具（例如，用户只是想表达一个无法具体行动化的愿望），那么，请选择调用名为 "report_action_failure" 的工具。
    -   在调用 "report_action_failure" 时，你只需要为其 "function" 的 "arguments" 准备一个可选的参数：
        * "reason_for_failure_short": 简要说明为什么这个动作无法通过其他工具执行，例如 "系统中没有找到能够执行此操作的工具" 或 "用户的意图似乎不需要借助外部工具来实现"。
5.  请确保你的最终输出**都必须**是一个包含 "tool_calls" 字段的JSON对象字符串。即使没有合适的工具（此时应选择 "report_action_failure"），也需要按此格式输出。

现在，请根据以上信息，直接输出你决定调用的工具及其参数的JSON对象字符串：
"""

INFORMATION_SUMMARY_PROMPT_TEMPLATE = """你是一个高效的信息处理和摘要助手。你的任务是为用户处理和总结来自外部工具的信息。

**用户获取这些信息的原始意图：**
* 原始查询/动作描述: "{original_query_or_action}"
* 当时的动机: "{original_motivation}"

**来自工具的原始信息输出：**
--- BEGIN RAW INFORMATION ---
{raw_tool_output}
--- END RAW INFORMATION ---

**你的任务：**
1.  仔细阅读并理解上述原始信息。
2.  结合用户的原始查询/动作和动机，判断哪些信息是对她最有价值和最相关的。
3.  生成一段**简洁明了的摘要**，字数控制在400字以内。
4.  摘要应直接回答或满足用户的原始意图，突出核心信息点。
5.  如果原始信息包含多个结果，请尝试整合关键内容，避免简单罗列。
6.  如果原始信息质量不高、不相关或未能找到有效信息，请在摘要中客观反映这一点（例如：“关于'{original_query_or_action}'的信息较少，主要发现有...”或“未能从提供的信息中找到关于'{original_query_or_action}'的直接答案。”）。
7.  摘要的语言风格应自然、易于理解，就像是用户自己整理得到的一样。

请输出你生成的摘要文本：
"""


def _create_llm_client_from_config(
    purpose_key: str,
    default_provider_name: str,
    root_cfg: AlcarusRootConfig,
) -> ProcessorClient | None:
    # ... (内容保持不变) ...
    try:
        if root_cfg.providers is None:
            logger.error("配置错误：AlcarusRootConfig 中缺少 'providers' 配置段。")
            return None

        provider_settings = getattr(root_cfg.providers, default_provider_name.lower(), None)
        if provider_settings is None or provider_settings.models is None:
            logger.error(
                f"配置错误：在 AlcarusRootConfig.providers 下未找到提供商 '{default_provider_name}' 的有效配置或其 'models' 配置段。"
            )
            return None

        model_params_cfg = getattr(provider_settings.models, purpose_key, None)
        if not isinstance(model_params_cfg, ModelParams):
            logger.error(
                f"配置错误：在提供商 '{default_provider_name}' 的 models 配置下未找到模型用途键 '{purpose_key}' 对应的有效 ModelParams 配置，或类型不匹配。"
            )
            return None

        actual_provider_name_str: str = model_params_cfg.provider
        actual_model_name_str: str = model_params_cfg.model_name

        if not actual_provider_name_str or not actual_model_name_str:
            logger.error(
                f"配置错误：模型 '{purpose_key}' (提供商: {actual_provider_name_str or '未知'}) 未指定 'provider' 或 'model_name'。"
            )
            return None

        general_llm_settings_obj: LLMClientSettings = root_cfg.llm_client_settings
        resolved_abandoned_keys: list[str] | None = None
        env_val_abandoned = os.getenv("LLM_ABANDONED_KEYS")
        if env_val_abandoned:
            try:
                keys_from_env = json.loads(env_val_abandoned)
                if isinstance(keys_from_env, list):
                    resolved_abandoned_keys = [str(k).strip() for k in keys_from_env if str(k).strip()]
            except json.JSONDecodeError:
                logger.warning(
                    f"环境变量 'LLM_ABANDONED_KEYS' 的值不是有效的JSON列表，将尝试按逗号分隔。值: {env_val_abandoned[:50]}..."
                )
                resolved_abandoned_keys = [k.strip() for k in env_val_abandoned.split(",") if k.strip()]
            if not resolved_abandoned_keys and env_val_abandoned.strip():
                resolved_abandoned_keys = [env_val_abandoned.strip()]

        model_for_client_constructor: dict[str, str] = {
            "provider": actual_provider_name_str.upper(),
            "name": actual_model_name_str,
        }

        proxy_settings_obj: ProxySettings = root_cfg.proxy
        final_proxy_host: str | None = None
        final_proxy_port: int | None = None
        if proxy_settings_obj.use_proxy and proxy_settings_obj.http_proxy_url:
            try:
                parsed_url = urlparse(proxy_settings_obj.http_proxy_url)
                final_proxy_host = parsed_url.hostname
                final_proxy_port = parsed_url.port
                if not final_proxy_host or final_proxy_port is None:
                    logger.warning(
                        f"代理URL '{proxy_settings_obj.http_proxy_url}' 解析不完整 (host: {final_proxy_host}, port: {final_proxy_port})。将不使用代理。"
                    )
                    final_proxy_host = None
                    final_proxy_port = None
            except Exception as e_parse_proxy:
                logger.warning(
                    f"解析代理URL '{proxy_settings_obj.http_proxy_url}' 失败: {e_parse_proxy}。将不使用代理。"
                )
                final_proxy_host = None
                final_proxy_port = None

        model_specific_kwargs: dict[str, Any] = {}
        if model_params_cfg.temperature is not None:
            model_specific_kwargs["temperature"] = model_params_cfg.temperature
        if model_params_cfg.max_output_tokens is not None:
            model_specific_kwargs["maxOutputTokens"] = model_params_cfg.max_output_tokens
        if model_params_cfg.top_p is not None:
            model_specific_kwargs["top_p"] = model_params_cfg.top_p
        if model_params_cfg.top_k is not None:
            model_specific_kwargs["top_k"] = model_params_cfg.top_k

        processor_constructor_args: dict[str, Any] = {
            "model": model_for_client_constructor,
            "image_placeholder_tag": general_llm_settings_obj.image_placeholder_tag,
            "stream_chunk_delay_seconds": general_llm_settings_obj.stream_chunk_delay_seconds,
            "enable_image_compression": general_llm_settings_obj.enable_image_compression,
            "image_compression_target_bytes": general_llm_settings_obj.image_compression_target_bytes,
            "rate_limit_disable_duration_seconds": general_llm_settings_obj.rate_limit_disable_duration_seconds,
            "proxy_host": final_proxy_host,
            "proxy_port": final_proxy_port,
            "abandoned_keys_config": resolved_abandoned_keys,
            **model_specific_kwargs,
        }

        final_constructor_args = {k: v for k, v in processor_constructor_args.items() if v is not None}
        client_instance = ProcessorClient(**final_constructor_args)  # type: ignore

        logger.info(
            f"成功创建 ProcessorClient 实例用于 '{purpose_key}' (模型: {client_instance.llm_client.model_name}, 提供商: {client_instance.llm_client.provider})."
        )
        return client_instance

    except AttributeError as e_attr:
        logger.error(f"配置访问错误 (AttributeError) 创建LLM客户端 (用途: {purpose_key}) 时: {e_attr}", exc_info=True)
        logger.error(
            "这通常意味着 AlcarusRootConfig 的 dataclass 定义与 config.toml 文件结构不匹配，或者某个必需的配置段/字段缺失。"
        )
        return None
    except Exception as e:
        logger.error(f"创建LLM客户端 (用途: {purpose_key}) 时发生未知错误: {e}", exc_info=True)
        return None


async def initialize_llm_clients_for_action_module() -> None:
    # ... (内容保持不变) ...
    global action_llm_client, summary_llm_client
    if action_llm_client and summary_llm_client:
        return
    logger.info("正在为行动处理模块初始化LLM客户端...")
    try:
        root_config: AlcarusRootConfig = get_typed_settings()
    except Exception as e:
        logger.critical(f"无法加载类型化配置对象: {e}", exc_info=True)
        raise RuntimeError(f"行动模块LLM客户端初始化失败：无法加载类型化配置 - {e}") from e

    action_llm_client = _create_llm_client_from_config(
        purpose_key="action_decision",
        default_provider_name="gemini",
        root_cfg=root_config,
    )
    if not action_llm_client:
        raise RuntimeError("行动决策LLM客户端初始化失败。请检查日志和配置文件。")

    summary_llm_client = _create_llm_client_from_config(
        purpose_key="information_summary",
        default_provider_name="gemini",
        root_cfg=root_config,
    )
    if not summary_llm_client:
        raise RuntimeError("信息总结LLM客户端初始化失败。请检查日志和配置文件。")

    logger.info("行动处理模块的LLM客户端初始化完成。")


async def _get_current_action_state_for_idempotency(
    db: StandardDatabase, collection_name: str, doc_key: str
) -> dict | None:
    """
    [幂等性辅助函数] 获取指定文档键的当前 action_attempted 状态。
    """
    if not doc_key:
        return None
    try:
        doc = await asyncio.to_thread(db.collection(collection_name).get, doc_key)
        if doc and isinstance(doc.get("action_attempted"), dict):
            return doc["action_attempted"]
        elif doc:
            logger.warning(
                f"[状态获取] 文档 {doc_key} 中未找到有效的 'action_attempted' 字段。文档内容: {str(doc)[:200]}..."
            )
            return {}
        else:
            logger.warning(f"[状态获取] 文档 {doc_key} 未在集合 {collection_name} 中找到。")
            return None
    except Exception as e:
        logger.error(f"[状态获取] 获取文档 {doc_key} 状态时发生错误: {e}", exc_info=True)
        return None


async def process_action_flow(
    action_id: str,
    doc_key_for_updates: str,
    action_description: str,
    action_motivation: str,
    current_thought_context: str,
    arango_db_for_updates: StandardDatabase,
    collection_name_for_updates: str,
    comm_layer_for_actions: CoreWebsocketServer | None = None,
) -> None:
    """
    处理一个完整的行动流程。
    使用数据库层面的原子条件更新来处理并发。
    """
    global core_communication_layer_for_actions
    current_comm_layer = comm_layer_for_actions if comm_layer_for_actions else core_communication_layer_for_actions

    logger.info(f"--- [Action ID: {action_id}, DocKey: {doc_key_for_updates}] 进入 process_action_flow ---")

    if not action_llm_client or not summary_llm_client:
        try:
            await initialize_llm_clients_for_action_module()
            if not action_llm_client or not summary_llm_client:
                raise RuntimeError("LLM客户端在 initialize_llm_clients_for_action_module 调用后仍未初始化。")
        except Exception as e_init:
            logger.critical(
                f"严重错误 [Action ID: {action_id}, DocKey: {doc_key_for_updates}]: 无法初始化行动模块的LLM客户端: {e_init}",
                exc_info=True,
            )
            await arangodb_handler.update_action_status_in_document(
                arango_db_for_updates,
                collection_name_for_updates,
                doc_key_for_updates,
                action_id,
                {
                    "status": "CRITICAL_FAILURE",
                    "error_message": f"行动模块LLM客户端初始化失败: {str(e_init)}",
                    "final_result_for_shuang": f"你尝试执行动作 '{action_description}' 时，系统遇到严重的初始化错误，无法继续。",
                },
            )
            return

    current_action_state = await _get_current_action_state_for_idempotency(
        arango_db_for_updates, collection_name_for_updates, doc_key_for_updates
    )
    if current_action_state is None and doc_key_for_updates:
        logger.error(
            f"错误 [Action ID: {action_id}, DocKey: {doc_key_for_updates}]: 无法获取动作文档的初始状态，流程终止。"
        )
        return

    target_status_processing = "PROCESSING_DECISION"
    expected_cond_for_processing = {}
    proceed_to_llm_decision = True

    if current_action_state:
        current_status_val = current_action_state.get("status")
        if current_status_val == target_status_processing:
            logger.info(
                f"[条件更新检查] Action ID {action_id}: 状态已经是 {target_status_processing}，不尝试更新，继续流程。"
            )
        elif current_status_val in ["TOOL_EXECUTING", "COMPLETED_SUCCESS", "COMPLETED_FAILURE", "CRITICAL_FAILURE"]:
            logger.info(
                f"[条件更新检查] Action ID {action_id}: 状态 ({current_status_val}) 已跳过 {target_status_processing}，不回退更新。检查是否跳过LLM决策。"
            )
            if current_status_val in ["COMPLETED_SUCCESS", "COMPLETED_FAILURE", "CRITICAL_FAILURE"]:
                proceed_to_llm_decision = False
        else:
            logger.info(
                f"[Action ID {action_id}]: 尝试更新状态到 {target_status_processing}。当前状态: {current_status_val}"
            )
            expected_cond_for_processing = {"status": current_status_val} if current_status_val else None

            update_success_processing = await arangodb_handler.update_action_status_in_document(
                arango_db_for_updates,
                collection_name_for_updates,
                doc_key_for_updates,
                action_id,
                {"status": target_status_processing},
                expected_conditions=expected_cond_for_processing,
            )
            if update_success_processing:
                logger.info(f"[Action ID {action_id}]: 状态成功更新到 {target_status_processing}。")
                current_action_state = await _get_current_action_state_for_idempotency(
                    arango_db_for_updates, collection_name_for_updates, doc_key_for_updates
                )
            else:
                # 更新未执行，重新获取状态
                logger.debug(
                    f"[Action ID {action_id}]: 更新状态到 {target_status_processing} 的DB调用返回False。重新获取状态。"
                )
                current_action_state = await _get_current_action_state_for_idempotency(
                    arango_db_for_updates, collection_name_for_updates, doc_key_for_updates
                )
                if not (current_action_state and current_action_state.get("status") == target_status_processing):
                    logger.error(
                        f"错误 [Action ID: {action_id}]: 更新到 {target_status_processing} 后状态仍不正确 ({current_action_state.get('status') if current_action_state else 'None'})，流程终止。"
                    )
                    await arangodb_handler.update_action_status_in_document(
                        arango_db_for_updates,
                        collection_name_for_updates,
                        doc_key_for_updates,
                        action_id,
                        {
                            "status": "COMPLETED_FAILURE",
                            "error_message": f"无法将状态设置为{target_status_processing}",
                            "final_result_for_shuang": f"系统在初始化动作时遇到状态问题，无法为动作 '{action_description}' 进行决策。",
                        },
                    )
                    return
                else:
                    logger.info(
                        f"[Action ID {action_id}]: 状态已是 {target_status_processing} (可能由并发操作完成，在更新尝试后确认)。"
                    )

    final_result_for_shuang: str = f"尝试执行动作 '{action_description}' 时出现未知的处理错误。"
    action_was_successful: bool = False

    if not proceed_to_llm_decision:
        logger.info(
            f"[流程控制] Action ID {action_id}: 动作状态为 {current_action_state.get('status')}，跳过LLM决策和工具执行。"
        )
        final_result_for_shuang = current_action_state.get("final_result_for_shuang", "动作已处理完成。")
        action_was_successful = current_action_state.get("status") == "COMPLETED_SUCCESS"
    else:
        relevant_adapter_messages_context = "无相关外部消息或请求。"
        try:
            latest_doc_for_msg_context = await arangodb_handler.get_latest_thought_document_raw(
                arango_db_for_updates, collection_name_for_updates
            )
            if latest_doc_for_msg_context and latest_doc_for_msg_context.get("adapter_messages"):
                formatted_messages = []
                for msg_entry in latest_doc_for_msg_context["adapter_messages"][-3:]:
                    sender = msg_entry.get("sender_nickname", "未知用户")
                    content = msg_entry.get("text_content", "[内容不可读]")
                    msg_type = "用户消息" if not msg_entry.get("is_platform_request") else "平台请求"
                    formatted_messages.append(f"- {msg_type}来自{sender}: {content}")
                if formatted_messages:
                    relevant_adapter_messages_context = "\n".join(formatted_messages)
        except Exception as e_fetch_msg:
            logger.warning(f"获取最近适配器消息以供行动决策时出错: {e_fetch_msg}")

        try:
            tools_json_str = json.dumps(AVAILABLE_TOOLS_SCHEMA_FOR_GEMINI, indent=2, ensure_ascii=False)
            decision_prompt = ACTION_DECISION_PROMPT_TEMPLATE.format(
                tools_json_string=tools_json_str,
                current_thought_context=current_thought_context,
                action_description=action_description,
                action_motivation=action_motivation,
                relevant_adapter_messages_context=relevant_adapter_messages_context,
            )
            logger.info(f"--- [Action ID: {action_id}] 请求行动决策LLM ---")
            decision_response: dict = await action_llm_client.llm_client.generate_with_tools(
                prompt=decision_prompt,
                tools=AVAILABLE_TOOLS_SCHEMA_FOR_GEMINI,
                is_stream=False,
            )
            logger.info(f"--- [Action ID: {action_id}, DocKey: {doc_key_for_updates}] 行动决策LLM调用完成 ---")

            if decision_response.get("error"):
                error_msg = decision_response.get("message", "行动决策LLM调用时返回了错误状态")
                logger.error(f"错误 [Action ID: {action_id}]: 行动决策LLM调用失败 - {error_msg}")
                final_result_for_shuang = f"我试图决定如何执行动作 '{action_description}' 时遇到了问题: {error_msg}"
                action_was_successful = False
                await arangodb_handler.update_action_status_in_document(
                    arango_db_for_updates,
                    collection_name_for_updates,
                    doc_key_for_updates,
                    action_id,
                    {
                        "status": "COMPLETED_FAILURE",
                        "error_message": f"行动决策LLM错误: {error_msg}",
                        "final_result_for_shuang": final_result_for_shuang,
                    },
                )
                return

            tool_call_chosen: dict | None = None
            if (
                decision_response.get("tool_calls")
                and isinstance(decision_response["tool_calls"], list)
                and len(decision_response["tool_calls"]) > 0
            ):
                tool_call_chosen = decision_response["tool_calls"][0]
            elif decision_response.get("text"):
                llm_text_output: str = decision_response.get("text", "").strip()
                try:
                    if llm_text_output.startswith("```json"):
                        llm_text_output = llm_text_output[7:-3].strip()
                    elif llm_text_output.startswith("```"):
                        llm_text_output = llm_text_output[3:-3].strip()
                    parsed_text_json: dict = json.loads(llm_text_output)
                    if (
                        isinstance(parsed_text_json, dict)
                        and parsed_text_json.get("tool_calls")
                        and isinstance(parsed_text_json["tool_calls"], list)
                        and len(parsed_text_json["tool_calls"]) > 0
                    ):
                        tool_call_chosen = parsed_text_json["tool_calls"][0]
                    else:
                        final_result_for_shuang = await report_action_failure(
                            intended_action_description=action_description,
                            intended_action_motivation=action_motivation,
                            reason_for_failure_short=f"行动决策模型未选择有效工具(text解析结构不对)：{llm_text_output[:100]}...",
                        )
                        action_was_successful = False
                except json.JSONDecodeError:
                    final_result_for_shuang = await report_action_failure(
                        intended_action_description=action_description,
                        intended_action_motivation=action_motivation,
                        reason_for_failure_short=f"行动决策模型的回复格式不正确(text解析失败)：{llm_text_output[:100]}...",
                    )
                    action_was_successful = False

            if not tool_call_chosen and not action_was_successful:
                logger.error(
                    f"错误 [Action ID: {action_id}]: 行动决策LLM未能提供有效工具调用或解析失败（最终检查点）。"
                )
                if final_result_for_shuang.startswith("尝试执行动作"):
                    final_result_for_shuang = await report_action_failure(
                        intended_action_description=action_description,
                        intended_action_motivation=action_motivation,
                        reason_for_failure_short="行动决策模型未能提供有效的工具调用指令或解析其输出失败（最终检查点）。",
                    )
                action_was_successful = False
                await arangodb_handler.update_action_status_in_document(
                    arango_db_for_updates,
                    collection_name_for_updates,
                    doc_key_for_updates,
                    action_id,
                    {
                        "status": "COMPLETED_FAILURE",
                        "error_message": "行动决策LLM未能提供有效工具调用或解析失败（最终检查点）。",
                        "final_result_for_shuang": final_result_for_shuang,
                    },
                )
                return

            if tool_call_chosen:
                tool_name: str | None = tool_call_chosen.get("function", {}).get("name")
                tool_args_str: str | None = tool_call_chosen.get("function", {}).get("arguments")
                if not tool_name or tool_args_str is None:
                    final_result_for_shuang = "系统在理解工具调用指令时出错（缺少工具名称或参数）。"
                    action_was_successful = False
                    await arangodb_handler.update_action_status_in_document(
                        arango_db_for_updates,
                        collection_name_for_updates,
                        doc_key_for_updates,
                        action_id,
                        {
                            "status": "COMPLETED_FAILURE",
                            "error_message": "解析工具调用格式错误",
                            "final_result_for_shuang": final_result_for_shuang,
                        },
                    )
                    return
                try:
                    tool_args: dict = json.loads(tool_args_str)
                    if not isinstance(tool_args, dict):
                        raise json.JSONDecodeError("Arguments not a dict", tool_args_str, 0)
                except json.JSONDecodeError:
                    final_result_for_shuang = f"系统在理解动作 '{action_description}' 的工具参数时发生JSON解析错误。"
                    action_was_successful = False
                    await arangodb_handler.update_action_status_in_document(
                        arango_db_for_updates,
                        collection_name_for_updates,
                        doc_key_for_updates,
                        action_id,
                        {
                            "status": "COMPLETED_FAILURE",
                            "error_message": f"工具参数JSON解析错误: {tool_args_str}",
                            "final_result_for_shuang": final_result_for_shuang,
                        },
                    )
                    return

                target_status_tool_executing = "TOOL_EXECUTING"
                expected_cond_for_tool_exec = {"status": "PROCESSING_DECISION"}

                current_action_state_before_tool_exec = await _get_current_action_state_for_idempotency(
                    arango_db_for_updates, collection_name_for_updates, doc_key_for_updates
                )
                proceed_with_tool_execution_logic = True

                if (
                    current_action_state_before_tool_exec
                    and current_action_state_before_tool_exec.get("status") == target_status_tool_executing
                    and current_action_state_before_tool_exec.get("tool_selected") == tool_name
                    and current_action_state_before_tool_exec.get("tool_args") == tool_args
                ):
                    logger.info(
                        f"[条件更新检查] Action ID {action_id}: 状态、工具和参数已是目标值 ({target_status_tool_executing}, {tool_name})，跳过DB更新。"
                    )
                    current_action_state = current_action_state_before_tool_exec
                elif current_action_state_before_tool_exec and current_action_state_before_tool_exec.get("status") in [
                    "COMPLETED_SUCCESS",
                    "COMPLETED_FAILURE",
                    "CRITICAL_FAILURE",
                ]:
                    logger.warning(
                        f"[条件更新检查] Action ID {action_id}: 动作已处于最终状态 ({current_action_state_before_tool_exec.get('status')})，不再更新到 {target_status_tool_executing}，并跳过工具执行。"
                    )
                    final_result_for_shuang = current_action_state_before_tool_exec.get(
                        "final_result_for_shuang", "动作已完成。"
                    )
                    action_was_successful = current_action_state_before_tool_exec.get("status") == "COMPLETED_SUCCESS"
                    proceed_with_tool_execution_logic = False
                else:
                    logger.info(
                        f"[Action ID {action_id}]: 尝试更新状态到 {target_status_tool_executing}, 工具: {tool_name}。期望旧状态: {expected_cond_for_tool_exec.get('status')}"
                    )
                    update_success_tool_exec = await arangodb_handler.update_action_status_in_document(
                        arango_db_for_updates,
                        collection_name_for_updates,
                        doc_key_for_updates,
                        action_id,
                        {"status": target_status_tool_executing, "tool_selected": tool_name, "tool_args": tool_args},
                        expected_conditions=expected_cond_for_tool_exec,
                    )
                    if update_success_tool_exec:
                        logger.info(f"[Action ID {action_id}]: 状态成功更新到 {target_status_tool_executing}。")
                        current_action_state = await _get_current_action_state_for_idempotency(
                            arango_db_for_updates, collection_name_for_updates, doc_key_for_updates
                        )
                    else:
                        # 更新到 TOOL_EXECUTING 失败或未执行 (DB调用返回False)
                        logger.debug(
                            f"[Action ID {action_id}]: 更新状态到 {target_status_tool_executing} 的DB调用返回False。重新获取并检查当前状态。"
                        )
                        current_action_state_after_failed_update = await _get_current_action_state_for_idempotency(
                            arango_db_for_updates, collection_name_for_updates, doc_key_for_updates
                        )
                        logger.debug(
                            f"[DEBUG][Action ID {action_id}]: 更新到 TOOL_EXECUTING 失败后，DB实际状态: {repr(current_action_state_after_failed_update.get('status') if current_action_state_after_failed_update else 'None')}. "
                            f"期望目标状态是: {repr(target_status_tool_executing)}"
                        )

                        if (
                            current_action_state_after_failed_update
                            and current_action_state_after_failed_update.get("status") == target_status_tool_executing
                        ):
                            logger.info(
                                f"[Action ID {action_id}]: 状态已经是 {target_status_tool_executing} (可能由并发操作完成)。将继续执行工具逻辑。"
                            )
                            current_action_state = current_action_state_after_failed_update
                        elif current_action_state_after_failed_update and current_action_state_after_failed_update.get(
                            "status"
                        ) in ["COMPLETED_SUCCESS", "COMPLETED_FAILURE", "CRITICAL_FAILURE"]:
                            logger.info(
                                f"[流程控制] Action ID {action_id}: 尝试更新到TOOL_EXECUTING失败，但发现动作已是最终状态 {current_action_state_after_failed_update.get('status')}。使用已有结果，跳过工具执行。"
                            )
                            final_result_for_shuang = current_action_state_after_failed_update.get(
                                "final_result_for_shuang", "动作已完成。"
                            )
                            action_was_successful = (
                                current_action_state_after_failed_update.get("status") == "COMPLETED_SUCCESS"
                            )
                            proceed_with_tool_execution_logic = False
                        else:
                            logger.error(
                                f"错误 [Action ID: {action_id}]: 无法将状态更新到 {target_status_tool_executing} 且动作未完成，流程终止。重新获取的状态为: {current_action_state_after_failed_update.get('status') if current_action_state_after_failed_update else 'None'}"
                            )
                            final_result_for_shuang = f"系统在准备执行工具时遇到状态同步问题（状态意外），无法继续动作 '{action_description}'。"
                            action_was_successful = False
                            await arangodb_handler.update_action_status_in_document(
                                arango_db_for_updates,
                                collection_name_for_updates,
                                doc_key_for_updates,
                                action_id,
                                {
                                    "status": "COMPLETED_FAILURE",
                                    "error_message": f"状态同步问题（意外状态 {current_action_state_after_failed_update.get('status') if current_action_state_after_failed_update else 'None'}），无法更新到TOOL_EXECUTING",
                                    "final_result_for_shuang": final_result_for_shuang,
                                },
                            )
                            return

                if proceed_with_tool_execution_logic:
                    logger.info(
                        f"--- [Action ID: {action_id}, DocKey: {doc_key_for_updates}] 开始执行工具: {tool_name}, 参数: {tool_args} ---"
                    )
                    raw_tool_output: str = "工具未返回任何输出或执行时发生错误。"
                    if tool_name == "web_search":
                        raw_tool_output = await search_web(**tool_args)
                        if isinstance(raw_tool_output, str) and any(
                            err_keyword in raw_tool_output.lower()
                            for err_keyword in ["error", "出错", "失败", "未能通过"]
                        ):
                            final_result_for_shuang = str(raw_tool_output)
                            action_was_successful = False
                        else:
                            logger.info(f"--- [Action ID: {action_id}] 网页搜索成功，准备总结 ---")
                            original_query_for_summary: str = tool_args.get("query", action_description)
                            summary_prompt: str = INFORMATION_SUMMARY_PROMPT_TEMPLATE.format(
                                original_query_or_action=original_query_for_summary,
                                original_motivation=action_motivation,
                                raw_tool_output=str(raw_tool_output),
                            )
                            if summary_llm_client:
                                summary_response: dict = await summary_llm_client.llm_client.make_request(
                                    prompt=summary_prompt, is_stream=False
                                )
                                if summary_response.get("error"):
                                    final_result_for_shuang = f"找到信息，但总结时出错: {summary_response.get('message', '')}. 原始: {str(raw_tool_output)[:100]}..."
                                    action_was_successful = False
                                elif summary_response.get("text"):
                                    final_result_for_shuang = summary_response.get("text")
                                    action_was_successful = True
                                    logger.info(f"--- [Action ID: {action_id}] 总结成功 ---")
                                else:
                                    final_result_for_shuang = (
                                        f"找到信息，但总结服务未返回文本. 原始: {str(raw_tool_output)[:100]}..."
                                    )
                                    action_was_successful = False
                            else:
                                final_result_for_shuang = f"总结服务不可用. 原始: {str(raw_tool_output)[:100]}..."
                                action_was_successful = False
                    elif tool_name == "send_reply_message_to_adapter":
                        if current_comm_layer:
                            msg_content = tool_args.get("message_content_text", "...")
                            target_uid = tool_args.get("target_user_id")
                            target_gid = tool_args.get("target_group_id")
                            reply_to_msg_id = tool_args.get("reply_to_message_id")
                            if not target_uid and not target_gid:
                                raw_tool_output = "发送失败:无目标ID"
                                final_result_for_shuang = "不知回复给谁"
                                action_was_successful = False
                            else:
                                bot_id_for_action = "core_bot"
                                platform_for_action = "core_platform"
                                action_message_info = BaseMessageInfo(
                                    platform=platform_for_action,
                                    bot_id=bot_id_for_action,
                                    interaction_purpose="core_action",
                                    time=time.time() * 1000.0,
                                    message_id=f"core_action_reply_{uuid.uuid4()}",
                                    user_info=UserInfo(user_id=target_uid) if target_uid else None,
                                    group_info=GroupInfo(group_id=target_gid) if target_gid else None,
                                    additional_config={"protocol_version": "1.2.0"},
                                )
                                segments_for_action = [Seg(type="text", data=msg_content)]
                                action_data_for_seg = {"segments": [s.to_dict() for s in segments_for_action]}
                                if target_uid:
                                    action_data_for_seg["target_user_id"] = target_uid
                                if target_gid:
                                    action_data_for_seg["target_group_id"] = target_gid
                                if reply_to_msg_id:
                                    action_data_for_seg["reply_to_message_id"] = reply_to_msg_id
                                core_action_seg = Seg(type="action:send_message", data=action_data_for_seg)
                                action_to_send = MessageBase(
                                    message_info=action_message_info,
                                    message_segment=Seg(type="seglist", data=[core_action_seg]),
                                )
                                send_success = await current_comm_layer.broadcast_action_to_adapters(action_to_send)
                                if send_success:
                                    raw_tool_output = f"消息已发送: '{msg_content}'"
                                    final_result_for_shuang = f"已回复 '{msg_content[:30]}...'"
                                    action_was_successful = True
                                else:
                                    raw_tool_output = "传递消息给适配器失败"
                                    final_result_for_shuang = "消息没发出"
                                    action_was_successful = False
                        else:
                            raw_tool_output = "发送失败:通信层未初始化"
                            final_result_for_shuang = "内部通讯出错"
                            action_was_successful = False
                    elif tool_name == "handle_platform_request_internally":
                        if current_comm_layer:
                            req_type = tool_args.get("request_type")
                            req_flag = tool_args.get("request_flag")
                            approve = tool_args.get("approve_action", False)
                            remark_reason = tool_args.get("remark_or_reason")
                            if not req_type or not req_flag:
                                raw_tool_output = "处理平台请求失败:缺少参数"
                                final_result_for_shuang = "处理平台请求信息不完整"
                                action_was_successful = False
                            else:
                                bot_id_for_action = "core_bot"
                                platform_for_action = "core_platform"
                                action_message_info = BaseMessageInfo(
                                    platform=platform_for_action,
                                    bot_id=bot_id_for_action,
                                    interaction_purpose="core_action",
                                    time=time.time() * 1000.0,
                                    message_id=f"core_action_handle_req_{uuid.uuid4()}",
                                    additional_config={"protocol_version": "1.2.0"},
                                )
                                aicarus_action_seg_type = ""
                                action_data_for_seg: dict[str, Any] = {"request_flag": req_flag, "approve": approve}
                                if req_type == "friend_add":
                                    aicarus_action_seg_type = "action:handle_friend_request"
                                    _ = (
                                        approve
                                        and remark_reason
                                        and action_data_for_seg.update({"remark": remark_reason})
                                    )
                                elif req_type in ["group_join_application", "group_invite_received"]:
                                    aicarus_action_seg_type = "action:handle_group_request"
                                    action_data_for_seg["request_type"] = req_type
                                    _ = (
                                        not approve
                                        and remark_reason
                                        and action_data_for_seg.update({"reason": remark_reason})
                                    )
                                else:
                                    raw_tool_output = f"处理平台请求失败:未知类型 '{req_type}'"
                                    final_result_for_shuang = f"不确定如何处理类型为 '{req_type}' 的平台请求"
                                    action_was_successful = False
                                if aicarus_action_seg_type:
                                    core_action_seg = Seg(type=aicarus_action_seg_type, data=action_data_for_seg)
                                    action_to_send = MessageBase(
                                        message_info=action_message_info,
                                        message_segment=Seg(type="seglist", data=[core_action_seg]),
                                    )
                                    send_success = await current_comm_layer.broadcast_action_to_adapters(action_to_send)
                                    if send_success:
                                        raw_tool_output = f"平台请求({req_type})指令已发送"
                                        final_result_for_shuang = f"已处理平台请求({req_type})"
                                        action_was_successful = True
                                    else:
                                        raw_tool_output = "传递平台请求指令失败"
                                        final_result_for_shuang = "平台请求指令没发出"
                                        action_was_successful = False
                        else:
                            raw_tool_output = "处理平台请求失败:通信层未初始化"
                            final_result_for_shuang = "内部通讯出错(平台请求)"
                            action_was_successful = False
                    elif tool_name == "report_action_failure":
                        tool_args_for_reporter: dict = tool_args.copy()
                        tool_args_for_reporter["intended_action_description"] = action_description
                        tool_args_for_reporter["intended_action_motivation"] = action_motivation
                        raw_tool_output = await report_action_failure(**tool_args_for_reporter)
                        final_result_for_shuang = str(raw_tool_output)
                        action_was_successful = False
                    else:
                        raw_tool_output = f"未知工具 '{tool_name}'"
                        final_result_for_shuang = raw_tool_output
                        action_was_successful = False

                    current_action_state_for_raw_out = await _get_current_action_state_for_idempotency(
                        arango_db_for_updates, collection_name_for_updates, doc_key_for_updates
                    )
                    expected_cond_for_raw_out = {"status": target_status_tool_executing}
                    new_raw_output_to_save = str(raw_tool_output)[:2000]

                    should_attempt_db_update_for_raw_output = True
                    if (
                        current_action_state_for_raw_out
                        and current_action_state_for_raw_out.get("tool_raw_output") == new_raw_output_to_save
                        and current_action_state_for_raw_out.get("status") == target_status_tool_executing
                    ):
                        logger.info(
                            f"[Idempotency-Python] Action ID {action_id}: tool_raw_output ('{new_raw_output_to_save[:50]}...') is already set and status is correct. No DB call needed for raw_output."
                        )
                        should_attempt_db_update_for_raw_output = False
                        current_action_state = current_action_state_for_raw_out

                    if should_attempt_db_update_for_raw_output:
                        logger.info(
                            f"[Action ID {action_id}]: Attempting to update tool_raw_output. Expected DB status: {expected_cond_for_raw_out.get('status')}. New raw_output: '{new_raw_output_to_save[:50]}...'"
                        )
                        update_success_raw_out = await arangodb_handler.update_action_status_in_document(
                            arango_db_for_updates,
                            collection_name_for_updates,
                            doc_key_for_updates,
                            action_id,
                            {"tool_raw_output": new_raw_output_to_save},
                            expected_conditions=expected_cond_for_raw_out,
                        )
                        if update_success_raw_out:
                            logger.info(
                                f"[Action ID {action_id}]: tool_raw_output update successful (writes_executed > 0)."
                            )
                            current_action_state = await _get_current_action_state_for_idempotency(
                                arango_db_for_updates, collection_name_for_updates, doc_key_for_updates
                            )
                        else:
                            # DB调用返回False，重新获取并验证
                            logger.info(
                                f"[Action ID {action_id}]: tool_raw_output update DB call returned False. Re-fetching and verifying."
                            )  # 改为INFO
                            current_action_state_after_raw_attempt = await _get_current_action_state_for_idempotency(
                                arango_db_for_updates, collection_name_for_updates, doc_key_for_updates
                            )
                            if (
                                current_action_state_after_raw_attempt
                                and current_action_state_after_raw_attempt.get("tool_raw_output")
                                == new_raw_output_to_save
                                and current_action_state_after_raw_attempt.get("status")
                                == expected_cond_for_raw_out.get("status")
                            ):
                                logger.info(
                                    f"[Action ID {action_id}]: tool_raw_output is now correctly set in DB (likely by concurrent update). Status is also as expected ('{expected_cond_for_raw_out.get('status')}'). Proceeding."
                                )
                                current_action_state = current_action_state_after_raw_attempt
                            elif current_action_state_after_raw_attempt and current_action_state_after_raw_attempt.get(
                                "status"
                            ) != expected_cond_for_raw_out.get("status"):
                                logger.error(
                                    f"ERROR [Action ID {action_id}]: tool_raw_output update attempt failed AND status changed unexpectedly. "
                                    f"Expected status '{expected_cond_for_raw_out.get('status')}', but found '{current_action_state_after_raw_attempt.get('status')}'. "
                                )
                                final_result_for_shuang = f"系统在记录工具输出时遇到状态不一致问题 (状态变为 {current_action_state_after_raw_attempt.get('status')})。"
                                action_was_successful = False
                                current_action_state = current_action_state_after_raw_attempt
                            else:
                                logger.error(
                                    f"ERROR [Action ID {action_id}]: tool_raw_output update attempt failed AND DB value is still not the target, while status is as expected. This is unexpected."
                                )
                                current_action_state = current_action_state_after_raw_attempt

        except Exception as e:
            logger.critical(
                f"严重错误 [Action ID: {action_id}, DocKey: {doc_key_for_updates}]: 在LLM决策或工具执行中发生意外: {e}",
                exc_info=True,
            )
            final_result_for_shuang = (
                f"我尝试执行动作 '{action_description}' 时，系统在决策或工具执行阶段发生严重内部错误: {str(e)}"
            )
            action_was_successful = False
            await arangodb_handler.update_action_status_in_document(
                arango_db_for_updates,
                collection_name_for_updates,
                doc_key_for_updates,
                action_id,
                {
                    "status": "CRITICAL_FAILURE",
                    "error_message": f"LLM决策/工具执行错误: {str(e)}",
                    "final_result_for_shuang": final_result_for_shuang,
                },
            )
            return

    # 更新最终状态
    final_status_to_set: str = "COMPLETED_SUCCESS" if action_was_successful else "COMPLETED_FAILURE"
    updates_for_final_status = {"status": final_status_to_set, "final_result_for_shuang": final_result_for_shuang}

    current_action_state_before_final = await _get_current_action_state_for_idempotency(
        arango_db_for_updates, collection_name_for_updates, doc_key_for_updates
    )
    expected_cond_for_final = {}
    allow_final_update_py_check = True

    if current_action_state_before_final:
        db_status = current_action_state_before_final.get("status")
        db_final_result = current_action_state_before_final.get("final_result_for_shuang")

        if db_status == "CRITICAL_FAILURE":
            logger.warning(
                f"[Idempotency-Python] Action ID {action_id}: Status is already CRITICAL_FAILURE. No further final status update will be attempted."
            )
            allow_final_update_py_check = False
        elif db_status == final_status_to_set and db_final_result == final_result_for_shuang:
            logger.info(
                f"[Idempotency-Python] Action ID {action_id}: Final status ('{final_status_to_set}') and result are already set. No DB call needed."
            )
            allow_final_update_py_check = False
        elif db_status == final_status_to_set and db_final_result != final_result_for_shuang:
            logger.info(
                f"[Action ID {action_id}]: Final status ('{final_status_to_set}') is already set, but final_result_for_shuang differs. Will attempt to update result. "
                f"Expected DB status for this update will be '{final_status_to_set}'."
            )
            expected_cond_for_final = {"status": final_status_to_set}
        elif db_status != final_status_to_set:
            if db_status not in ["COMPLETED_SUCCESS", "COMPLETED_FAILURE"]:
                expected_cond_for_final = {"status": db_status}

    if allow_final_update_py_check:
        logger.info(
            f"[Action ID {action_id}]: Attempting to set final status to '{final_status_to_set}'. Expected DB conditions for update: {expected_cond_for_final if expected_cond_for_final else 'None (unconditional or first set)'}."
        )
        update_success_final = await arangodb_handler.update_action_status_in_document(
            arango_db_for_updates,
            collection_name_for_updates,
            doc_key_for_updates,
            action_id,
            updates_for_final_status,
            expected_conditions=expected_cond_for_final if expected_cond_for_final else None,
        )
        if update_success_final:
            logger.info(f"[Action ID {action_id}]: Final status update successful (writes_executed > 0).")
        else:
            # DB调用返回False，重新获取并验证
            logger.info(
                f"[Action ID {action_id}]: Final status update DB call returned False. Re-fetching and verifying."
            )  # 改为INFO
            final_check_state = await _get_current_action_state_for_idempotency(
                arango_db_for_updates, collection_name_for_updates, doc_key_for_updates
            )

            if (
                final_check_state
                and final_check_state.get("status") == final_status_to_set
                and final_check_state.get("final_result_for_shuang") == final_result_for_shuang
            ):
                logger.info(
                    f"[Action ID {action_id}]: Final status and result are now correctly set in DB (likely by concurrent update)."
                )
            elif final_check_state and final_check_state.get("status") == final_status_to_set:
                logger.info(
                    f"[Action ID {action_id}]: Final status in DB is '{final_status_to_set}', but final_result_for_shuang might differ or was not updated as intended (this is OK if result was already correct). "  # 改为INFO
                    f"DB result (len {len(final_check_state.get('final_result_for_shuang', '')) if final_check_state else 0}): '{str(final_check_state.get('final_result_for_shuang'))[:50]}...'. "
                    f"Intended result (len {len(final_result_for_shuang)}): '{final_result_for_shuang[:50]}...'."
                )
            else:
                logger.error(
                    f"ERROR [Action ID {action_id}]: Final status update attempt failed AND status in DB is not the target. "
                    f"Target status '{final_status_to_set}', but DB status is '{final_check_state.get('status') if final_check_state else 'None'}'."
                )
    else:
        logger.info(
            f"[Idempotency-Python] Action ID {action_id}: No DB call attempted for final status update based on Python-level checks."
        )

    final_db_state_after_all = await _get_current_action_state_for_idempotency(
        arango_db_for_updates, collection_name_for_updates, doc_key_for_updates
    )
    final_status_in_db_str = final_db_state_after_all.get("status") if final_db_state_after_all else "无法获取"
    logger.info(
        f"--- [Action ID: {action_id}, DocKey: {doc_key_for_updates}] 动作处理流程完成。逻辑判断最终状态: {final_status_to_set}。数据库中最终确认状态: {final_status_in_db_str} ---"
    )
    logger.debug(
        f"最终反馈给霜 (Action ID: {action_id}, DocKey: {doc_key_for_updates}): {str(final_result_for_shuang)[:300]}..."
    )
