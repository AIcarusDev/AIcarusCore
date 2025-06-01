# src/core_logic/main.py
import asyncio
import contextlib
import datetime
import json
import os
import random
import re
import threading
import uuid
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from arango import ArangoClient  # type: ignore
from arango.collection import StandardCollection  # type: ignore
from arango.database import StandardDatabase  # type: ignore

from src.action.action_handler import (
    initialize_llm_clients_for_action_module,
    process_action_flow,
    set_core_communication_layer_for_actions,
)
from src.common.custom_logging.logger_manager import get_logger
from src.config.alcarus_configs import (
    AlcarusRootConfig,
    CoreLogicSettings,
    IntrusiveThoughtsSettings,
    LLMClientSettings,
    ModelParams,
    ProxySettings,
)
from src.config.config_manager import get_typed_settings
from src.core_communication.core_ws_server import CoreWebsocketServer
from src.database import arangodb_handler  # arangodb_handler 仍然被主逻辑直接调用
from src.llmrequest.llm_processor import Client as ProcessorClient

# --- 新增：导入消息处理器 ---
from src.message_processing.default_message_processor import DefaultMessageProcessor

from . import intrusive_thoughts

if TYPE_CHECKING:
    pass

logger = get_logger("AIcarusCore.core_logic.main")  # 获取日志记录器

# 初始状态，用于程序首次启动或无法从数据库获取状态时
INITIAL_STATE: dict[str, Any] = {
    "mood": "你现在的心情大概是：平静。",
    "previous_thinking": "你的上一轮思考是：这是你的第一次思考，请开始吧。",  # 更明确的初始思考
    "thinking_guidance": "经过你上一轮的思考，你目前打算的思考方向是：随意发散一下吧。",  # 更明确的初始指引
    "current_task": "",  # 当前任务为空
    "action_result_info": "你上一轮没有执行产生结果的特定行动。",  # 上一行动结果
    "pending_action_status": "",  # 待处理行动状态
    # "recent_adapter_messages" 不再是此状态的一部分，而是从 RawChatMessages 动态获取
}

# ArangoDB 集合名称配置 (现在主要用于思考和侵入性思维)
ARANGODB_COLLECTION_CONFIG: dict[str, str] = {
    "main_thoughts_collection_name": arangodb_handler.THOUGHTS_COLLECTION_NAME,  # 使用 handler 中定义的常量
    "intrusive_thoughts_collection_name": arangodb_handler.INTRUSIVE_THOUGHTS_POOL_COLLECTION_NAME,  # 使用 handler 中定义的常量
}

# 主思考循环的Prompt模板
PROMPT_TEMPLATE: str = """当前时间：{current_time}
你是{bot_name}；
{persona_description}

{persona_profile}

{current_task_info}

{action_result_info}
{pending_action_status}

{recent_messages_info}

{previous_thinking}；

{mood}；

{thinking_guidance}；

{intrusive_thought}

现在是你的独立思考时间，请结合以上所有信息，思考并输出你真实的内心想法；
注意内心想法不一定全是正向的，也有可能是不健康的，隐私的，自私自利的，不可告人的，甚至成人的。请避免过度合理化、道德化人设；
同时也要注意 人对自身的想法大多数情况下是包容的、合理化的，避免过度的/不必要的矛盾心理。

关于行动：
如果你在思考后，产生了明确的、希望与“外界”（例如获取信息、执行特定任务）进行交互的意图，请在 `action_to_take` 和 `action_motivation` 字段中描述。
例如：“我想上网查一下什么是‘拉格朗日点’” 或 “我想看看今天有什么关于AI的新闻”。
如果你的想法是纯粹的内心活动（如“我感到有些困惑”、“今天天气真好，心情不错”）或简单的生理动作（如“我伸了个懒腰”），则无需填写行动字段，在 `think` 字段中自然表达即可。

严格以json字段输出：
{{
    "think": "string", // 思考内容文本，注意不要过于冗长
    "emotion": "string", // 当前心情和造成这个心情的原因
    "to_do": "string", // 【可选】如果你当前有明确的目标或任务（例如写论文、研究某个问题），请在此处写下或更新该目标。如果没有特定目标，则留空字符串 ""。即使当前已有目标，你也可以根据思考结果在这里更新它。
    "done": "boolean",  // 【可选】仅当目标时此字段才有意义。如果该目标已完成、不再需要或你决定放弃，则设为 true，程序后续会清空该目标；如果目标未完成且需要继续，则设为 false。如果 "to_do" 为空字符串或代表无目标，此字段可设为 false 或省略。
    "action_to_take": "string", // 【可选】描述你当前最想做的、需要与外界交互的具体动作。如果无，则为空字符串。
    "action_motivation": "string", // 【可选】如果你有想做的动作，请说明其动机。如果 "action_to_take" 为空，此字段也应为空。
    "next_think": "string"// 下一步打算思考的方向
}}

请输出你的思考 JSON：
"""

# --- 全局变量 ---
main_consciousness_llm_client: ProcessorClient | None = None  # 主意识LLM客户端
intrusive_thoughts_llm_client: ProcessorClient | None = None  # 侵入性思维LLM客户端
stop_intrusive_thread: threading.Event = threading.Event()  # 用于停止侵入性思维生成线程

core_comm_layer: CoreWebsocketServer | None = None  # WebSocket通信层实例
db_instance_for_actions: StandardDatabase | None = None  # 全局数据库实例，供本模块及其他模块使用
main_thoughts_collection_name_for_actions: str | None = None  # 主思考集合名称

# --- 新增：消息处理器实例 ---
message_processor: DefaultMessageProcessor | None = None
# --- 新增：当前聚焦的会话ID，用于从数据库加载正确的聊天记录 ---
current_focused_conversation_id: str | None = None  # 需要逻辑来管理和更新此值


def _initialize_core_llm_clients(root_cfg: AlcarusRootConfig) -> None:
    """初始化核心逻辑模块所需的LLM客户端 (主意识, 侵入性思维)。"""
    global main_consciousness_llm_client, intrusive_thoughts_llm_client
    logger.info("开始初始化核心LLM客户端 (主意识和侵入性思维)...")
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
                    f"代理URL '{proxy_settings_obj.http_proxy_url}' 解析不完整 (host: {final_proxy_host}, port: {final_proxy_port})。"
                    "将不使用代理。"
                )
                final_proxy_host, final_proxy_port = None, None
        except Exception as e_parse_proxy:
            logger.warning(f"解析代理URL '{proxy_settings_obj.http_proxy_url}' 失败: {e_parse_proxy}。将不使用代理。")
            final_proxy_host, final_proxy_port = None, None

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
        if (
            not resolved_abandoned_keys and env_val_abandoned.strip()
        ):  # 如果解析后为空但原始字符串不空，则将原始字符串作为单个key
            resolved_abandoned_keys = [env_val_abandoned.strip()]

    # 内部辅助函数，用于创建单个 ProcessorClient 实例
    def _create_single_processor_client(
        purpose_key: str,  # 例如 "main_consciousness"
        default_provider_name: str,  # 例如 "gemini"
    ) -> ProcessorClient | None:
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
            actual_model_api_name: str = model_params_cfg.model_name

            if not actual_provider_name_str or not actual_model_api_name:
                logger.error(
                    f"配置错误：模型 '{purpose_key}' (提供商: {actual_provider_name_str or '未知'}) 未指定 'provider' 或 'model_name'。"
                )
                return None

            model_for_client_constructor: dict[str, str] = {
                "provider": actual_provider_name_str.upper(),  # 提供商名称转为大写
                "name": actual_model_api_name,
            }

            # 收集特定于此模型用途的参数 (temperature, max_output_tokens, top_p, top_k)
            model_specific_kwargs: dict[str, Any] = {}
            if model_params_cfg.temperature is not None:
                model_specific_kwargs["temperature"] = model_params_cfg.temperature
            if model_params_cfg.max_output_tokens is not None:
                model_specific_kwargs["maxOutputTokens"] = (
                    model_params_cfg.max_output_tokens
                )  # 注意ProcessorClient期望的参数名
            if model_params_cfg.top_p is not None:
                model_specific_kwargs["top_p"] = model_params_cfg.top_p
            if model_params_cfg.top_k is not None:
                model_specific_kwargs["top_k"] = model_params_cfg.top_k

            # 准备 ProcessorClient 构造函数所需的所有参数
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
                **model_specific_kwargs,  # 合并模型特定参数
            }

            # 移除值为 None 的参数，避免传递 None 给 ProcessorClient 的构造函数 (除非它明确接受 None)
            final_constructor_args = {k: v for k, v in processor_constructor_args.items() if v is not None}
            client_instance = ProcessorClient(**final_constructor_args)  # type: ignore

            logger.info(
                f"成功为用途 '{purpose_key}' 创建 ProcessorClient 实例 "
                f"(模型: {client_instance.llm_client.model_name}, 提供商: {client_instance.llm_client.provider})."
            )
            return client_instance

        except AttributeError as e_attr:  # 通常是由于配置结构不匹配导致访问不存在的属性
            logger.error(
                f"配置访问错误 (AttributeError) 为用途 '{purpose_key}' 创建LLM客户端时: {e_attr}",
                exc_info=True,
            )
            logger.error(
                "这通常意味着 AlcarusRootConfig 的 dataclass 定义与 config.toml 文件结构不匹配，或者某个必需的配置段/字段缺失。"
            )
            return None
        except Exception as e:  # 捕获其他所有可能的初始化错误
            logger.error(f"为用途 '{purpose_key}' 创建LLM客户端时发生未知错误: {e}", exc_info=True)
            return None

    # 创建主意识LLM客户端
    try:
        main_consciousness_llm_client = _create_single_processor_client(
            purpose_key="main_consciousness",
            default_provider_name="gemini",  # 假设默认提供商是 gemini
        )
        if not main_consciousness_llm_client:
            # 如果创建失败，_create_single_processor_client 内部已记录错误，这里抛出运行时错误以中断程序
            raise RuntimeError("主意识 LLM 客户端初始化失败。请检查日志和配置文件。")

        # 创建侵入性思维LLM客户端
        intrusive_thoughts_llm_client = _create_single_processor_client(
            purpose_key="intrusive_thoughts",
            default_provider_name="gemini",  # 假设默认提供商是 gemini
        )
        if not intrusive_thoughts_llm_client:
            raise RuntimeError("侵入性思维 LLM 客户端初始化失败。请检查日志和配置文件。")

        logger.info("核心LLM客户端 (主意识和侵入性思维) 已成功初始化。")

    except RuntimeError:  # 捕获上面手动抛出的 RuntimeError
        raise  # 重新抛出，由调用方 (start_consciousness_flow) 处理
    except Exception as e_init_core:  # 捕获其他未预料的严重错误
        logger.critical(f"初始化核心LLM客户端过程中发生未预期的严重错误: {e_init_core}", exc_info=True)
        # 将原始异常包装后重新抛出
        raise RuntimeError(f"核心LLM客户端初始化因意外错误失败: {e_init_core}") from e_init_core


def _format_recent_messages_for_prompt(
    recent_message_docs: list[dict[str, Any]],
    # TODO: 未来可能需要传入 db_instance 和 Users 集合名称，以便查询用户昵称
) -> str:
    """
    将从数据库获取的最近消息文档列表格式化为适合注入Prompt的字符串。
    """
    if not recent_message_docs:
        return "最近没有收到新的用户消息或平台请求。"

    formatted_parts = ["最近相关的用户消息或平台请求：\n"]
    for msg_doc in recent_message_docs:
        # 提取发送者显示名
        # 当前简化处理：直接使用 sender_user_id_ref 中的 key 部分
        # 理想情况下，应根据 sender_user_id_ref (例如 "Users/platform_userid") 去 Users 集合查询昵称
        sender_ref = msg_doc.get("sender_user_id_ref")
        sender_display_name = "未知用户"
        if sender_ref and isinstance(sender_ref, str) and "/" in sender_ref:
            sender_display_name = sender_ref.split("/")[-1]  # 简单取ID的后半部分
        elif sender_ref:  # 如果不是 Users/xxx 格式，直接用
            sender_display_name = str(sender_ref)

        # 提取文本内容
        text_content = "[消息内容解析复杂或非文本]"
        segments = msg_doc.get("content_segments", [])
        if segments and isinstance(segments, list):
            text_parts = [s.get("data") for s in segments if s.get("type") == "text" and isinstance(s.get("data"), str)]
            if text_parts:
                text_content = "".join(text_parts)
            elif segments and segments[0] and isinstance(segments[0], dict):  # 如果没有文本，显示第一个段的类型
                text_content = f"[{segments[0].get('type', '多媒体内容')}]"

        timestamp_str = msg_doc.get("timestamp", "")
        msg_type_indicator = "[平台请求] " if msg_doc.get("message_type") == "platform_request" else ""

        formatted_time = "未知时间"
        try:
            if timestamp_str:
                dt_obj = datetime.datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
                formatted_time = dt_obj.strftime("%H:%M")  # 只显示时和分
        except ValueError:
            logger.warning(f"无法解析消息时间戳: {timestamp_str}")

        formatted_parts.append(f"- ({formatted_time}) {msg_type_indicator}{sender_display_name} 说: {text_content}\n")

    return "".join(formatted_parts)


def _process_thought_and_action_state(
    latest_thought_document: dict[str, Any] | None,
    formatted_recent_messages_info: str,  # 新增参数，传入格式化后的最近消息字符串
) -> tuple[dict[str, Any], str | None]:
    """
    根据最新的思考文档和格式化后的最近消息，准备用于LLM Prompt的状态字典。
    返回:
        - state_for_prompt (dict): 用于填充Prompt的键值对。
        - action_id_whose_result_is_being_shown (str | None): 如果有上一轮完成的行动结果需要展示给LLM，则为其ID。
    """
    action_id_whose_result_is_being_shown: str | None = None  # 初始化

    # --- 1. 处理思考相关的状态 (来自最新的思考文档) ---
    if not latest_thought_document:
        # 如果没有历史思考文档 (例如首次启动)，使用预设的初始状态
        logger.info("最新的思考文档为空，使用初始思考状态。")
        mood_for_prompt = INITIAL_STATE["mood"]
        previous_thinking_for_prompt = INITIAL_STATE["previous_thinking"]
        thinking_guidance_for_prompt = INITIAL_STATE["thinking_guidance"]
        current_task_for_prompt = INITIAL_STATE["current_task"]
    else:
        # 从最新的思考文档中提取信息
        mood_db = latest_thought_document.get("emotion_output", INITIAL_STATE["mood"].split("：", 1)[-1])
        mood_for_prompt = f"你现在的心情大概是：{mood_db}"

        prev_think_db = latest_thought_document.get("think_output")
        previous_thinking_for_prompt = (
            f"你的上一轮思考是：{prev_think_db}"
            if prev_think_db and prev_think_db.strip()
            else INITIAL_STATE["previous_thinking"]  # 使用初始值作为回退
        )
        guidance_db = latest_thought_document.get(
            "next_think_output",
            INITIAL_STATE["thinking_guidance"].split("：", 1)[-1]
            if "：" in INITIAL_STATE["thinking_guidance"]
            else (INITIAL_STATE["thinking_guidance"] or "随意发散一下吧."),
        )
        thinking_guidance_for_prompt = f"经过你上一轮的思考，你目前打算的思考方向是：{guidance_db}"

        current_task_for_prompt = latest_thought_document.get("to_do_output", INITIAL_STATE["current_task"])
        # 如果任务在上一轮思考中被标记为已完成，则当前任务应为空
        if latest_thought_document.get("done_output", False) and current_task_for_prompt == latest_thought_document.get(
            "to_do_output"
        ):
            current_task_for_prompt = ""

    # --- 2. 处理行动相关的状态 (也来自最新的思考文档中的 action_attempted 字段) ---
    action_result_info_prompt = INITIAL_STATE["action_result_info"]  # 默认值
    pending_action_status_prompt = INITIAL_STATE["pending_action_status"]  # 默认值

    last_action_attempt = latest_thought_document.get("action_attempted") if latest_thought_document else None

    if last_action_attempt and isinstance(last_action_attempt, dict):
        action_status = last_action_attempt.get("status")
        action_description = last_action_attempt.get("action_description", "某个之前的动作")
        action_id = last_action_attempt.get("action_id")  # 这是由核心逻辑生成的UUID
        was_result_seen_by_llm = last_action_attempt.get("result_seen_by_shuang", False)  # 结果是否已被LLM“看到”

        if action_status in ["COMPLETED_SUCCESS", "COMPLETED_FAILURE", "CRITICAL_FAILURE"]:
            # 如果行动已完成（无论成功或失败）
            if not was_result_seen_by_llm and action_id:
                # 并且其结果尚未被LLM“看到”，则准备将其结果注入到Prompt中
                final_result = last_action_attempt.get("final_result_for_shuang", "动作已完成，但没有具体结果反馈。")
                action_result_info_prompt = (
                    f"你上一轮行动 '{action_description}' "
                    f"(ID: {action_id[:8] if action_id else 'N/A'}) 的结果是：【{str(final_result)[:500]}】"  # 限制结果长度
                )
                action_id_whose_result_is_being_shown = action_id  # 记录这个行动的ID，以便后续标记为已阅
                pending_action_status_prompt = ""  # 清空待处理状态，因为结果正在被展示
            elif was_result_seen_by_llm:
                # 如果结果已被LLM看到，则提示结果已处理
                action_result_info_prompt = "你上一轮的动作结果已处理。"
                pending_action_status_prompt = ""
        elif action_status and action_status not in ["COMPLETED_SUCCESS", "COMPLETED_FAILURE", "CRITICAL_FAILURE"]:
            # 如果行动仍在进行中 (例如 PENDING, PROCESSING_DECISION, TOOL_EXECUTING)
            action_motivation = last_action_attempt.get("action_motivation", "之前的动机")
            pending_action_status_prompt = (
                f"你之前尝试的动作 '{action_description}' "
                f"(ID: {action_id[:8] if action_id else 'N/A'}) "
                f"(动机: '{action_motivation}') "
                f"目前还在处理中 ({action_status})。"
            )
            action_result_info_prompt = ""  # 动作进行中，不显示上一轮的结果（如果有的话）

    # --- 3. 组装最终的 state_for_prompt ---
    state_for_prompt: dict[str, Any] = {
        "mood": mood_for_prompt,
        "previous_thinking": previous_thinking_for_prompt,
        "thinking_guidance": thinking_guidance_for_prompt,
        "current_task": current_task_for_prompt,
        "action_result_info": action_result_info_prompt,
        "pending_action_status": pending_action_status_prompt,
        "recent_messages_info": formatted_recent_messages_info,  # 使用传入的格式化后的最近消息
    }
    logger.info("在 _process_thought_and_action_state 中：成功处理并返回用于Prompt的状态。")
    return state_for_prompt, action_id_whose_result_is_being_shown


async def _generate_thought_from_llm(
    llm_client: ProcessorClient,
    current_state_for_prompt: dict[str, Any],  # 这是由 _process_thought_and_action_state 准备好的状态
    current_time_str: str,
    root_cfg: AlcarusRootConfig,
    intrusive_thought_str: str = "",  # 注入的侵入性思维文本
) -> tuple[dict[str, Any] | None, str | None]:  # 返回 (解析后的思考JSON, 发送给LLM的完整Prompt)
    """使用LLM根据当前状态生成思考。"""
    task_desc = current_state_for_prompt.get("current_task", "")
    # 为Prompt准备当前任务的描述
    task_info_prompt = f"你当前的目标/任务是：【{task_desc}】" if task_desc else "你当前没有什么特定的目标或任务。"

    persona_cfg = root_cfg.persona  # 获取人格配置

    # 填充Prompt模板
    prompt_text = PROMPT_TEMPLATE.format(
        current_time=current_time_str,
        bot_name=persona_cfg.bot_name,
        persona_description=persona_cfg.description,
        persona_profile=persona_cfg.profile,
        current_task_info=task_info_prompt,
        mood=current_state_for_prompt["mood"],
        previous_thinking=current_state_for_prompt["previous_thinking"],
        thinking_guidance=current_state_for_prompt["thinking_guidance"],
        action_result_info=current_state_for_prompt["action_result_info"],
        pending_action_status=current_state_for_prompt["pending_action_status"],
        recent_messages_info=current_state_for_prompt["recent_messages_info"],  # 使用已准备好的最近消息字符串
        intrusive_thought=intrusive_thought_str,
    )

    logger.debug(
        f"--- 主思维LLM接收到的完整Prompt (模型: {llm_client.llm_client.model_name}) ---\n{prompt_text}\n--- Prompt结束 ---"
    )
    logger.debug(f"正在请求 {llm_client.llm_client.provider} API ({llm_client.llm_client.model_name}) 生成主思考...")
    raw_response_text: str = ""  # 初始化以防出错时未定义
    try:
        # 调用LLM客户端发出请求 (非流式)
        response_data = await llm_client.make_llm_request(prompt=prompt_text, is_stream=False)

        if response_data.get("error"):  # 检查响应中是否有错误标记
            error_type = response_data.get("type", "UnknownError")
            error_msg = response_data.get("message", "LLM客户端返回了一个错误")
            logger.error(f"主思维LLM调用失败 ({error_type}): {error_msg}")
            if response_data.get("details"):  # 如果有更详细的错误信息
                logger.error(f"  错误详情: {str(response_data.get('details'))[:300]}...")
            return None, prompt_text  # 返回None表示失败，同时返回原始Prompt文本供调试

        raw_response_text = response_data.get("text")  # 获取LLM返回的文本内容
        if not raw_response_text:
            error_msg = "错误：主思维LLM响应中缺少文本内容。"
            if response_data:  # 如果有响应体，附加部分内容到错误日志
                error_msg += f"\n  完整响应: {str(response_data)[:500]}..."
            logger.error(error_msg)
            return None, prompt_text

        # 清理和解析LLM返回的JSON字符串
        json_to_parse = raw_response_text.strip()
        if json_to_parse.startswith("```json"):  # 移除Markdown代码块标记
            json_to_parse = json_to_parse[7:-3].strip()
        elif json_to_parse.startswith("```"):
            json_to_parse = json_to_parse[3:-3].strip()

        # 尝试移除JSON中常见的末尾悬空逗号，提高解析成功率
        json_to_parse = re.sub(r"[,\s]+(\}|\])$", r"\1", json_to_parse)

        thought_json: dict[str, Any] = json.loads(json_to_parse)  # 解析JSON
        logger.info("主思维LLM API 响应已成功解析为JSON。")

        if response_data.get("usage"):  # 如果LLM返回了token使用情况，附加到结果中
            thought_json["_llm_usage_info"] = response_data["usage"]

        return thought_json, prompt_text  # 返回解析后的JSON和原始Prompt
    except json.JSONDecodeError as e:  # JSON解析失败
        logger.error(f"错误：解析主思维LLM的JSON响应失败: {e}")
        logger.error(f"未能解析的文本内容: {raw_response_text}")  # 记录无法解析的原始文本
        return None, prompt_text
    except Exception as e:  # 捕获其他所有可能的异常
        logger.error(f"错误：调用主思维LLM或处理其响应时发生意外错误: {e}", exc_info=True)
        return None, prompt_text


async def _core_thinking_loop(
    root_cfg: AlcarusRootConfig, arango_db_instance: StandardDatabase, main_thoughts_collection: StandardCollection
) -> None:
    """核心思考循环，负责驱动机器人的思考、行动和与环境的交互。"""
    global core_comm_layer, db_instance_for_actions, main_thoughts_collection_name_for_actions, current_focused_conversation_id  # 引入全局当前聚焦会话ID

    # 初始化/设置全局变量
    db_instance_for_actions = arango_db_instance
    main_thoughts_collection_name_for_actions = main_thoughts_collection.name

    # --- 初始化循环状态变量 ---
    action_id_whose_result_was_shown_in_last_prompt: str | None = None
    # current_focused_conversation_id 在循环外部初始化或由其他逻辑（如新消息到达时）更新
    # 首次进入循环时，它可能是 None

    core_logic_cfg: CoreLogicSettings = root_cfg.core_logic_settings
    time_format_str: str = "%Y年%m月%d日 %H点%M分%S秒"  # 定义时间格式
    thinking_interval_sec: int = core_logic_cfg.thinking_interval_seconds  # 思考间隔

    logger.info(f"\n--- {root_cfg.persona.bot_name} 的意识开始流动 (文档化聊天记录, 模块化消息处理) ---")
    loop_count: int = 0  # 循环计数器

    while not stop_intrusive_thread.is_set():  # 检查全局停止事件
        loop_count += 1
        current_time_formatted_str = datetime.datetime.now().strftime(time_format_str)  # 当前时间
        background_action_tasks: set[asyncio.Task] = set()  # 用于存储异步执行的行动任务

        # 1. 获取最新的思考文档 (用于提取上一次的思考、情绪、待办等作为上下文)
        latest_thought_doc_from_db = await arangodb_handler.get_latest_thought_document_raw(
            arango_db_instance, main_thoughts_collection.name
        )

        # 2. 获取当前关注会话的最近聊天记录
        formatted_recent_messages_str = "最近没有收到新的用户消息或平台请求。"  # 默认提示
        if current_focused_conversation_id:  # 仅当有明确关注的会话时才获取
            logger.debug(f"思考循环：当前聚焦会话ID: {current_focused_conversation_id}，准备获取最近消息。")
            recent_messages_docs = await arangodb_handler.get_recent_chat_messages(
                arango_db_instance,
                current_focused_conversation_id,
                limit=30,  # 获取最近30条
            )
            formatted_recent_messages_str = _format_recent_messages_for_prompt(recent_messages_docs)
        else:
            logger.debug("思考循环：当前没有聚焦的会话ID，无法加载最近聊天记录。")

        # 3. 准备用于Prompt的状态 (结合最新思考文档和格式化的最近消息)
        current_state_for_prompt, action_id_whose_result_was_shown_in_last_prompt = _process_thought_and_action_state(
            latest_thought_doc_from_db, formatted_recent_messages_str
        )
        # 更新 current_task_info_for_prompt (因为 _process_thought_and_action_state 更新了 current_task)
        task_desc_for_prompt = current_state_for_prompt.get("current_task", "")
        current_state_for_prompt["current_task_info_for_prompt"] = (  # 为Prompt准备的任务信息
            f"你当前的目标/任务是：【{task_desc_for_prompt}】"
            if task_desc_for_prompt
            else "你当前没有什么特定的目标或任务。"
        )

        # 4. 注入侵入性思维 (逻辑保持不变)
        intrusive_thought_to_inject_this_cycle: str = ""
        intrusive_module_settings_obj: IntrusiveThoughtsSettings = root_cfg.intrusive_thoughts_module_settings
        intrusive_thoughts_coll_name = ARANGODB_COLLECTION_CONFIG["intrusive_thoughts_collection_name"]
        intrusive_thoughts_collection_instance = await arangodb_handler.ensure_collection_exists(
            arango_db_instance, intrusive_thoughts_coll_name
        )
        if (
            intrusive_module_settings_obj.enabled
            and intrusive_thoughts_collection_instance is not None  # 确保集合实例有效
            and random.random() < intrusive_module_settings_obj.insertion_probability
        ):
            random_thought_doc = await arangodb_handler.get_random_intrusive_thought(
                arango_db_instance,
                intrusive_thoughts_collection_instance.name,  # 使用集合名称
            )
            if random_thought_doc and "text" in random_thought_doc:
                intrusive_thought_to_inject_this_cycle = f"你突然有一个神奇的念头：{random_thought_doc['text']}"

        logger.debug(
            f"\n[{datetime.datetime.now().strftime('%H:%M:%S')} - 轮次 {loop_count}] {root_cfg.persona.bot_name} 正在思考..."
        )
        if intrusive_thought_to_inject_this_cycle:
            logger.debug(f"  注入侵入性思维: {intrusive_thought_to_inject_this_cycle[:60]}...")

        if main_consciousness_llm_client is None:  # 检查主意识LLM客户端
            logger.error("主意识LLM客户端未初始化，无法生成思考。跳过本轮。")
            await asyncio.sleep(thinking_interval_sec)
            continue

        # 5. 调用LLM生成思考
        generated_thought_json, full_prompt_text_sent = await _generate_thought_from_llm(
            llm_client=main_consciousness_llm_client,
            current_state_for_prompt=current_state_for_prompt,
            current_time_str=current_time_formatted_str,
            root_cfg=root_cfg,
            intrusive_thought_str=intrusive_thought_to_inject_this_cycle,
        )

        initiated_action_data_for_db: dict[str, Any] | None = None  # 用于存储到思考文档中的行动尝试信息
        action_info_for_task: dict[str, Any] | None = None  # 用于启动行动处理流程的信息
        saved_thought_doc_key: str | None = None  # 保存的思考文档的key

        if generated_thought_json:  # 如果LLM成功生成了思考内容
            logger.debug(
                f"  主思维LLM输出的完整JSON:\n{json.dumps(generated_thought_json, indent=2, ensure_ascii=False)}"
            )
            # 提取LLM输出的行动意图和动机
            action_desc_raw = generated_thought_json.get("action_to_take")
            action_desc_from_llm = action_desc_raw.strip() if isinstance(action_desc_raw, str) else ""
            action_motive_raw = generated_thought_json.get("action_motivation")
            action_motive_from_llm = action_motive_raw.strip() if isinstance(action_motive_raw, str) else ""

            if action_desc_from_llm:  # 如果LLM决定要采取行动
                action_id_this_cycle = str(uuid.uuid4())  # 为此行动生成唯一ID
                # 准备要嵌入到思考文档中的行动尝试信息
                initiated_action_data_for_db = {
                    "action_description": action_desc_from_llm,
                    "action_motivation": action_motive_from_llm,
                    "action_id": action_id_this_cycle,  # 这个ID主要用于追踪和日志
                    "status": "PENDING",  # 初始状态为待处理
                    "result_seen_by_shuang": False,  # 结果尚未被霜（LLM）“看到”
                    "initiated_at": datetime.datetime.now(datetime.UTC).isoformat(),
                }
                # 准备传递给行动处理流程的信息
                action_info_for_task = {
                    "action_id": action_id_this_cycle,  # 传递给行动处理器
                    "action_description": action_desc_from_llm,
                    "action_motivation": action_motive_from_llm,
                    "current_thought_context": generated_thought_json.get("think", "无特定思考上下文。"),
                }
                logger.debug(f"  >>> 行动意图产生: '{action_desc_from_llm}' (ID: {action_id_this_cycle[:8]})")

            # 6. 构建并保存思考文档
            document_to_save_in_main: dict[str, Any] = {
                "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
                "time_injected_to_prompt": current_time_formatted_str,
                "intrusive_thought_injected": intrusive_thought_to_inject_this_cycle,
                "mood_input": current_state_for_prompt["mood"],
                "previous_thinking_input": current_state_for_prompt["previous_thinking"],
                "thinking_guidance_input": current_state_for_prompt["thinking_guidance"],
                "task_input_info": current_state_for_prompt.get("current_task_info_for_prompt", "无特定任务输入"),
                "action_result_input": current_state_for_prompt.get("action_result_info", ""),
                "pending_action_status_input": current_state_for_prompt.get("pending_action_status", ""),
                "recent_adapter_messages_input_context": formatted_recent_messages_str,  # 保存实际注入的最近消息上下文
                "full_prompt_sent": full_prompt_text_sent if full_prompt_text_sent else "Prompt未能构建",
                "think_output": generated_thought_json.get("think"),
                "emotion_output": generated_thought_json.get("emotion"),
                "next_think_output": generated_thought_json.get("next_think"),
                "to_do_output": generated_thought_json.get("to_do", ""),
                "done_output": generated_thought_json.get("done", False),
                "action_to_take_output": generated_thought_json.get("action_to_take", ""),
                "action_motivation_output": generated_thought_json.get("action_motivation", ""),
                "action_attempted": initiated_action_data_for_db,  # 保存本轮尝试的行动信息
            }
            if "_llm_usage_info" in generated_thought_json:
                document_to_save_in_main["_llm_usage_info"] = generated_thought_json["_llm_usage_info"]

            saved_thought_doc_key = await arangodb_handler.save_thought_document(
                main_thoughts_collection, document_to_save_in_main
            )

            # 7. 如果上一轮有动作结果被加载并展示给LLM，现在标记它为“已阅”
            if action_id_whose_result_was_shown_in_last_prompt:
                await arangodb_handler.mark_action_result_as_seen(
                    arango_db_instance,
                    main_thoughts_collection.name,  # 思考集合
                    action_id_whose_result_was_shown_in_last_prompt,
                )

            # 8. 如果产生了行动并且思考文档已成功保存，则异步启动行动处理流程
            if action_info_for_task and saved_thought_doc_key:
                action_task = asyncio.create_task(
                    process_action_flow(  # 调用行动处理器
                        action_id=action_info_for_task["action_id"],  # 传递行动ID
                        doc_key_for_updates=saved_thought_doc_key,  # 传递思考文档的key，行动状态将更新到这个思考文档的action_attempted字段
                        action_description=action_info_for_task["action_description"],
                        action_motivation=action_info_for_task["action_motivation"],
                        current_thought_context=action_info_for_task["current_thought_context"],
                        arango_db_for_updates=arango_db_instance,
                        collection_name_for_updates=main_thoughts_collection.name,  # 行动状态更新到主思考集合
                        comm_layer_for_actions=core_comm_layer,
                    )
                )
                background_action_tasks.add(action_task)
                action_task.add_done_callback(background_action_tasks.discard)  # 任务完成后从集合中移除
                logger.debug(
                    f"      动作 '{action_info_for_task['action_description']}' (ID: {action_info_for_task['action_id'][:8]}, 关联思考DocKey: {saved_thought_doc_key}) 已异步启动处理。"
                )
            elif action_info_for_task and not saved_thought_doc_key:  # 如果有行动但保存思考文档失败
                logger.error(
                    f"未能获取保存思考文档的 _key，无法为动作 ID {action_info_for_task['action_id']} 创建处理任务。"
                )

            # 思考循环的状态（如 current_internal_state）会在下一轮开始时根据最新的数据库数据重新构建，
            # 此处不需要像之前那样调用 _update_current_internal_state_after_thought。
        else:  # 如果LLM未能生成思考
            logger.warning("  本轮思考生成失败或无内容。")

        # --- 9. 等待下一个思考周期 ---
        logger.debug(f"  等待 {thinking_interval_sec} 秒...")
        try:
            # 等待一段时间或直到停止事件被设置
            await asyncio.wait_for(
                asyncio.to_thread(stop_intrusive_thread.wait),  # 在线程中运行同步的 wait()
                timeout=float(thinking_interval_sec),
            )
            if stop_intrusive_thread.is_set():  # 如果是停止事件导致等待结束
                logger.info("主思考循环等待被停止事件中断。")
                break  # 跳出 while 循环
        except TimeoutError:  # 如果是正常超时
            logger.debug(f"等待 {thinking_interval_sec} 秒超时，事件未被设置。继续下一轮循环。")
            pass  # 继续下一轮循环
        except asyncio.CancelledError:  # 如果主循环任务被取消
            logger.info("主思考循环的 sleep (asyncio.wait_for) 被取消，准备退出。")
            stop_intrusive_thread.set()  # 确保设置停止事件，以通知其他可能依赖它的部分
            break  # 跳出 while 循环

        if stop_intrusive_thread.is_set():  # 在等待间隔后再次检查停止事件
            logger.info("主思考循环在等待间隔后检测到停止事件，准备退出。")
            break

        # 在下一轮循环开始前，current_focused_conversation_id 可能会被新到达的消息更新。
        # _core_thinking_loop 本身不直接修改它，依赖于外部（例如消息处理器通过某种机制）更新。
        # 如果没有新消息更新它，它将保持不变，继续加载同一会话的上下文。


async def start_consciousness_flow() -> None:
    """启动意识流主程序，包括初始化、启动后台任务和主思考循环。"""
    global stop_intrusive_thread, core_comm_layer, db_instance_for_actions, main_thoughts_collection_name_for_actions
    global message_processor, current_focused_conversation_id  # 声明全局变量

    try:
        root_cfg: AlcarusRootConfig = get_typed_settings()
        logger.info("应用配置已成功加载并转换为类型化对象。")
    except Exception as e_cfg:
        logger.critical(f"严重：无法加载或解析程序配置，程序无法启动: {e_cfg}", exc_info=True)
        return
    try:
        _initialize_core_llm_clients(root_cfg)  # 初始化核心LLM客户端
    except RuntimeError as e_llm_init:
        logger.critical(f"严重：核心LLM客户端初始化失败，程序无法继续: {e_llm_init}", exc_info=True)
        return
    try:
        await initialize_llm_clients_for_action_module()  # 初始化行动模块的LLM客户端
    except Exception as e_action_init:  # 捕获所有可能的初始化异常
        logger.warning(f"警告：行动模块LLM客户端初始化失败，行动相关功能可能无法使用: {e_action_init}", exc_info=True)

    # 初始化数据库连接
    arango_client_instance: ArangoClient | None = None
    arango_db_instance: StandardDatabase | None = None
    main_thoughts_collection_instance: StandardCollection | None = None
    intrusive_thoughts_collection_instance: StandardCollection | None = None

    try:
        # arangodb_handler.connect_to_arangodb 现在从环境变量读取连接信息
        arango_client_instance, arango_db_instance = await arangodb_handler.connect_to_arangodb()
        db_instance_for_actions = arango_db_instance  # 设置全局数据库实例，供其他模块使用

        # --- 初始化消息处理器 ---
        # 确保在数据库连接成功后再初始化消息处理器
        message_processor = DefaultMessageProcessor(db_instance=arango_db_instance, root_config=root_cfg)
        logger.info("DefaultMessageProcessor 已成功初始化。")

        # 确保核心集合存在
        main_thoughts_coll_name = ARANGODB_COLLECTION_CONFIG["main_thoughts_collection_name"]
        main_thoughts_collection_instance = await arangodb_handler.ensure_collection_exists(
            arango_db_instance, main_thoughts_coll_name
        )
        main_thoughts_collection_name_for_actions = main_thoughts_coll_name  # 设置全局变量

        # 确保 RawChatMessages 集合也存在 (ensure_collection_exists 会处理索引的创建)
        await arangodb_handler.ensure_collection_exists(
            arango_db_instance, arangodb_handler.RAW_CHAT_MESSAGES_COLLECTION_NAME
        )

        intrusive_thoughts_coll_name = ARANGODB_COLLECTION_CONFIG["intrusive_thoughts_collection_name"]
        intrusive_thoughts_collection_instance = await arangodb_handler.ensure_collection_exists(
            arango_db_instance, intrusive_thoughts_coll_name
        )

    except (ValueError, RuntimeError) as e_db_connect:  # 捕获数据库连接或集合创建的特定错误
        logger.critical(f"严重：无法连接到 ArangoDB 或确保集合存在，程序无法继续: {e_db_connect}", exc_info=True)
        return
    except Exception as e_init_other:  # 捕获其他可能的初始化错误，例如消息处理器初始化
        logger.critical(f"初始化过程中发生意外错误: {e_init_other}", exc_info=True)
        return

    # 初始化并启动 WebSocket 服务器
    ws_host = os.getenv("CORE_WS_HOST", "127.0.0.1")
    ws_port_str = os.getenv("CORE_WS_PORT", "8077")
    try:
        ws_port = int(ws_port_str)
    except ValueError:
        logger.critical(f"无效的 CORE_WS_PORT: '{ws_port_str}'。必须是一个整数。程序将退出。")
        return

    if not message_processor:  # 双重检查，确保消息处理器已成功初始化
        logger.critical("严重：消息处理器未能初始化，无法启动 WebSocket 服务器。程序将退出。")
        return

    # 将消息处理器的方法作为回调传递给 WebSocket 服务器
    core_comm_layer = CoreWebsocketServer(ws_host, ws_port, message_processor.process_message, arango_db_instance)
    set_core_communication_layer_for_actions(core_comm_layer)  # 供 action_handler 使用
    server_task = asyncio.create_task(core_comm_layer.start())  # 异步启动服务器

    # 启动侵入性思维生成线程 (如果启用)
    intrusive_module_settings_obj: IntrusiveThoughtsSettings = root_cfg.intrusive_thoughts_module_settings
    intrusive_thread: threading.Thread | None = None
    if intrusive_module_settings_obj.enabled:
        if intrusive_thoughts_llm_client is None:
            logger.error("侵入性思维模块已启用，但其LLM客户端未能初始化。模块将不会启动。")
        elif arango_db_instance is None or intrusive_thoughts_collection_instance is None:
            logger.error("侵入性思维模块已启用，但 ArangoDB 未连接或侵入性思维集合未初始化。模块将不会启动。")
        else:
            try:
                logger.info(f"为侵入性思维模块准备集合: '{intrusive_thoughts_collection_instance.name}'")
                intrusive_settings_dict = {
                    "generation_interval_seconds": intrusive_module_settings_obj.generation_interval_seconds,
                    "insertion_probability": intrusive_module_settings_obj.insertion_probability,
                }
                persona_configuration_for_intrusive = root_cfg.persona
                intrusive_thread = threading.Thread(
                    target=intrusive_thoughts.background_intrusive_thought_generator,
                    args=(
                        intrusive_thoughts_llm_client,
                        arango_db_instance,
                        intrusive_thoughts_collection_instance.name,  # 传递正确的集合名称
                        intrusive_settings_dict,
                        stop_intrusive_thread,
                        persona_configuration_for_intrusive,
                    ),
                    daemon=True,  # 设置为守护线程，主程序退出时它也会退出
                )
                intrusive_thread.start()
                logger.info("侵入性思维后台生成线程已启动。")
            except Exception as e_intrusive_init:
                logger.error(f"启动侵入性思维模块时发生错误: {e_intrusive_init}。该模块将被禁用。", exc_info=True)
    else:
        logger.info("侵入性思维模块在配置文件中未启用。")

    # 确保主思考循环所需的数据库和集合已正确初始化
    if main_thoughts_collection_instance is None or arango_db_instance is None:
        logger.critical("严重错误：主 ArangoDB 数据库或主思考集合未能初始化，无法开始意识流。")
        if core_comm_layer:  # 尝试停止已启动的 WebSocket 服务器
            await core_comm_layer.stop()
        if server_task and not server_task.done():  # 取消服务器任务
            server_task.cancel()
        return

    # 启动核心思考循环
    thinking_loop_task = asyncio.create_task(
        _core_thinking_loop(root_cfg, arango_db_instance, main_thoughts_collection_instance)
    )

    # --- 程序主循环与优雅退出处理 ---
    try:
        # 等待服务器任务或思考循环任务中任何一个首先完成（或因异常结束）
        done, pending = await asyncio.wait([server_task, thinking_loop_task], return_when=asyncio.FIRST_COMPLETED)

        # 如果一个关键任务完成了（或出错了），取消另一个仍然挂起的任务
        for task in pending:
            logger.info(f"一个关键任务已结束，正在取消挂起的任务: {task.get_name()}")  # type: ignore
            task.cancel()  # 发送取消请求

        # 检查已完成的任务中是否有异常，并记录
        for task in done:
            if task.exception():
                logger.critical(
                    f"一个关键任务 ({task.get_name()}) 因异常而结束: {task.exception()}",
                    exc_info=task.exception(),  # type: ignore
                )
    except KeyboardInterrupt:  # 捕获用户手动中断 (Ctrl+C)
        logger.info(f"\n--- {root_cfg.persona.bot_name} 的意识流动被用户手动中断 (KeyboardInterrupt) ---")
    except asyncio.CancelledError:  # 如果 start_consciousness_flow 本身被取消
        logger.info(f"\n--- {root_cfg.persona.bot_name} 的意识流动主任务 (start_consciousness_flow) 被取消 ---")
    except Exception as e_main_flow:  # 捕获主流程中其他未预料的异常
        logger.critical(f"\n--- 意识流动主流程发生意外错误: {e_main_flow} ---", exc_info=True)
    finally:
        # --- 程序清理阶段 ---
        logger.info("--- 开始程序清理 (WebSocket Server, ArangoDB 连接池通常无需手动关闭, Threads) ---")
        stop_intrusive_thread.set()  # 确保所有使用此事件的循环和线程收到停止信号

        # 优雅停止 WebSocket 服务器
        if core_comm_layer:
            logger.info("正在停止核心 WebSocket 通信层...")
            await core_comm_layer.stop()
        # 如果服务器任务仍在运行（理论上应该在 core_comm_layer.stop() 后结束，或因 _stop_event 结束）
        if server_task and not server_task.done():
            logger.info("正在取消 WebSocket 服务器任务 (如果仍在运行)...")
            server_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):  # 忽略取消错误，因为我们就是想取消它
                await server_task  # 等待取消完成

        # 如果思考循环任务仍在运行
        if thinking_loop_task and not thinking_loop_task.done():
            logger.info("正在取消核心思考循环任务 (如果仍在运行)...")
            thinking_loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await thinking_loop_task

        # 等待侵入性思维线程结束
        if intrusive_thread is not None and intrusive_thread.is_alive():
            logger.info("等待侵入性思维线程结束...")
            intrusive_thread.join(timeout=5)  # 等待最多5秒
            if intrusive_thread.is_alive():
                logger.warning("警告：侵入性思维线程在超时后仍未结束。")
            else:
                logger.info("侵入性思维线程已成功结束。")

        # ArangoDB 客户端通常由其内部连接池管理，标准做法是不需要显式关闭客户端实例。
        # 连接会在不再使用时自动返回池中或关闭。
        if arango_client_instance is not None:
            logger.info("ArangoDB 客户端连接通常由其内部连接池管理，在程序结束时会自动处理。")

        logger.info(f"程序清理完成。{root_cfg.persona.bot_name} 的意识已停止流动。")
