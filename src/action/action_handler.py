import json
import logging
import os
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

# ArangoDB 相关的导入移至 arangodb_handler
from arango.database import StandardDatabase

# from arango.exceptions import AQLQueryExecuteError, ArangoServerError, ArangoClientError # 已移走
from src.config.alcarus_configs import (
    AlcarusRootConfig,
    LLMClientSettings,
    LLMPurpose,
    ModelParams,
    ProviderSettings,
    ProxySettings,
)
from src.config.config_manager import get_typed_settings

# --- 新增：导入新的数据库处理器函数 ---
from src.database import arangodb_handler
from src.llmrequest.llm_processor import Client as ProcessorClient
from src.tools.failure_reporter import report_action_failure
from src.tools.web_searcher import search_web

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)
action_llm_client: ProcessorClient | None = None
summary_llm_client: ProcessorClient | None = None

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
        ]
    }
]

ACTION_DECISION_PROMPT_TEMPLATE = """你是一个智能行动辅助系统。你的主要任务是分析用户当前的思考、他们明确提出的行动意图以及背后的动机。根据这些信息，你需要从下方提供的可用工具列表中，选择一个最合适的工具来帮助用户完成这个行动，或者判断行动是否无法完成。

请参考以下信息来进行决策：

可用工具列表（以JSON Schema格式描述）：
{tools_json_string}

用户当前的思考上下文：
"{current_thought_context}"

用户明确想做的动作（原始意图描述）：
"{action_description}"

用户的动机（原始行动动机）：
"{action_motivation}"

你的决策应遵循以下步骤：
1.  仔细理解用户想要完成的动作、他们为什么想做这个动作，以及他们此刻正在思考什么。
2.  然后，查看提供的工具列表，判断是否有某个工具的功能与用户的行动意图相匹配。
3.  如果找到了能够满足用户意图的工具（例如 "web_search"），请选择它，并为其准备好准确的调用参数。你的输出需要是一个包含 "tool_calls" 列表的JSON对象字符串。这个列表中的每个对象都描述了一个工具调用，应包含 "id"（可以是一个唯一的调用标识，例如 "call_工具名_随机串"），"type" 固定为 "function"，以及 "function" 对象（包含 "name": "工具的实际名称" 和 "arguments": "一个包含所有必需参数的JSON字符串"）。
4.  如果经过分析，你认为用户提出的动作意图非常模糊，或者现有的任何工具都无法实现它，或者这个意图本质上不需要外部工具（例如，用户只是想表达一个无法具体行动化的愿望），那么，请选择调用名为 "report_action_failure" 的工具。
    -   在调用 "report_action_failure" 时，你只需要为其 "function" 的 "arguments" 准备一个可选的参数：
        * "reason_for_failure_short": 简要说明为什么这个动作无法通过其他工具执行，例如 "系统中没有找到能够执行此操作的工具" 或 "用户的意图似乎不需要借助外部工具来实现"。
5.  请确保你的最终输出**都必须**是一个包含 "tool_calls" 字段的JSON对象字符串。即使没有合适的工具（此时应选择 "report_action_failure"），也需要按此格式输出。

现在，请根据以上信息，直接输出你决定调用的工具及其参数的JSON对象字符串：
"""

# 信息总结LLM的Prompt模板
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
    settings_purpose_name: str,
    root_cfg: AlcarusRootConfig,
) -> ProcessorClient | None:
    # (此函数逻辑不变)
    try:
        llm_purpose_cfg = getattr(root_cfg, settings_purpose_name, None)
        if not isinstance(llm_purpose_cfg, LLMPurpose):
            logger.error(
                f"配置错误：在 AlcarusRootConfig 中未找到有效的 LLMPurpose 配置段，或类型不匹配，对应键名: '{settings_purpose_name}'。"
            )
            return None
        provider_name_str: str = llm_purpose_cfg.provider
        model_key_in_toml_str: str = llm_purpose_cfg.model_key_in_toml
        if not provider_name_str or not model_key_in_toml_str:
            logger.error(
                f"配置错误：LLMPurpose 配置段 '{settings_purpose_name}' 中缺少 'provider' 或 'model_key_in_toml'。"
            )
            return None
        if root_cfg.providers is None:
            logger.error("配置错误：AlcarusRootConfig 中缺少 'providers' 配置段。")
            return None
        provider_cfg = getattr(root_cfg.providers, provider_name_str.lower(), None)
        if not isinstance(provider_cfg, ProviderSettings):
            logger.error(
                f"配置错误：在 AlcarusRootConfig.providers 中未找到提供商 '{provider_name_str}' 的有效 ProviderSettings 配置，或类型不匹配。"
            )
            return None
        if provider_cfg.models is None:
            logger.error(f"配置错误：提供商 '{provider_name_str}' 下缺少 'models' 配置段。")
            return None
        model_params_cfg = getattr(provider_cfg.models, model_key_in_toml_str, None)
        if not isinstance(model_params_cfg, ModelParams):
            logger.error(
                f"配置错误：在提供商 '{provider_name_str}' 的 models 配置下未找到模型键 '{model_key_in_toml_str}' 对应的有效 ModelParams 配置，或类型不匹配。"
            )
            return None
        actual_model_name_str = model_params_cfg.model_name
        if not actual_model_name_str:
            logger.error(f"配置错误：模型 '{model_key_in_toml_str}' 未指定 'model_name'。")
            return None
        model_for_client_constructor: dict[str, str] = {
            "provider": provider_name_str.upper(),
            "name": actual_model_name_str,
        }
        general_llm_settings_obj: LLMClientSettings = root_cfg.llm_client_settings
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
        resolved_abandoned_keys: list[str] | None = None
        if hasattr(general_llm_settings_obj, "abandoned_keys") and general_llm_settings_obj.abandoned_keys is not None:
            resolved_abandoned_keys = general_llm_settings_obj.abandoned_keys  # type: ignore
        elif general_llm_settings_obj.abandoned_keys_env_var:
            env_val = os.getenv(general_llm_settings_obj.abandoned_keys_env_var)
            if env_val:
                try:
                    keys_from_env = json.loads(env_val)
                    if isinstance(keys_from_env, list):
                        resolved_abandoned_keys = [str(k).strip() for k in keys_from_env if str(k).strip()]
                except json.JSONDecodeError:
                    resolved_abandoned_keys = [k.strip() for k in env_val.split(",") if k.strip()]
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
            f"成功创建 ProcessorClient 实例用于 '{settings_purpose_name}' "
            f"(模型: {client_instance.llm_client.model_name}, 提供商: {client_instance.llm_client.provider})."
        )
        return client_instance
    except AttributeError as e_attr:
        logger.error(
            f"配置访问错误 (AttributeError) 创建LLM客户端 ({settings_purpose_name}) 时: {e_attr}", exc_info=True
        )
        logger.error(
            "这通常意味着 AlcarusRootConfig 的 dataclass 定义与 config.toml 文件结构不匹配，或者某个必需的配置段/字段缺失。"
        )
        return None
    except Exception as e:
        logger.error(f"创建LLM客户端 ({settings_purpose_name}) 时发生未知错误: {e}", exc_info=True)
        return None


async def initialize_llm_clients_for_action_module() -> None:
    # (此函数逻辑不变)
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
        settings_purpose_name="action_llm_settings",
        root_cfg=root_config,
    )
    if not action_llm_client:
        raise RuntimeError("行动决策LLM客户端初始化失败。请检查日志和配置文件。")
    summary_llm_client = _create_llm_client_from_config(
        settings_purpose_name="summary_llm_settings",
        root_cfg=root_config,
    )
    if not summary_llm_client:
        raise RuntimeError("信息总结LLM客户端初始化失败。请检查日志和配置文件。")
    logger.info("行动处理模块的LLM客户端初始化完成。")


# --- _update_action_in_db 函数已移至 arangodb_handler.py 并重命名为 update_action_status_in_document ---


async def process_action_flow(
    action_id: str,
    doc_key_for_updates: str,
    action_description: str,
    action_motivation: str,
    current_thought_context: str,
    arango_db_for_updates: StandardDatabase,
    collection_name_for_updates: str,
) -> None:
    """
    处理一个完整的行动流程。
    """
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
            # 调用新的数据库处理器函数
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

    await arangodb_handler.update_action_status_in_document(
        arango_db_for_updates,
        collection_name_for_updates,
        doc_key_for_updates,
        action_id,
        {"status": "PROCESSING_DECISION"},
    )

    final_result_for_shuang: str = f"尝试执行动作 '{action_description}' 时出现未知的处理错误。"
    action_was_successful: bool = False

    try:
        tools_json_str = json.dumps(AVAILABLE_TOOLS_SCHEMA_FOR_GEMINI, indent=2, ensure_ascii=False)
        decision_prompt = ACTION_DECISION_PROMPT_TEMPLATE.format(
            tools_json_string=tools_json_str,
            current_thought_context=current_thought_context,
            action_description=action_description,
            action_motivation=action_motivation,
        )
        # --- 新增日志打印 ---
        if action_llm_client and action_llm_client.llm_client:  # 检查客户端是否存在
            logger.info(
                f"--- 行动决策LLM接收到的完整Prompt (模型: {action_llm_client.llm_client.model_name}, Action ID: {action_id}) ---\n{decision_prompt}\n--- Prompt结束 ---"
            )
        else:
            logger.warning(f"行动决策LLM客户端未初始化，无法打印其Prompt (Action ID: {action_id})")
        # --------------------

        logger.info(f"--- [Action ID: {action_id}] 请求行动决策LLM ---")
        # logger.debug(f"行动决策Prompt:\n{decision_prompt}") # 取消注释以调试Prompt内容

        # 调用行动决策LLM的 generate_with_tools 方法
        # 注意：我们现在通过 action_llm_client.llm_client 来访问底层的 generate_with_tools
        decision_response: dict = await action_llm_client.llm_client.generate_with_tools(
            prompt=decision_prompt,
            tools=AVAILABLE_TOOLS_SCHEMA_FOR_GEMINI,
            is_stream=False,
        )
        logger.info(f"--- [Action ID: {action_id}, DocKey: {doc_key_for_updates}] 行动决策LLM调用完成 ---")

        if decision_response.get("error"):
            error_msg = decision_response.get("message", "行动决策LLM调用时返回了错误状态")
            logger.error(
                f"错误 [Action ID: {action_id}, DocKey: {doc_key_for_updates}]: 行动决策LLM调用失败 - {error_msg}"
            )
            final_result_for_shuang = f"我试图决定如何执行动作 '{action_description}' 时遇到了问题: {error_msg}"
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
            logger.info(
                f"信息 [Action ID: {action_id}, DocKey: {doc_key_for_updates}]: 行动决策LLM通过 'tool_calls' 字段返回了工具选择。"
            )
            tool_call_chosen = decision_response["tool_calls"][0]
        elif decision_response.get("text"):
            llm_text_output: str = decision_response.get("text", "").strip()
            logger.info(
                f"信息 [Action ID: {action_id}, DocKey: {doc_key_for_updates}]: 行动决策LLM的 'tool_calls' 为空，尝试从 'text' 字段解析。Text: '{llm_text_output[:200]}...'"
            )
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
                    logger.info(
                        f"信息 [Action ID: {action_id}, DocKey: {doc_key_for_updates}]: 成功从 'text' 字段解析出 'tool_calls'。"
                    )
                    tool_call_chosen = parsed_text_json["tool_calls"][0]
                else:
                    logger.warning(
                        f"警告 [Action ID: {action_id}, DocKey: {doc_key_for_updates}]: 'text' 字段内容不是预期的 tool_calls JSON 结构。Text: {llm_text_output}"
                    )
                    final_result_for_shuang = await report_action_failure(
                        intended_action_description=action_description,
                        intended_action_motivation=action_motivation,
                        reason_for_failure_short=f"行动决策模型未选择有效工具，其回复为：{llm_text_output[:100]}...",
                    )
                    action_was_successful = False
            except json.JSONDecodeError:
                logger.warning(
                    f"警告 [Action ID: {action_id}, DocKey: {doc_key_for_updates}]: 'text' 字段内容无法解析为JSON。Text: {llm_text_output}"
                )
                final_result_for_shuang = await report_action_failure(
                    intended_action_description=action_description,
                    intended_action_motivation=action_motivation,
                    reason_for_failure_short=f"行动决策模型的回复格式不正确：{llm_text_output[:100]}...",
                )
                action_was_successful = False

        if tool_call_chosen:
            tool_name: str | None = tool_call_chosen.get("function", {}).get("name")
            tool_args_str: str | None = tool_call_chosen.get("function", {}).get("arguments")

            if not tool_name or tool_args_str is None:
                logger.error(
                    f"错误 [Action ID: {action_id}, DocKey: {doc_key_for_updates}]: 解析出的工具调用缺少name或arguments。Tool call: {tool_call_chosen}"
                )
                final_result_for_shuang = "系统在理解工具调用指令时出错（缺少工具名称或参数）。"
                await arangodb_handler.update_action_status_in_document(
                    arango_db_for_updates,
                    collection_name_for_updates,
                    doc_key_for_updates,
                    action_id,
                    {
                        "status": "COMPLETED_FAILURE",
                        "error_message": "解析工具调用格式错误 (缺少name或arguments)",
                        "final_result_for_shuang": final_result_for_shuang,
                    },
                )
                return

            try:
                tool_args: dict = json.loads(tool_args_str)
                if not isinstance(tool_args, dict):
                    raise json.JSONDecodeError("Arguments not a dict", tool_args_str, 0)
            except json.JSONDecodeError:
                logger.error(
                    f"错误 [Action ID: {action_id}, DocKey: {doc_key_for_updates}]: 解析工具参数JSON字符串失败 - '{tool_args_str}'"
                )
                final_result_for_shuang = f"系统在理解动作 '{action_description}' 的工具参数时发生JSON解析错误。"
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

            await arangodb_handler.update_action_status_in_document(
                arango_db_for_updates,
                collection_name_for_updates,
                doc_key_for_updates,
                action_id,
                {"status": "TOOL_EXECUTING", "tool_selected": tool_name, "tool_args": tool_args},
            )
            logger.info(
                f"--- [Action ID: {action_id}, DocKey: {doc_key_for_updates}] 行动决策LLM选择工具: {tool_name}, 参数: {tool_args} ---"
            )

            raw_tool_output: str = "工具未返回任何输出或执行时发生错误。"

            if tool_name == "web_search":
                raw_tool_output = await search_web(**tool_args)
                if isinstance(raw_tool_output, str) and any(
                    err_keyword in raw_tool_output.lower() for err_keyword in ["error", "出错", "失败", "未能通过"]
                ):
                    final_result_for_shuang = str(raw_tool_output)
                    action_was_successful = False
                else:
                    logger.info(
                        f"--- [Action ID: {action_id}, DocKey: {doc_key_for_updates}] 网页搜索成功，准备调用信息总结LLM处理结果 ---"
                    )
                    original_query_for_summary: str = tool_args.get("query", action_description)
                    summary_prompt: str = INFORMATION_SUMMARY_PROMPT_TEMPLATE.format(
                        original_query_or_action=original_query_for_summary,
                        original_motivation=action_motivation,
                        raw_tool_output=str(raw_tool_output),
                    )
                    if summary_llm_client:
                        logger.info(f"--- [Action ID: {action_id}, DocKey: {doc_key_for_updates}] 调用信息总结LLM ---")
                        summary_response: dict = await summary_llm_client.llm_client.make_request(
                            prompt=summary_prompt, is_stream=False
                        )
                        if summary_response.get("error"):
                            error_msg_summary: str = summary_response.get("message", "信息总结LLM调用时返回错误")
                            logger.error(
                                f"错误 [Action ID: {action_id}, DocKey: {doc_key_for_updates}]: 信息总结LLM调用失败 - {error_msg_summary}"
                            )
                            final_result_for_shuang = (
                                f"我找到了关于 '{original_query_for_summary}' 的信息，但在尝试总结时遇到了问题：{error_msg_summary}。"
                                f" 这是原始内容的一部分：{str(raw_tool_output)[:250]}..."
                            )
                            action_was_successful = False
                        elif summary_response.get("text"):
                            final_result_for_shuang = summary_response.get("text")
                            logger.info(
                                f"--- [Action ID: {action_id}, DocKey: {doc_key_for_updates}] 信息总结LLM成功返回总结 ---"
                            )
                            action_was_successful = True
                        else:
                            logger.error(
                                f"错误 [Action ID: {action_id}, DocKey: {doc_key_for_updates}]: 信息总结LLM响应中缺少预期的文本内容。"
                            )
                            final_result_for_shuang = (
                                f"我找到了关于 '{original_query_for_summary}' 的信息，但总结服务未能正确处理它。"
                                f" 这是原始内容的一部分：{str(raw_tool_output)[:250]}..."
                            )
                            action_was_successful = False
                    else:
                        logger.critical(
                            f"严重错误 [Action ID: {action_id}, DocKey: {doc_key_for_updates}]: summary_llm_client 未初始化！无法进行信息总结。"
                        )
                        final_result_for_shuang = (
                            f"系统错误：信息总结服务当前不可用。关于 '{original_query_for_summary}' 的原始信息："
                            f" {str(raw_tool_output)[:250]}..."
                        )
                        action_was_successful = False

            elif tool_name == "report_action_failure":
                tool_args_for_reporter: dict = tool_args.copy()
                tool_args_for_reporter["intended_action_description"] = action_description
                tool_args_for_reporter["intended_action_motivation"] = action_motivation
                raw_tool_output = await report_action_failure(**tool_args_for_reporter)
                final_result_for_shuang = str(raw_tool_output)
                action_was_successful = False

            else:
                logger.warning(
                    f"警告 [Action ID: {action_id}, DocKey: {doc_key_for_updates}]: 未知的工具名称 '{tool_name}' 被行动决策LLM请求。"
                )
                raw_tool_output = (
                    f"系统请求了一个未知的工具 '{tool_name}' 来执行动作 '{action_description}'，但该工具未实现。"
                )
                final_result_for_shuang = raw_tool_output
                action_was_successful = False

            await arangodb_handler.update_action_status_in_document(
                arango_db_for_updates,
                collection_name_for_updates,
                doc_key_for_updates,
                action_id,
                {"tool_raw_output": str(raw_tool_output)[:2000]},
            )

        elif not tool_call_chosen:
            logger.info(
                f"最终 [Action ID: {action_id}, DocKey: {doc_key_for_updates}]: 行动决策LLM未能提供有效的工具调用指令。"
            )
            if final_result_for_shuang.startswith("尝试执行动作"):
                final_result_for_shuang = await report_action_failure(
                    intended_action_description=action_description,
                    intended_action_motivation=action_motivation,
                    reason_for_failure_short="行动决策模型未能提供有效的工具调用指令。",
                )
            action_was_successful = False

    except Exception as e:
        logger.critical(
            f"严重错误 [Action ID: {action_id}, DocKey: {doc_key_for_updates}]: 在 process_action_flow 主流程中发生意外: {e}",
            exc_info=True,
        )
        final_result_for_shuang = f"我尝试执行动作 '{action_description}' 时，系统发生了严重的内部错误: {str(e)}"
        action_was_successful = False
        await arangodb_handler.update_action_status_in_document(
            arango_db_for_updates,
            collection_name_for_updates,
            doc_key_for_updates,
            action_id,
            {"status": "CRITICAL_FAILURE", "error_message": str(e), "final_result_for_shuang": final_result_for_shuang},
        )
        return

    final_status: str = "COMPLETED_SUCCESS" if action_was_successful else "COMPLETED_FAILURE"
    await arangodb_handler.update_action_status_in_document(
        arango_db_for_updates,
        collection_name_for_updates,
        doc_key_for_updates,
        action_id,
        {"status": final_status, "final_result_for_shuang": final_result_for_shuang},
    )

    logger.info(
        f"--- [Action ID: {action_id}, DocKey: {doc_key_for_updates}] 动作处理流程完成。最终状态: {final_status} ---"
    )
    logger.debug(
        f"最终反馈给霜 (Action ID: {action_id}, DocKey: {doc_key_for_updates}): {str(final_result_for_shuang)[:300]}..."
    )
