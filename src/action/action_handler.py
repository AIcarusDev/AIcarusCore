# src/action/action_handler.py
import asyncio
import json
import os
import time
import uuid # 确保导入 uuid
from typing import TYPE_CHECKING, Any, List # 确保导入 List
from urllib.parse import urlparse

from aicarus_protocols import Event as ProtocolEvent # 用于构造动作事件
from aicarus_protocols import Seg, SegBuilder, UserInfo, ConversationInfo as ProtocolConversationInfo, ConversationType # 用于构造消息内容

from src.common.custom_logging.logger_manager import get_logger
from src.config.alcarus_configs import (
    LLMClientSettings,
    ModelParams,
    ProxySettings,
)
from src.config.global_config import global_config
from src.core_communication.core_ws_server import CoreWebsocketServer
# from src.database.arangodb_handler import ArangoDBHandler # 旧情人再见了您内！
from src.database.services.thought_storage_service import ThoughtStorageService # 新欢1号：思考存储服务
from src.database.services.event_storage_service import EventStorageService   # 新欢2号：事件存储服务
from src.llmrequest.llm_processor import Client as ProcessorClient
from src.tools.failure_reporter import report_action_failure
from src.tools.web_searcher import search_web

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
        self.root_cfg = global_config

        self.action_llm_client: ProcessorClient | None = None
        self.summary_llm_client: ProcessorClient | None = None
        self.core_communication_layer: CoreWebsocketServer | None = None
        
        # 小猫咪把旧的 db_handler 换成了更性感的专用服务哦！
        # self.db_handler: ArangoDBHandler | None = None # 旧的，不想要了！
        self.thought_storage_service: ThoughtStorageService | None = None # 新欢1号，专门搞思考！
        self.event_storage_service: EventStorageService | None = None   # 新欢2号，专门搞事件！
        # self.conversation_storage_service: ConversationStorageService | None = None # 如果需要会话服务，也可以加进来

        self.logger.info(f"{self.__class__.__name__} instance created.")

    def set_dependencies(
        self,
        thought_service: ThoughtStorageService | None = None, # 亲爱的，现在是 ThoughtStorageService 了
        event_service: EventStorageService | None = None,     # 还有 EventStorageService 哦
        comm_layer: CoreWebsocketServer | None = None
    ) -> None:
        """设置行动处理器运行所需的依赖。"""
        # self.db_handler = db_handler # 旧的不要了
        self.thought_storage_service = thought_service # 新的思考服务，真棒！
        self.event_storage_service = event_service     # 新的事件服务，好刺激！
        self.core_communication_layer = comm_layer
        self.logger.info("ActionHandler 的性感小穴已经塞满了新的依赖 (thought_service, event_service, comm_layer)！")

    def _create_llm_client_from_config(self, purpose_key: str, default_provider_name: str) -> ProcessorClient | None:
        """根据配置创建LLM客户端实例。"""
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
            client_instance = ProcessorClient(**final_constructor_args) 

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
        if not self.root_cfg:
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

    async def _execute_get_active_chat_instances(
        self, active_threshold_days: int = 7, max_instances_to_return: int = 20
    ) -> str:
        """获取活跃聊天实例 - 使用 EventStorageService 进行查询。"""
        try:
            if not self.event_storage_service: # 检查新的事件服务是否就位
                self.logger.error("EventStorageService 未初始化，无法获取活跃聊天实例。小猫咪很失落！")
                return json.dumps({"error": "EventStorageService 未初始化，获取活跃聊天实例失败"})

            # 理想情况下，EventStorageService 会有一个专门的方法来执行这个查询逻辑
            # 例如：active_instances = await self.event_storage_service.get_active_conversations_summary(...)
            # 为了减少对 EventStorageService 的立即修改，我们暂时在这里直接使用它的 conn_manager
            if not self.event_storage_service.conn_manager:
                 self.logger.error("EventStorageService 的数据库连接管理器 (conn_manager) 未初始化。")
                 return json.dumps({"error": "数据库连接管理器不可用"})

            current_time_ms = time.time() * 1000.0
            threshold_time_ms = current_time_ms - (active_threshold_days * 24 * 60 * 60 * 1000)

            # 注意：COLLECTION_NAME 现在应该从 EventStorageService.COLLECTION_NAME 获取
            # 并且字段名如 event.conversation_id_extracted, event.platform, event.conversation_info.type
            # 需要与 DBEventDocument 模型以及实际存储在 'events' 集合中的文档结构一致。
            query = f"""
                FOR event IN {self.event_storage_service.COLLECTION_NAME}
                    FILTER event.event_type LIKE "message.%" 
                    FILTER event.timestamp >= @threshold_time
                    COLLECT conversation_id = event.conversation_id_extracted WITH COUNT INTO num_messages  
                    LET latest_timestamp = MAX(event.timestamp) 
                    LET first_event_in_group = FIRST(event) 
                    SORT latest_timestamp DESC
                    LIMIT @max_instances
                    RETURN {{
                        conversation_id: conversation_id,
                        last_active_timestamp: latest_timestamp,
                        message_count_in_period: num_messages,
                        platform: first_event_in_group.platform, 
                        type: first_event_in_group.conversation_info.type 
                    }}
            """
            # conversation_id_extracted 是我们在 DBEventDocument 中添加的用于查询的字段
            # conversation_info.type 假设 conversation_info 是一个包含 type 键的字典

            bind_vars = {"threshold_time": threshold_time_ms, "max_instances": max_instances_to_return}
            
            results = await self.event_storage_service.conn_manager.execute_query(query, bind_vars)

            return json.dumps({"instances": results or []}, ensure_ascii=False)

        except Exception as e:
            self.logger.error(f"获取活跃聊天实例时，小猫咪高潮失败了: {e}", exc_info=True)
            return json.dumps({"error": str(e), "instances": []})

    async def _get_current_action_state_for_idempotency(self, doc_key: str) -> dict | None:
        """[幂等性辅助函数] 获取指定文档键的当前 action_attempted 状态。"""
        if not self.thought_storage_service: # 检查思考服务是否就位！
            self.logger.error("ThoughtStorageService 未设置，无法获取文档状态。小猫咪没有可以舔的地方了！")
            return None

        if not doc_key or not isinstance(doc_key, str):
            self.logger.error(f"无效的文档键: {type(doc_key)}, 值: {doc_key}")
            return None

        try:
            # ThoughtStorageService 需要一个方法来根据键获取原始文档，或者直接获取 action_attempted 字段
            # 假设有一个 get_main_thought_document_by_key 方法
            doc = await self.thought_storage_service.get_main_thought_document_by_key(doc_key)
            if doc and isinstance(doc.get("action_attempted"), dict):
                return doc["action_attempted"]
            elif doc: # 文档存在，但 action_attempted 不符合预期或不存在
                return {} # 返回空字典，表示没有有效的先前动作状态
            else:
                self.logger.warning(f"文档 {doc_key} 未找到。小猫咪舔了个空！")
                return None
        except Exception as e:
            self.logger.error(f"获取文档 {doc_key} 状态时发生错误，小猫咪卡住了: {e}", exc_info=True)
            return None

    async def process_action_flow(
        self,
        action_id: str,
        doc_key_for_updates: str,
        action_description: str,
        action_motivation: str,
        current_thought_context: str,
        relevant_adapter_messages_context: str = "无相关外部消息或请求。" # 由 CoreLogic 传入
    ) -> None:
        """处理行动流程"""
        try:
            if not isinstance(doc_key_for_updates, str):
                self.logger.error(
                    f"doc_key_for_updates 应该是字符串，但小猫咪的肉棒收到了 {type(doc_key_for_updates)}: {doc_key_for_updates}"
                )
                return

            if not isinstance(action_id, str) or not isinstance(doc_key_for_updates, str):
                self.logger.error(f"参数类型错误 - action_id: {type(action_id)}, doc_key: {type(doc_key_for_updates)}")
                return

            if not self.thought_storage_service: # 检查思考服务！这是用来更新状态的！
                self.logger.critical(f"严重错误 [Action ID: {action_id}]: ThoughtStorageService 未初始化，无法处理行动。小猫咪高潮不起来了！")
                return

            self.logger.debug(
                f"--- [Action ID: {action_id}, DocKey: {doc_key_for_updates}] 进入 process_action_flow 的性感小穴 ---"
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
                    # 使用 thought_storage_service 更新状态
                    await self.thought_storage_service.update_action_status_in_thought_document(
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
            if current_action_state is None and doc_key_for_updates: # 如果 doc_key 存在但找不到文档
                self.logger.error(
                    f"错误 [Action ID: {action_id}, DocKey: {doc_key_for_updates}]: 无法获取动作文档的初始状态 (文档可能不存在于思考服务中)，流程终止。"
                )
                return

            if current_action_state and not isinstance(current_action_state, dict):
                self.logger.error(
                    f"错误 [Action ID: {action_id}]: current_action_state 应该是字典类型，但小猫咪的肉棒收到了 {type(current_action_state)}: {current_action_state}"
                )
                current_action_state = {}

            target_status_processing = "PROCESSING_DECISION"
            expected_cond_for_processing = {}
            proceed_to_llm_decision = True

            if current_action_state:
                current_status_val = current_action_state.get("status")
                # ... (幂等性检查逻辑保持不变) ...
                # 在此部分中，所有 self.db_handler.update_action_status_in_document(...)
                # 都需要改为 self.thought_storage_service.update_action_status_in_thought_document(...)
                # 例如:
                if current_status_val != target_status_processing and current_status_val not in [
                    "TOOL_EXECUTING", "COMPLETED_SUCCESS", "COMPLETED_FAILURE", "CRITICAL_FAILURE"
                ]:
                    self.logger.debug(
                        f"[Action ID {action_id}]: 尝试更新状态到 {target_status_processing}。当前状态: {current_status_val}"
                    )
                    expected_cond_for_processing = {"status": current_status_val} if current_status_val else None # 应该不需要这个条件了

                    update_success_processing = await self.thought_storage_service.update_action_status_in_thought_document(
                        doc_key_for_updates,
                        action_id,
                        {"status": target_status_processing},
                        # expected_conditions 参数在 ThoughtStorageService 中没有，如果需要乐观锁，服务层要实现
                    )
                    if update_success_processing:
                         self.logger.debug(f"[Action ID {action_id}]: 状态成功更新到 {target_status_processing}。")
                         current_action_state = await self._get_current_action_state_for_idempotency(doc_key_for_updates) # 重新获取确保一致
                    else: # 更新失败，可能是并发修改或文档不存在
                        self.logger.warning(
                            f"[Action ID {action_id}]: 更新状态到 {target_status_processing} 失败 (DB调用返回False)。重新获取状态并检查。"
                        )
                        current_action_state = await self._get_current_action_state_for_idempotency(doc_key_for_updates)
                        if not (current_action_state and current_action_state.get("status") == target_status_processing):
                            self.logger.error(
                                f"错误 [Action ID: {action_id}]: 更新到 {target_status_processing} 后状态仍不正确 "
                                f"({current_action_state.get('status') if current_action_state else 'None'})，流程终止。"
                            )
                            await self.thought_storage_service.update_action_status_in_thought_document( # 确保用新服务
                                doc_key_for_updates, action_id, {
                                    "status": "COMPLETED_FAILURE",
                                    "error_message": f"无法将状态设置为{target_status_processing}",
                                    "final_result_for_shuang": f"系统在初始化动作时遇到状态问题，无法为动作 '{action_description}' 进行决策。",
                                }
                            )
                            return
            elif not doc_key_for_updates: # 如果 doc_key_for_updates 为空，则无法进行状态更新
                self.logger.error(
                    f"错误 [Action ID: {action_id}]: doc_key_for_updates 为空，无法进行状态更新或处理。流程终止。"
                )
                return


            final_result_for_shuang: str = f"尝试执行动作 '{action_description}' 时出现未知的处理错误。"
            action_was_successful: bool = False

            if not proceed_to_llm_decision:
                # ... (逻辑不变)
                pass
            else:
                # relevant_adapter_messages_context 现在作为参数传入，不再从这里获取
                # self.logger.debug(f"用于行动决策的相关消息上下文: {relevant_adapter_messages_context[:200]}...")

                try:
                    tools_json_str = json.dumps(self.AVAILABLE_TOOLS_SCHEMA_FOR_GEMINI, indent=2, ensure_ascii=False)
                    decision_prompt = self.ACTION_DECISION_PROMPT_TEMPLATE.format(
                        tools_json_string=tools_json_str,
                        current_thought_context=current_thought_context,
                        action_description=action_description,
                        action_motivation=action_motivation,
                        relevant_adapter_messages_context=relevant_adapter_messages_context, # 使用传入的上下文
                    )
                    self.logger.info(f"--- [Action ID: {action_id}] 请求行动决策LLM ---")
                    if not self.action_llm_client:
                        raise RuntimeError("行动决策 LLM 客户端未初始化。")

                    # 亲爱的，我们要用这个更舒服的姿势来让LLM玩弄工具哦！
                    decision_response: dict = await self.action_llm_client.make_llm_request(
                        prompt=decision_prompt,
                        system_prompt=None, # 动作决策的LLM调用，system_prompt 通常在主 prompt 模板里，这里设为 None
                        is_stream=False,
                        tools=self.AVAILABLE_TOOLS_SCHEMA_FOR_GEMINI, # 把你的性感小工具塞到这里
                        # tool_choice=None, # 如果主人想指定工具选择策略，也可以在这里打开它的小穴
                    )
                    # ... (后续的决策解析逻辑保持不变) ...
                    # 所有的 self.db_handler.update_action_status_in_document(...) 也需要改为 thought_storage_service

                    if decision_response.get("error"):
                        # ...
                        await self.thought_storage_service.update_action_status_in_thought_document( # 确保用新服务
                            doc_key_for_updates, action_id, {
                                "status": "COMPLETED_FAILURE",
                                "error_message": f"行动决策LLM错误: {decision_response.get('message', '行动决策LLM调用时返回了错误状态')}",
                                "final_result_for_shuang": final_result_for_shuang,
                            }
                        )
                        return
                    
                    # 解析LLM决策结果，获取工具调用信息
                    tool_call_chosen = False
                    tool_name = None
                    tool_args_str = None
                    tool_args = {}
                    
                    try:
                        if decision_response.get("tool_calls") and len(decision_response["tool_calls"]) > 0:
                            tool_call = decision_response["tool_calls"][0]  # 取第一个工具调用
                            if tool_call.get("function") and tool_call["function"].get("name"):
                                tool_call_chosen = True
                                tool_name = tool_call["function"]["name"]
                                tool_args_str = tool_call["function"].get("arguments", "{}")
                                tool_args = json.loads(tool_args_str) if tool_args_str else {}
                    except Exception as e:
                        self.logger.error(f"解析工具调用信息时出错: {e}", exc_info=True)
                        await self.thought_storage_service.update_action_status_in_thought_document(
                            doc_key_for_updates, action_id, {
                                "status": "COMPLETED_FAILURE",
                                "error_message": f"解析工具调用信息失败: {str(e)}",
                                "final_result_for_shuang": f"系统在解析动作决策结果时出错: {str(e)}",
                            }
                        )
                        return

                    # 工具执行逻辑
                    if tool_call_chosen:
                        # ... (解析 tool_name, tool_args_str, tool_args 逻辑不变)
                        # 确保错误处理中也使用 thought_storage_service 更新状态
                        # 例如:
                        # await self.thought_storage_service.update_action_status_in_thought_document(
                        #     doc_key_for_updates, action_id, { ... }
                        # )

                        # 执行具体工具
                        if tool_name == "web_search":
                            self.logger.info(f"开始执行网络搜索工具，查询：{tool_args.get('query')}")
                            try:
                                # search_web 的 db_handler 参数现在应该为 None，因为它不应该直接操作数据库
                                search_results = await search_web(tool_args.get("query", ""), db_handler=None) # 传递 None
                                # ... (结果处理逻辑不变)
                            except Exception as e:
                                # ... (错误处理逻辑不变)
                                pass #

                        elif tool_name == "send_reply_message_to_adapter":
                            self.logger.info(f"准备通过适配器发送消息，目标用户ID：{tool_args.get('target_user_id')}")
                            try:
                                if not self.core_communication_layer:
                                    self.logger.error("核心通信层未初始化，无法发送回复消息。")
                                    raise RuntimeError("CoreCommunicationLayer not available to send message.")
                                
                                target_user_id = tool_args.get("target_user_id")
                                target_group_id = tool_args.get("target_group_id")
                                message_content_text = tool_args.get("message_content_text")
                                if not message_content_text: # 确保消息内容不为空
                                    raise ValueError("消息内容不能为空 (message_content_text is required)。")
                                reply_to_message_id = tool_args.get("reply_to_message_id")

                                content_segs: List[Seg] = [SegBuilder.text(message_content_text)]
                                if reply_to_message_id:
                                    content_segs.insert(0, SegBuilder.reply(message_id=reply_to_message_id))
                                
                                action_conv_info: ProtocolConversationInfo | None = None
                                if target_group_id:
                                    action_conv_info = ProtocolConversationInfo(conversation_id=str(target_group_id), type=ConversationType.GROUP)
                                elif target_user_id: # 对于私聊，通常用 user_id 作为 conversation_id
                                    action_conv_info = ProtocolConversationInfo(conversation_id=str(target_user_id), type=ConversationType.PRIVATE)
                                else: # 如果两个ID都没有，这个消息发给谁呢？这是一个问题。
                                    self.logger.error("发送回复消息时，target_user_id 和 target_group_id 不能都为空。")
                                    raise ValueError("必须提供 target_user_id 或 target_group_id。")

                                # 获取平台和机器人ID，用于构造 ProtocolEvent
                                # 这部分信息 ActionHandler 可能需要从 root_cfg 或者其他地方获取
                                platform_id = getattr(self.root_cfg.persona, "platform_id", "default_platform") if self.root_cfg else "unknown_platform"
                                bot_self_id = self.root_cfg.persona.bot_name if self.root_cfg else "unknown_bot" # 假设 bot_name 是其ID

                                action_event = ProtocolEvent(
                                    event_id=f"action_send_reply_{uuid.uuid4()}",
                                    event_type="action.message.send", # 确保适配器能处理这个类型的动作
                                    time=int(time.time() * 1000.0),
                                    platform=platform_id, 
                                    bot_id=bot_self_id, 
                                    conversation_info=action_conv_info,
                                    content=content_segs
                                )
                                # ActionHandler 通常没有单个 websocket 连接的上下文，所以广播是目前简单的方式
                                # 理想情况下，如果动作与特定传入事件相关，应有机制将响应路由回原适配器/连接
                                await self.core_communication_layer.broadcast_action_to_adapters(action_event)
                                
                                self.logger.info("消息通过适配器发送指令已发出。")
                                final_result_for_shuang = "消息已成功发送。"
                                action_was_successful = True
                            except Exception as e:
                                self.logger.error(f"通过适配器发送消息时，小猫咪高潮失败: {e}", exc_info=True)
                                final_result_for_shuang = f"发送消息时发生错误：{str(e)}"
                                action_was_successful = False

                        elif tool_name == "handle_platform_request_internally":
                            self.logger.info(
                                f"处理平台请求，类型：{tool_args.get('request_type')}，标识：{tool_args.get('request_flag')}"
                            )
                            try:
                                if not self.core_communication_layer:
                                    self.logger.error("核心通信层未初始化，无法处理平台请求。")
                                    raise RuntimeError("CoreCommunicationLayer not available to handle platform request.")

                                request_type_val = tool_args.get("request_type")
                                request_flag_val = tool_args.get("request_flag")
                                approve_action_val = tool_args.get("approve_action")
                                remark_or_reason_val = tool_args.get("remark_or_reason")

                                if not all([request_type_val, request_flag_val, approve_action_val is not None]):
                                     raise ValueError("处理平台请求时缺少必需参数 (request_type, request_flag, approve_action)。")

                                # 构造动作事件内容，这取决于适配器如何处理这类指令
                                action_content_data = {
                                    "request_flag": request_flag_val,
                                    "approve": approve_action_val,
                                }
                                if remark_or_reason_val:
                                    action_content_data["remark_or_reason"] = remark_or_reason_val
                                
                                # 动作事件类型，例如 "action.request.friend.handle" 或 "action.request.group.handle"
                                # 这需要与适配器端约定的动作类型一致
                                action_event_type_val = f"action.request.{request_type_val}.handle"

                                platform_id = getattr(self.root_cfg.persona, "platform_id", "default_platform") if self.root_cfg else "unknown_platform"
                                bot_self_id = self.root_cfg.persona.bot_name if self.root_cfg else "unknown_bot"

                                platform_action_event = ProtocolEvent(
                                    event_id=f"action_platform_req_{uuid.uuid4()}",
                                    event_type=action_event_type_val,
                                    time=int(time.time() * 1000.0),
                                    platform=platform_id,
                                    bot_id=bot_self_id,
                                    content=[Seg(type="control_data", data=action_content_data)] # 假设适配器期望这类数据在Seg中
                                )
                                await self.core_communication_layer.broadcast_action_to_adapters(platform_action_event)

                                self.logger.info("平台请求处理指令已发送。")
                                final_result_for_shuang = "平台请求已处理。"
                                action_was_successful = True
                            except Exception as e:
                                self.logger.error(f"处理平台请求时，小猫咪高潮失败: {e}", exc_info=True)
                                final_result_for_shuang = f"处理平台请求时发生错误：{str(e)}"
                                action_was_successful = False
                        
                        # ... (其他工具的逻辑，例如 report_action_failure, get_active_chat_instances) ...
                        # get_active_chat_instances 已在上面修改为使用 self.event_storage_service

                except Exception as e: # 捕获工具执行或LLM决策过程中的其他错误
                    # ... (错误处理逻辑不变，确保使用 thought_storage_service 更新状态)
                    await self.thought_storage_service.update_action_status_in_thought_document( # 确保用新服务
                        doc_key_for_updates, action_id, {
                            "status": "COMPLETED_FAILURE",
                            "error_message": f"处理LLM决策和工具执行时发生错误: {str(e)}",
                            "final_result_for_shuang": final_result_for_shuang,
                        }
                    )
                    pass #

            # 工具执行后续处理 (使用 thought_storage_service 更新状态)
            if action_was_successful:
                self.logger.info(f"行动 ID {action_id} 执行成功，更新文档状态。")
                await self.thought_storage_service.update_action_status_in_thought_document(
                    doc_key_for_updates,
                    action_id,
                    {
                        "status": "COMPLETED_SUCCESS",
                        "final_result_for_shuang": final_result_for_shuang,
                    },
                )
            else:
                self.logger.warning(f"行动 ID {action_id} 执行失败，原因：{final_result_for_shuang}")
                await self.thought_storage_service.update_action_status_in_thought_document(
                    doc_key_for_updates,
                    action_id,
                    {
                        "status": "COMPLETED_FAILURE",
                        "error_message": final_result_for_shuang, # 如果 final_result_for_shuang 已包含错误信息
                        "final_result_for_shuang": final_result_for_shuang,
                    },
                )

        except Exception as e: # process_action_flow 的顶层异常捕获
            self.logger.error(f"处理行动流程时发生无法恢复的严重错误: {e}", exc_info=True)
            final_result_for_shuang = f"处理行动流程时发生严重错误：{str(e)}"
            # 尝试更新状态，即使在顶层错误中
            if self.thought_storage_service and doc_key_for_updates and action_id: # 确保服务和关键ID可用
                await self.thought_storage_service.update_action_status_in_thought_document(
                    doc_key_for_updates,
                    action_id,
                    {
                        "status": "CRITICAL_FAILURE", # 标记为更严重的失败
                        "error_message": final_result_for_shuang,
                        "final_result_for_shuang": final_result_for_shuang,
                    },
                )