# src/action/action_handler.py
import asyncio
import json
import os  # 添加缺失的os导入
import time  # 添加缺失的time导入
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

# v1.4.0 协议导入 - 替换旧的导入
# StandardDatabase 仅用于类型提示，实际操作通过 ArangoDBHandler
from src.common.custom_logging.logger_manager import get_logger
from src.config.alcarus_configs import (
    LLMClientSettings,
    ModelParams,
    ProxySettings,
)
from src.config.global_config import global_config
from src.core_communication.core_ws_server import CoreWebsocketServer
from src.database.arangodb_handler import ArangoDBHandler  # 导入封装后的数据库处理器
from src.llmrequest.llm_processor import Client as ProcessorClient  # 重命名
from src.tools.failure_reporter import report_action_failure  # 保留工具导入
from src.tools.web_searcher import search_web  # 保留工具导入

if TYPE_CHECKING:
    pass


class ActionHandler:
    """
    负责处理AI的行动决策、工具调用和结果反馈。
    """

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
                {
                    "name": "get_active_chat_instances",
                    "description": "获取机器人当前所有活跃的聊天会话列表，包括群聊和私聊。可以指定活跃天数阈值和最大返回数量。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "active_threshold_days": {
                                "type": "integer",
                                "description": "可选。定义多少天内有消息记录的会话被认为是活跃的。默认为7天。",
                            },
                            "max_instances_to_return": {
                                "type": "integer",
                                "description": "可选。最多返回多少个活跃的会话实例。默认为20个。",
                            },
                        },
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
3.  如果找到了能够满足用户意图的工具（例如 "web_search", "send_reply_message_to_adapter", "handle_platform_request_internally"），请选择它，并为其准备好准确的调用参数。你的输出需要是一个包含 "tool_calls" 列表的JSON对象字符串。这个列表中的每个对象都描述了一个工具调用，应包含 "id"（可以是一个唯一的调用标识，例如 "call_工具名_随机串"），"type" 固定为 "function"，以及 "function" 对象（包含 "name": "工具的实际名称" 和 "arguments": "一个包含所有必需参数的JSON字符串"）。
4.  如果经过分析，你认为用户提出的动作意图非常模糊，或者现有的任何工具都无法实现它，或者这个意图本质上不需要外部工具，那么，请选择调用名为 "report_action_failure" 的工具。
    -   在调用 "report_action_failure" 时，你只需要为其 "function" 的 "arguments" 准备一个可选的参数：
        * "reason_for_failure_short": 简要说明为什么这个动作无法通过其他工具执行
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

    def __init__(self) -> None:
        self.logger = get_logger(f"AIcarusCore.{self.__class__.__name__}")
        self.root_cfg = global_config  # 直接使用全局配置

        self.action_llm_client: ProcessorClient | None = None
        self.summary_llm_client: ProcessorClient | None = None
        self.core_communication_layer: CoreWebsocketServer | None = None
        self.db_handler: ArangoDBHandler | None = None  # 将在初始化或设置时提供

        self.logger.info(f"{self.__class__.__name__} instance created.")
        # LLM客户端的初始化推迟到 initialize_llm_clients 方法

    def set_dependencies(self, db_handler: ArangoDBHandler, comm_layer: CoreWebsocketServer | None = None) -> None:
        """设置行动处理器运行所需的依赖。"""
        self.db_handler = db_handler
        self.core_communication_layer = comm_layer
        self.logger.info("ActionHandler dependencies (db_handler, comm_layer) have been set.")

    def _create_llm_client_from_config(self, purpose_key: str, default_provider_name: str) -> ProcessorClient | None:
        """根据配置创建LLM客户端实例。"""
        # 逻辑与原函数 _create_llm_client_from_config 保持一致，但使用 self.root_cfg
        if not self.root_cfg:
            self.logger.critical("Root config not loaded. Cannot create LLM client.")
            return None
        try:
            if self.root_cfg.providers is None:
                self.logger.error("配置错误：AlcarusRootConfig 中缺少 'providers' 配置段。")
                return None

            provider_settings = getattr(self.root_cfg.providers, default_provider_name.lower(), None)
            if provider_settings is None or provider_settings.models is None:
                self.logger.error(
                    f"配置错误：在 AlcarusRootConfig.providers 下未找到提供商 '{default_provider_name}' 的有效配置或其 'models' 配置段。"
                )
                return None

            model_params_cfg = getattr(provider_settings.models, purpose_key, None)
            if not isinstance(model_params_cfg, ModelParams):
                self.logger.error(
                    f"配置错误：在提供商 '{default_provider_name}' 的 models 配置下未找到模型用途键 '{purpose_key}' 对应的有效 ModelParams 配置，或类型不匹配。"
                )
                return None

            actual_provider_name_str: str = model_params_cfg.provider
            actual_model_name_str: str = model_params_cfg.model_name

            if not actual_provider_name_str or not actual_model_name_str:
                self.logger.error(
                    f"配置错误：模型 '{purpose_key}' (提供商: {actual_provider_name_str or '未知'}) 未指定 'provider' 或 'model_name'。"
                )
                return None

            general_llm_settings_obj: LLMClientSettings = self.root_cfg.llm_client_settings
            resolved_abandoned_keys: list[str] | None = None
            env_val_abandoned = os.getenv("LLM_ABANDONED_KEYS")
            if env_val_abandoned:
                try:
                    keys_from_env = json.loads(env_val_abandoned)
                    if isinstance(keys_from_env, list):
                        resolved_abandoned_keys = [str(k).strip() for k in keys_from_env if str(k).strip()]
                except json.JSONDecodeError:
                    self.logger.warning(
                        f"环境变量 'LLM_ABANDONED_KEYS' 的值不是有效的JSON列表，将尝试按逗号分隔。值: {env_val_abandoned[:50]}..."
                    )
                    resolved_abandoned_keys = [k.strip() for k in env_val_abandoned.split(",") if k.strip()]
                if not resolved_abandoned_keys and env_val_abandoned.strip():
                    resolved_abandoned_keys = [env_val_abandoned.strip()]

            model_for_client_constructor: dict[str, str] = {
                "provider": actual_provider_name_str.upper(),
                "name": actual_model_name_str,
            }

            proxy_settings_obj: ProxySettings = self.root_cfg.proxy
            final_proxy_host: str | None = None
            final_proxy_port: int | None = None
            if proxy_settings_obj.use_proxy and proxy_settings_obj.http_proxy_url:
                try:
                    parsed_url = urlparse(proxy_settings_obj.http_proxy_url)
                    final_proxy_host = parsed_url.hostname
                    final_proxy_port = parsed_url.port
                    if not final_proxy_host or final_proxy_port is None:
                        self.logger.warning(
                            f"代理URL '{proxy_settings_obj.http_proxy_url}' 解析不完整 (host: {final_proxy_host}, port: {final_proxy_port})。将不使用代理。"
                        )
                        final_proxy_host = None
                        final_proxy_port = None
                except Exception as e_parse_proxy:
                    self.logger.warning(
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

            self.logger.info(
                f"成功创建 ProcessorClient 实例用于 '{purpose_key}' (模型: {client_instance.llm_client.model_name}, 提供商: {client_instance.llm_client.provider})."
            )
            return client_instance

        except AttributeError as e_attr:
            self.logger.error(
                f"配置访问错误 (AttributeError) 创建LLM客户端 (用途: {purpose_key}) 时: {e_attr}", exc_info=True
            )
            self.logger.error(
                "这通常意味着 AlcarusRootConfig 的 dataclass 定义与 config.toml 文件结构不匹配，或者某个必需的配置段/字段缺失。"
            )
            return None
        except Exception as e:
            self.logger.error(f"创建LLM客户端 (用途: {purpose_key}) 时发生未知错误: {e}", exc_info=True)
            return None

    async def initialize_llm_clients(self) -> None:
        """初始化行动处理模块所需的LLM客户端。"""
        if self.action_llm_client and self.summary_llm_client:
            self.logger.info("行动处理模块的LLM客户端已初始化。")
            return

        self.logger.info("正在为行动处理模块初始化LLM客户端...")
        if not self.root_cfg:  # 确保配置已加载
            self.logger.critical("无法初始化行动模块LLM客户端：Root config 未加载。")
            raise RuntimeError("行动模块LLM客户端初始化失败：Root config 未加载。")

        self.action_llm_client = self._create_llm_client_from_config(
            purpose_key="action_decision", default_provider_name="gemini"
        )
        if not self.action_llm_client:
            raise RuntimeError("行动决策LLM客户端初始化失败。请检查日志和配置文件。")

        self.summary_llm_client = self._create_llm_client_from_config(
            purpose_key="information_summary", default_provider_name="gemini"
        )
        if not self.summary_llm_client:
            raise RuntimeError("信息总结LLM客户端初始化失败。请检查日志和配置文件。")

        self.logger.info("行动处理模块的LLM客户端初始化完成。")

    # --- 我们新加的工具执行方法 ---
    async def _execute_get_active_chat_instances(
        self, active_threshold_days: int = 7, max_instances_to_return: int = 20
    ) -> str:
        """获取活跃聊天实例 - 简化版本"""
        try:
            if not self.db_handler:
                return json.dumps({"error": "数据库处理器未初始化"})

            # 简单查询最近的会话
            current_time_ms = time.time() * 1000.0
            threshold_time_ms = current_time_ms - (active_threshold_days * 24 * 60 * 60 * 1000)

            query = f"""
                FOR event IN {ArangoDBHandler.EVENTS_COLLECTION_NAME}
                    FILTER event.event_type LIKE "message.%"
                    FILTER event.timestamp >= @threshold_time
                    COLLECT conversation_id = event.conversation_id INTO group
                    LET latest_timestamp = MAX(group[*].event.timestamp)
                    SORT latest_timestamp DESC
                    LIMIT @max_instances
                    RETURN {{
                        conversation_id: conversation_id,
                        last_active_timestamp: latest_timestamp,
                        platform: FIRST(group[*].event.platform),
                        type: "unknown"
                    }}
            """

            bind_vars = {"threshold_time": threshold_time_ms, "max_instances": max_instances_to_return}

            results = await self.db_handler.execute_query(query, bind_vars)

            return json.dumps({"instances": results or []}, ensure_ascii=False)

        except Exception as e:
            self.logger.error(f"获取活跃聊天实例失败: {e}", exc_info=True)
            return json.dumps({"error": str(e), "instances": []})

    # --- 新加的工具执行方法结束 ---

    async def _get_current_action_state_for_idempotency(self, doc_key: str) -> dict | None:
        """[幂等性辅助函数] 获取指定文档键的当前 action_attempted 状态。"""
        if not self.db_handler:
            self.logger.error("数据库处理器未设置，无法获取文档状态。")
            return None

        # 基本的文档键检查
        if not doc_key or not isinstance(doc_key, str):
            self.logger.error(f"无效的文档键: {type(doc_key)}, 值: {doc_key}")
            return None

        try:
            doc = await asyncio.to_thread(
                self.db_handler.db.collection(ArangoDBHandler.THOUGHTS_COLLECTION_NAME).get,
                doc_key,
            )
            if doc and isinstance(doc.get("action_attempted"), dict):
                return doc["action_attempted"]
            elif doc:
                return {}
            else:
                self.logger.warning(f"文档 {doc_key} 未找到")
                return None
        except Exception as e:
            self.logger.error(f"获取文档 {doc_key} 状态时发生错误: {e}", exc_info=True)
            return None

    async def process_action_flow(
        self,
        action_id: str,
        doc_key_for_updates: str,
        action_description: str,
        action_motivation: str,
        current_thought_context: str,
    ) -> None:
        """处理行动流程"""
        try:
            # 添加类型验证
            if not isinstance(doc_key_for_updates, str):
                self.logger.error(
                    f"doc_key_for_updates 应该是字符串，但收到了 {type(doc_key_for_updates)}: {doc_key_for_updates}"
                )
                return

            # 基本参数检查
            if not isinstance(action_id, str) or not isinstance(doc_key_for_updates, str):
                self.logger.error(f"参数类型错误 - action_id: {type(action_id)}, doc_key: {type(doc_key_for_updates)}")
                return

            if not self.db_handler:
                self.logger.critical(f"严重错误 [Action ID: {action_id}]: 数据库处理器未初始化，无法处理行动。")
                return

            self.logger.debug(
                f"--- [Action ID: {action_id}, DocKey: {doc_key_for_updates}] 进入 process_action_flow ---"
            )

            if not self.action_llm_client or not self.summary_llm_client:
                try:
                    await self.initialize_llm_clients()
                    if not self.action_llm_client or not self.summary_llm_client:
                        raise RuntimeError("LLM客户端在 initialize_llm_clients 调用后仍未初始化。")
                except Exception as e_init:
                    self.logger.critical(
                        f"严重错误 [Action ID: {action_id}, DocKey: {doc_key_for_updates}]: 无法初始化行动模块的LLM客户端: {e_init}",
                        exc_info=True,
                    )
                    await self.db_handler.update_action_status_in_document(
                        doc_key_for_updates,
                        action_id,
                        {
                            "status": "CRITICAL_FAILURE",
                            "error_message": f"行动模块LLM客户端初始化失败: {str(e_init)}",
                            "final_result_for_shuang": f"你尝试执行动作 '{action_description}' 时，系统遇到严重的初始化错误，无法继续。",
                        },
                    )
                    return

            current_action_state = await self._get_current_action_state_for_idempotency(doc_key_for_updates)
            if current_action_state is None and doc_key_for_updates:
                self.logger.error(
                    f"错误 [Action ID: {action_id}, DocKey: {doc_key_for_updates}]: 无法获取动作文档的初始状态 (文档可能不存在)，流程终止。"
                )
                return

            # 添加类型检查
            if current_action_state and not isinstance(current_action_state, dict):
                self.logger.error(
                    f"错误 [Action ID: {action_id}]: current_action_state 应该是字典类型，但收到了 {type(current_action_state)}: {current_action_state}"
                )
                current_action_state = {}

            target_status_processing = "PROCESSING_DECISION"
            expected_cond_for_processing = {}
            proceed_to_llm_decision = True

            if current_action_state:
                current_status_val = current_action_state.get("status")
                if current_status_val == target_status_processing:
                    self.logger.debug(
                        f"[条件更新检查] Action ID {action_id}: 状态已经是 {target_status_processing}，不尝试更新，继续流程。"
                    )
                elif current_status_val in [
                    "TOOL_EXECUTING",
                    "COMPLETED_SUCCESS",
                    "COMPLETED_FAILURE",
                    "CRITICAL_FAILURE",
                ]:
                    self.logger.debug(
                        f"[条件更新检查] Action ID {action_id}: 状态 ({current_status_val}) 已跳过 {target_status_processing}，不回退更新。检查是否跳过LLM决策。"
                    )
                    if current_status_val in ["COMPLETED_SUCCESS", "COMPLETED_FAILURE", "CRITICAL_FAILURE"]:
                        proceed_to_llm_decision = False
                else:
                    self.logger.debug(
                        f"[Action ID {action_id}]: 尝试更新状态到 {target_status_processing}。当前状态: {current_status_val}"
                    )
                    expected_cond_for_processing = {"status": current_status_val} if current_status_val else None

                    update_success_processing = await self.db_handler.update_action_status_in_document(
                        doc_key_for_updates,
                        action_id,
                        {"status": target_status_processing},
                        expected_conditions=expected_cond_for_processing,
                    )
                    if update_success_processing:
                        self.logger.debug(f"[Action ID {action_id}]: 状态成功更新到 {target_status_processing}。")
                        current_action_state = await self._get_current_action_state_for_idempotency(doc_key_for_updates)
                    else:
                        self.logger.debug(
                            f"[Action ID {action_id}]: 更新状态到 {target_status_processing} 的DB调用返回False。重新获取状态。"
                        )
                        current_action_state = await self._get_current_action_state_for_idempotency(doc_key_for_updates)
                        if not (
                            current_action_state and current_action_state.get("status") == target_status_processing
                        ):
                            self.logger.error(
                                f"错误 [Action ID: {action_id}]: 更新到 {target_status_processing} 后状态仍不正确 ({current_action_state.get('status') if current_action_state else 'None'})，流程终止。"
                            )
                            await self.db_handler.update_action_status_in_document(
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
                            self.logger.debug(
                                f"[Action ID {action_id}]: 状态已是 {target_status_processing} (可能由并发操作完成，在更新尝试后确认)。"
                            )
            elif not doc_key_for_updates:
                self.logger.error(
                    f"错误 [Action ID: {action_id}]: doc_key_for_updates 为空，无法进行状态更新或处理。流程终止。"
                )
                return

            final_result_for_shuang: str = f"尝试执行动作 '{action_description}' 时出现未知的处理错误。"
            action_was_successful: bool = False

            if not proceed_to_llm_decision:
                self.logger.debug(
                    f"[流程控制] Action ID {action_id}: 动作状态为 {current_action_state.get('status') if current_action_state else '未知'}，跳过LLM决策和工具执行。"
                )
                final_result_for_shuang = (
                    current_action_state.get("final_result_for_shuang", "动作已处理完成。")
                    if current_action_state
                    else "动作状态未知，结果无法确定。"
                )
                action_was_successful = (
                    current_action_state.get("status") == "COMPLETED_SUCCESS" if current_action_state else False
                )
            else:
                relevant_adapter_messages_context = "无相关外部消息或请求。"
                try:
                    latest_doc_for_msg_context = await self.db_handler.get_latest_thought_document_raw()
                    self.logger.debug(f"获取到最新思考文档类型: {type(latest_doc_for_msg_context)}")

                    # 直接处理返回数据，按预期格式访问
                    if latest_doc_for_msg_context:
                        # 如果返回的是列表，取第一个元素
                        if isinstance(latest_doc_for_msg_context, list):
                            doc_data = latest_doc_for_msg_context[0] if latest_doc_for_msg_context else {}
                        else:
                            doc_data = latest_doc_for_msg_context

                        # 记录文档键以便调试
                        self.logger.debug(
                            f"最新文档键: {list(doc_data.keys()) if isinstance(doc_data, dict) else '非字典类型'}"
                        )

                        # 首先尝试使用recent_contextual_information_input字段
                        if isinstance(doc_data, dict) and "recent_contextual_information_input" in doc_data:
                            contextual_info = doc_data["recent_contextual_information_input"]
                            self.logger.debug("从recent_contextual_information_input提取消息上下文")

                            # 提取聊天历史
                            if "chat_history:" in contextual_info:
                                chat_lines = []
                                chat_section = contextual_info.split("chat_history:")[1].split("\n")

                                for line in chat_section:
                                    if "time:" in line and "message:" in contextual_info:
                                        # 提取时间和消息
                                        time_part = line.strip()
                                        sender_part = next(
                                            (line_item for line_item in chat_section if "sender_id:" in line_item), ""
                                        )
                                        message_part = next(
                                            (line_item for line_item in chat_section if "text:" in line_item), ""
                                        )

                                        if time_part and sender_part and message_part:
                                            sender = sender_part.split("sender_id:")[1].strip().strip('"')
                                            content = message_part.split("text:")[1].strip().strip('"')
                                            chat_lines.append(f"- 用户消息来自{sender}: {content}")

                                if chat_lines:
                                    relevant_adapter_messages_context = "\n".join(chat_lines[-3:])
                                    self.logger.debug(
                                        f"从recent_contextual_information_input提取到 {len(chat_lines)} 条消息"
                                    )
                except Exception as e_fetch_msg:
                    self.logger.warning(f"获取最近适配器消息以供行动决策时出错: {e_fetch_msg}", exc_info=True)
                    # 确保即使出错，仍有默认值
                    relevant_adapter_messages_context = "无法获取相关消息或请求。"

                try:
                    tools_json_str = json.dumps(self.AVAILABLE_TOOLS_SCHEMA_FOR_GEMINI, indent=2, ensure_ascii=False)
                    decision_prompt = self.ACTION_DECISION_PROMPT_TEMPLATE.format(
                        tools_json_string=tools_json_str,
                        current_thought_context=current_thought_context,
                        action_description=action_description,
                        action_motivation=action_motivation,
                        relevant_adapter_messages_context=relevant_adapter_messages_context,
                    )
                    self.logger.info(f"--- [Action ID: {action_id}] 请求行动决策LLM ---")
                    if not self.action_llm_client:
                        raise RuntimeError("行动决策 LLM 客户端未初始化。")

                    decision_response: dict = await self.action_llm_client.llm_client.generate_with_tools(
                        prompt=decision_prompt,
                        tools=self.AVAILABLE_TOOLS_SCHEMA_FOR_GEMINI,
                        is_stream=False,
                    )
                    self.logger.debug(
                        f"--- [Action ID: {action_id}, DocKey: {doc_key_for_updates}] 行动决策LLM调用完成 ---"
                    )

                    if decision_response.get("error"):
                        error_msg = decision_response.get("message", "行动决策LLM调用时返回了错误状态")
                        self.logger.error(f"错误 [Action ID: {action_id}]: 行动决策LLM调用失败 - {error_msg}")
                        final_result_for_shuang = (
                            f"我试图决定如何执行动作 '{action_description}' 时遇到了问题: {error_msg}"
                        )
                        action_was_successful = False
                        await self.db_handler.update_action_status_in_document(
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
                        self.logger.error(
                            f"错误 [Action ID: {action_id}]: 行动决策LLM未能提供有效工具调用或解析失败（最终检查点）。"
                        )
                        if final_result_for_shuang.startswith("尝试执行动作"):
                            final_result_for_shuang = await report_action_failure(
                                intended_action_description=action_description,
                                intended_action_motivation=action_motivation,
                                reason_for_failure_short="行动决策模型未能提供有效的工具调用指令或解析其输出失败（最终检查点）。",
                            )
                        action_was_successful = False
                        await self.db_handler.update_action_status_in_document(
                            doc_key_for_updates,
                            action_id,
                            {
                                "status": "COMPLETED_FAILURE",
                                "error_message": "行动决策LLM未能提供有效工具调用或解析失败（最终检查点）。",
                                "final_result_for_shuang": final_result_for_shuang,
                            },
                        )
                        return

                    # 工具执行逻辑
                    if tool_call_chosen:
                        tool_name: str | None = tool_call_chosen.get("function", {}).get("name")
                        tool_args_str: str | None = tool_call_chosen.get("function", {}).get("arguments")

                        if not tool_name or tool_args_str is None:
                            final_result_for_shuang = "系统在理解工具调用指令时出错（缺少工具名称或参数）。"
                            action_was_successful = False
                            await self.db_handler.update_action_status_in_document(
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
                                raise ValueError(f"解析后的工具参数不是字典类型，而是 {type(tool_args)}")
                        except Exception as e:
                            final_result_for_shuang = f"解析工具调用参数时出错：{str(e)}"
                            action_was_successful = False
                            await self.db_handler.update_action_status_in_document(
                                doc_key_for_updates,
                                action_id,
                                {
                                    "status": "COMPLETED_FAILURE",
                                    "error_message": final_result_for_shuang,
                                    "final_result_for_shuang": final_result_for_shuang,
                                },
                            )
                            return

                        # 执行具体工具
                        if tool_name == "web_search":
                            self.logger.info(f"开始执行网络搜索工具，查询：{tool_args.get('query')}")
                            try:
                                search_results = await search_web(tool_args.get("query", ""), self.db_handler)
                                self.logger.info(f"网络搜索工具执行成功，找到 {len(search_results)} 个结果。")
                                if len(search_results) > 0:
                                    top_result = search_results[0]
                                    summary_text = (
                                        top_result.get("summary")
                                        or top_result.get("snippet")
                                        or "找到的结果没有摘要信息。"
                                    )
                                    final_result_for_shuang = f"网络搜索结果摘要：{summary_text}"
                                    action_was_successful = True
                                else:
                                    final_result_for_shuang = "未找到任何相关的网络搜索结果。"
                                    action_was_successful = False
                            except Exception as e:
                                self.logger.error(f"执行网络搜索工具时发生错误: {e}", exc_info=True)
                                final_result_for_shuang = f"执行网络搜索时发生错误：{str(e)}"
                                action_was_successful = False

                        elif tool_name == "send_reply_message_to_adapter":
                            self.logger.info(f"准备通过适配器发送消息，目标用户ID：{tool_args.get('target_user_id')}")
                            try:
                                await self.db_handler.send_reply_message_to_adapter(
                                    target_user_id=tool_args.get("target_user_id"),
                                    target_group_id=tool_args.get("target_group_id"),
                                    message_content_text=tool_args.get("message_content_text"),
                                    reply_to_message_id=tool_args.get("reply_to_message_id"),
                                )
                                self.logger.info("消息通过适配器发送成功。")
                                final_result_for_shuang = "消息已成功发送。"
                                action_was_successful = True
                            except Exception as e:
                                self.logger.error(f"通过适配器发送消息时发生错误: {e}", exc_info=True)
                                final_result_for_shuang = f"发送消息时发生错误：{str(e)}"
                                action_was_successful = False

                        elif tool_name == "handle_platform_request_internally":
                            self.logger.info(
                                f"处理平台请求，类型：{tool_args.get('request_type')}，标识：{tool_args.get('request_flag')}"
                            )
                            try:
                                await self.db_handler.handle_platform_request_internally(
                                    request_type=tool_args.get("request_type"),
                                    request_flag=tool_args.get("request_flag"),
                                    approve_action=tool_args.get("approve_action"),
                                    remark_or_reason=tool_args.get("remark_or_reason"),
                                )
                                self.logger.info("平台请求处理指令已发送。")
                                final_result_for_shuang = "平台请求已处理。"
                                action_was_successful = True
                            except Exception as e:
                                self.logger.error(f"处理平台请求时发生错误: {e}", exc_info=True)
                                final_result_for_shuang = f"处理平台请求时发生错误：{str(e)}"
                                action_was_successful = False

                        elif tool_name == "report_action_failure":
                            self.logger.info(f"报告行动失败，原因：{tool_args.get('reason_for_failure_short')}")
                            try:
                                final_result_for_shuang = await report_action_failure(
                                    intended_action_description=action_description,
                                    intended_action_motivation=action_motivation,
                                    reason_for_failure_short=tool_args.get("reason_for_failure_short", ""),
                                )
                                self.logger.info("行动失败报告处理完毕。")
                                action_was_successful = True
                            except Exception as e:
                                self.logger.error(f"报告行动失败时发生错误: {e}", exc_info=True)
                                final_result_for_shuang = f"报告行动失败时发生错误：{str(e)}"
                                action_was_successful = False

                        elif tool_name == "get_active_chat_instances":
                            self.logger.info(
                                f"获取活跃聊天实例，阈值：{tool_args.get('active_threshold_days')} 天，最多返回：{tool_args.get('max_instances_to_return')} 个"
                            )
                            try:
                                active_instances_result = await self._execute_get_active_chat_instances(
                                    active_threshold_days=tool_args.get("active_threshold_days", 7),
                                    max_instances_to_return=tool_args.get("max_instances_to_return", 20),
                                )
                                self.logger.info(
                                    f"获取活跃聊天实例成功，返回 {len(json.loads(active_instances_result).get('instances', []))} 个实例。"
                                )
                                final_result_for_shuang = active_instances_result
                                action_was_successful = True
                            except Exception as e:
                                self.logger.error(f"获取活跃聊天实例时发生错误: {e}", exc_info=True)
                                final_result_for_shuang = f"获取活跃聊天实例时发生错误：{str(e)}"
                                action_was_successful = False

                        else:
                            final_result_for_shuang = f"未知的工具调用：{tool_name}"
                            action_was_successful = False
                            self.logger.error(final_result_for_shuang)

                except Exception as e:
                    self.logger.error(f"处理LLM决策和工具执行时发生错误: {e}", exc_info=True)
                    final_result_for_shuang = f"处理行动决策时发生错误：{str(e)}"
                    action_was_successful = False

            # 工具执行后续处理
            if action_was_successful:
                self.logger.info(f"行动 ID {action_id} 执行成功，更新文档状态。")
                await self.db_handler.update_action_status_in_document(
                    doc_key_for_updates,
                    action_id,
                    {
                        "status": "COMPLETED_SUCCESS",
                        "final_result_for_shuang": final_result_for_shuang,
                    },
                )
            else:
                self.logger.warning(f"行动 ID {action_id} 执行失败，状态：{final_result_for_shuang}")
                await self.db_handler.update_action_status_in_document(
                    doc_key_for_updates,
                    action_id,
                    {
                        "status": "COMPLETED_FAILURE",
                        "error_message": final_result_for_shuang,
                        "final_result_for_shuang": final_result_for_shuang,
                    },
                )

        except Exception as e:
            self.logger.error(f"处理行动流程时发生错误: {e}", exc_info=True)
            final_result_for_shuang = f"处理行动流程时发生错误：{str(e)}"
            await self.db_handler.update_action_status_in_document(
                doc_key_for_updates,
                action_id,
                {
                    "status": "COMPLETED_FAILURE",
                    "error_message": final_result_for_shuang,
                    "final_result_for_shuang": final_result_for_shuang,
                },
            )
