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

from aicarus_protocols import MessageBase, Seg
from arango import ArangoClient  # type: ignore
from arango.collection import StandardCollection  # type: ignore
from arango.database import StandardDatabase  # type: ignore
from websockets.server import WebSocketServerProtocol  # type: ignore # 用于类型提示

from src.action.action_handler import (
    initialize_llm_clients_for_action_module,
    process_action_flow,
    set_core_communication_layer_for_actions,  # 新增导入
)
from src.common.custom_logging.logger_manager import get_logger
from src.config.alcarus_configs import (
    AlcarusRootConfig,
    CoreLogicSettings,
    DatabaseSettings,
    IntrusiveThoughtsSettings,
    LLMClientSettings,
    ModelParams,
    ProxySettings,
)
from src.config.config_manager import get_typed_settings

# --- 核心通信层 ---
from src.core_communication.core_ws_server import CoreWebsocketServer  # 新增导入
from src.database import arangodb_handler
from src.llmrequest.llm_processor import Client as ProcessorClient

from . import intrusive_thoughts

if TYPE_CHECKING:
    pass

logger = get_logger("AIcarusCore.core_logic.main")  # 获取日志记录器

INITIAL_STATE: dict[str, Any] = {
    "mood": "你现在的心情大概是：平静。",
    "previous_thinking": " ",
    "thinking_guidance": " ",
    "current_task": "",
    "action_result_info": "你上一轮没有执行产生结果的特定行动。",
    "pending_action_status": "",
    "recent_adapter_messages": [],  # 新增字段，用于存储来自适配器的最近消息
}

ARANGODB_COLLECTION_CONFIG: dict[str, str] = {
    "main_thoughts_collection_name": "thoughts_collection",
    "intrusive_thoughts_collection_name": "intrusive_thoughts_pool",
}

# 更新后的 PROMPT_TEMPLATE，加入了最近消息的上下文
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

main_consciousness_llm_client: ProcessorClient | None = None  # LLM客户端实例
intrusive_thoughts_llm_client: ProcessorClient | None = None  # 侵入性思维的LLM客户端实例
stop_intrusive_thread: threading.Event = threading.Event()  # 用于停止侵入性思维生成线程的事件

# --- 全局 CoreWebsocketServer 实例 ---
core_comm_layer: CoreWebsocketServer | None = None
# --- 全局 ArangoDB 实例，供其他模块（如 action_handler）在必要时访问 ---
db_instance_for_actions: StandardDatabase | None = None
main_thoughts_collection_name_for_actions: str | None = None


def _initialize_core_llm_clients(root_cfg: AlcarusRootConfig) -> None:
    """初始化核心逻辑所需的LLM客户端。"""
    # (此函数内容与你之前提供的版本基本一致，确保它能正确工作)
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
        if not resolved_abandoned_keys and env_val_abandoned.strip():
            resolved_abandoned_keys = [env_val_abandoned.strip()]

    def _create_single_processor_client(
        purpose_key: str,
        default_provider_name: str,
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
                "provider": actual_provider_name_str.upper(),
                "name": actual_model_api_name,
            }

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
                f"成功为用途 '{purpose_key}' 创建 ProcessorClient 实例 "
                f"(模型: {client_instance.llm_client.model_name}, 提供商: {client_instance.llm_client.provider})."
            )
            return client_instance

        except AttributeError as e_attr:
            logger.error(
                f"配置访问错误 (AttributeError) 为用途 '{purpose_key}' 创建LLM客户端时: {e_attr}",
                exc_info=True,
            )
            logger.error(
                "这通常意味着 AlcarusRootConfig 的 dataclass 定义与 config.toml 文件结构不匹配，或者某个必需的配置段/字段缺失。"
            )
            return None
        except Exception as e:
            logger.error(f"为用途 '{purpose_key}' 创建LLM客户端时发生未知错误: {e}", exc_info=True)
            return None

    try:
        main_consciousness_llm_client = _create_single_processor_client(
            purpose_key="main_consciousness", default_provider_name="gemini"
        )
        if not main_consciousness_llm_client:
            raise RuntimeError("主意识 LLM 客户端初始化失败。请检查日志。")

        intrusive_thoughts_llm_client = _create_single_processor_client(
            purpose_key="intrusive_thoughts", default_provider_name="gemini"
        )
        if not intrusive_thoughts_llm_client:
            raise RuntimeError("侵入性思维 LLM 客户端初始化失败。请检查日志。")

        logger.info("核心LLM客户端 (主意识和侵入性思维) 已成功初始化。")

    except RuntimeError:
        raise
    except Exception as e_init_core:
        logger.critical(f"初始化核心LLM客户端过程中发生未预期的严重错误: {e_init_core}", exc_info=True)
        raise RuntimeError(f"核心LLM客户端初始化因意外错误失败: {e_init_core}") from e_init_core


def _process_db_document_to_state(latest_document: dict[str, Any] | None) -> tuple[dict[str, Any], str | None]:
    """将从数据库获取的最新文档转换为用于Prompt的状态字典。"""
    action_id_whose_result_is_being_shown: str | None = None

    if not latest_document:
        logger.info("数据库为空（或查询失败），使用硬编码的初始状态。")
        state = {
            "mood": INITIAL_STATE["mood"],
            "previous_thinking": "你的上一轮思考是：这是你的第一次思考，请开始吧。",
            "thinking_guidance": (
                f"经过你上一轮的思考，你目前打算的思考方向是：{INITIAL_STATE['thinking_guidance'].split('：', 1)[-1] if '：' in INITIAL_STATE['thinking_guidance'] else (INITIAL_STATE['thinking_guidance'] or '随意发散一下吧。')}"
            ),
            "current_task": INITIAL_STATE["current_task"],
            "action_result_info": INITIAL_STATE["action_result_info"],
            "pending_action_status": INITIAL_STATE["pending_action_status"],
            "recent_adapter_messages": [],  # 初始化为空列表
        }
        return state, None

    mood_db = latest_document.get("emotion_output", INITIAL_STATE["mood"].split("：", 1)[-1])
    prev_think_db = latest_document.get("think_output")
    prev_think_prompt_db = (
        f"你的上一轮思考是：{prev_think_db}"
        if prev_think_db and prev_think_db.strip()
        else "你的上一轮思考是：这是你的第一次思考，请开始吧。"
    )
    guidance_db = latest_document.get(
        "next_think_output",
        INITIAL_STATE["thinking_guidance"].split("：", 1)[-1]
        if "：" in INITIAL_STATE["thinking_guidance"]
        else (INITIAL_STATE["thinking_guidance"] or "随意发散一下吧."),
    )
    current_task_db = latest_document.get("to_do_output", INITIAL_STATE["current_task"])
    if latest_document.get("done_output", False) and current_task_db == latest_document.get("to_do_output"):
        current_task_db = ""

    action_result_info_prompt = INITIAL_STATE["action_result_info"]
    pending_action_status_prompt = INITIAL_STATE["pending_action_status"]
    last_action_attempt = latest_document.get("action_attempted")

    if last_action_attempt and isinstance(last_action_attempt, dict):
        action_status = last_action_attempt.get("status")
        action_description = last_action_attempt.get("action_description", "某个之前的动作")
        action_id = last_action_attempt.get("action_id")
        was_result_seen = last_action_attempt.get("result_seen_by_shuang", False)

        if action_status in ["COMPLETED_SUCCESS", "COMPLETED_FAILURE", "CRITICAL_FAILURE"]:
            if not was_result_seen and action_id:
                final_result = last_action_attempt.get("final_result_for_shuang", "动作已完成，但没有具体结果反馈。")
                action_result_info_prompt = (
                    f"你上一轮行动 '{action_description}' "
                    f"(ID: {action_id[:8] if action_id else 'N/A'}) 的结果是：【{str(final_result)[:500]}】"
                )
                action_id_whose_result_is_being_shown = action_id
            elif was_result_seen:
                action_result_info_prompt = "你上一轮的动作结果已处理。"
        elif action_status and action_status not in ["COMPLETED_SUCCESS", "COMPLETED_FAILURE", "CRITICAL_FAILURE"]:
            action_motivation = last_action_attempt.get("action_motivation", "之前的动机")
            pending_action_status_prompt = (
                f"你之前尝试的动作 '{action_description}' "
                f"(ID: {action_id[:8] if action_id else 'N/A'}) "
                f"(动机: '{action_motivation}') "
                f"目前还在处理中 ({action_status})。"
            )
            action_result_info_prompt = ""

    # 处理来自适配器的消息
    recent_messages_from_adapter: list[dict[str, Any]] = latest_document.get("adapter_messages", [])
    recent_messages_info_prompt = ""
    if recent_messages_from_adapter:
        recent_messages_info_prompt = "最近收到的用户消息或平台请求：\n"
        for msg_entry in recent_messages_from_adapter[-3:]:  # 显示最近3条
            sender_name = msg_entry.get("sender_nickname", "未知用户")
            msg_text = msg_entry.get("text_content", "[非文本内容或解析失败]")
            timestamp_str = msg_entry.get("timestamp", "")
            msg_type_indicator = "[平台请求] " if msg_entry.get("is_platform_request") else ""
            try:
                dt_obj = datetime.datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
                formatted_time = dt_obj.strftime("%H:%M")
            except (ValueError, TypeError):
                formatted_time = "未知时间"

            recent_messages_info_prompt += f"- ({formatted_time}) {msg_type_indicator}{sender_name} 说: {msg_text}\n"
    else:
        recent_messages_info_prompt = "最近没有收到新的用户消息或平台请求。"

    current_state_for_prompt: dict[str, Any] = {
        "mood": f"你现在的心情大概是：{mood_db}",
        "previous_thinking": prev_think_prompt_db,
        "thinking_guidance": f"经过你上一轮的思考，你目前打算的思考方向是：{guidance_db}",
        "current_task": current_task_db,
        "action_result_info": action_result_info_prompt,
        "pending_action_status": pending_action_status_prompt,
        "recent_messages_info": recent_messages_info_prompt,  # 新增
    }
    logger.info("在 _process_db_document_to_state 中：成功处理并返回状态。")
    return current_state_for_prompt, action_id_whose_result_is_being_shown


async def _generate_thought_from_llm(
    llm_client: ProcessorClient,
    current_state_for_prompt: dict[str, Any],
    current_time_str: str,
    root_cfg: AlcarusRootConfig,
    intrusive_thought_str: str = "",
) -> tuple[dict[str, Any] | None, str | None]:
    """使用LLM根据当前状态生成思考。"""
    # (此函数内容与你之前提供的版本基本一致，确保PROMPT_TEMPLATE中的占位符被正确填充)
    task_desc = current_state_for_prompt.get("current_task", "")
    task_info_prompt = f"你当前的目标/任务是：【{task_desc}】" if task_desc else "你当前没有什么特定的目标或任务。"

    persona_cfg = root_cfg.persona

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
        recent_messages_info=current_state_for_prompt["recent_messages_info"],  # 确保填充
        intrusive_thought=intrusive_thought_str,
    )

    logger.debug(
        f"--- 主思维LLM接收到的完整Prompt (模型: {llm_client.llm_client.model_name}) ---\\n{prompt_text}\\n--- Prompt结束 ---"
    )
    logger.debug(f"正在请求 {llm_client.llm_client.provider} API ({llm_client.llm_client.model_name}) 生成主思考...")
    raw_response_text: str = ""
    try:
        response_data = await llm_client.make_llm_request(prompt=prompt_text, is_stream=False)
        if response_data.get("error"):
            error_type = response_data.get("type", "UnknownError")
            error_msg = response_data.get("message", "LLM客户端返回了一个错误")
            logger.error(f"主思维LLM调用失败 ({error_type}): {error_msg}")
            if response_data.get("details"):
                logger.error(f"  错误详情: {str(response_data.get('details'))[:300]}...")
            return None, prompt_text
        raw_response_text = response_data.get("text")
        if not raw_response_text:
            error_msg = "错误：主思维LLM响应中缺少文本内容。"
            if response_data:
                error_msg += f"\n  完整响应: {str(response_data)[:500]}..."
            logger.error(error_msg)
            return None, prompt_text
        json_to_parse = raw_response_text.strip()
        if json_to_parse.startswith("```json"):
            json_to_parse = json_to_parse[7:-3].strip()
        elif json_to_parse.startswith("```"):
            json_to_parse = json_to_parse[3:-3].strip()
        # 尝试移除末尾可能存在的逗号，以提高JSON解析的鲁棒性
        json_to_parse = re.sub(r"[,\s]+(\}|\])$", r"\1", json_to_parse)

        thought_json: dict[str, Any] = json.loads(json_to_parse)
        logger.info("主思维LLM API 响应已成功解析为JSON。")
        if response_data.get("usage"):  # 保存 LLM token 使用情况
            thought_json["_llm_usage_info"] = response_data["usage"]
        return thought_json, prompt_text
    except json.JSONDecodeError as e:
        logger.error(f"错误：解析主思维LLM的JSON响应失败: {e}")
        logger.error(f"未能解析的文本内容: {raw_response_text}")
        return None, prompt_text
    except Exception as e:
        logger.error(f"错误：调用主思维LLM或处理其响应时发生意外错误: {e}", exc_info=True)
        return None, prompt_text


def _update_current_internal_state_after_thought(
    current_state_in_memory: dict[str, Any],
    generated_thought_json: dict[str, Any] | None,
    initiated_action_this_cycle: dict[str, Any] | None,
    action_id_whose_result_was_shown: str | None,
) -> dict[str, Any]:
    """根据生成的思考更新内存中的当前状态。"""
    # (此函数内容与你之前提供的版本基本一致，确保 adapter_messages 被正确清空)
    if not generated_thought_json:
        return current_state_in_memory  # 如果没有生成思考，状态不变

    updated_state = current_state_in_memory.copy()  # 创建副本以修改

    # 更新心情、上一轮思考、下一步思考方向
    updated_state["mood"] = f"你现在的心情大概是：{generated_thought_json.get('emotion', '平静，原因不明')}"
    updated_state["previous_thinking"] = (
        f"你的上一轮思考是：{generated_thought_json.get('think', '未能获取思考内容。')}"
    )
    updated_state["thinking_guidance"] = (
        f"经过你上一轮的思考，你目前打算的思考方向是：{generated_thought_json.get('next_think', '随意发散一下吧。')}"
    )

    # 更新当前任务状态
    llm_todo_text = generated_thought_json.get("to_do", "").strip()
    llm_done_flag = generated_thought_json.get("done", False)
    current_task_in_state = updated_state.get("current_task", "")

    if llm_done_flag and current_task_in_state and current_task_in_state == generated_thought_json.get("to_do"):
        logger.info(f"AI标记任务 '{current_task_in_state}' 为已完成。将从内存状态中清除。")
        updated_state["current_task"] = ""  # 清除已完成的任务
    elif llm_todo_text and llm_todo_text != current_task_in_state:
        logger.info(f"AI将任务从 '{current_task_in_state or '无'}' 更新/设定为 '{llm_todo_text}'。")
        updated_state["current_task"] = llm_todo_text  # 更新或设定新任务

    # 清理 recent_adapter_messages，因为它们已经被用于本轮思考的上下文中了
    updated_state["recent_adapter_messages"] = []

    # 更新行动相关的状态提示
    if initiated_action_this_cycle:  # 如果本轮产生了新的行动
        action_desc_new = initiated_action_this_cycle.get("action_description", "某个新动作")
        action_id_new = initiated_action_this_cycle.get("action_id", "")[:8]  # 取ID前8位作显示
        updated_state["pending_action_status"] = (
            f"你刚刚决定尝试动作 '{action_desc_new}' (ID: {action_id_new})，目前正在处理中..."
        )
        updated_state["action_result_info"] = ""  # 清除上一轮的动作结果信息
    elif action_id_whose_result_was_shown:  # 如果上一轮的动作结果在本轮被展示了
        updated_state["pending_action_status"] = ""  # 没有待处理的动作
        updated_state["action_result_info"] = "你上一轮的动作结果已处理。"  # 标记结果已处理
    else:  # 既没有新动作，也没有旧动作结果被展示
        updated_state["pending_action_status"] = INITIAL_STATE["pending_action_status"]  # 恢复到初始的待处理状态
        updated_state["action_result_info"] = INITIAL_STATE["action_result_info"]  # 恢复到初始的动作结果信息

    return updated_state


# --- 处理从适配器收到的消息的回调函数 ---
async def handle_incoming_adapter_message(
    message: MessageBase,
    websocket: WebSocketServerProtocol,  # 收到消息的 WebSocket 连接，用于可能的直接回复
) -> None:
    """处理从适配器收到的 AIcarusMessageBase 消息。"""
    global db_instance_for_actions, main_thoughts_collection_name_for_actions, core_comm_layer

    logger.info(
        f"核心逻辑收到来自适配器 ({websocket.remote_address}) 的消息，类型: {message.message_info.interaction_purpose}"
    )
    logger.debug(f"完整收到的 AicarusMessageBase: {message.to_dict()}")

    # 确保数据库实例已初始化
    if not db_instance_for_actions or not main_thoughts_collection_name_for_actions:
        logger.error("数据库未初始化，无法存储收到的适配器消息。")
        return

    # 根据消息意图进行处理
    if message.message_info.interaction_purpose == "user_message":
        user_info = message.message_info.user_info
        group_info = message.message_info.group_info

        sender_id = user_info.user_id if user_info else "unknown_user"
        sender_nickname = user_info.user_nickname if user_info else "未知用户"
        target_group_id = group_info.group_id if group_info else None  # 可能是私聊

        # 简单提取文本内容作为示例
        text_content_parts: list[str] = []
        if (
            message.message_segment
            and message.message_segment.type == "seglist"
            and isinstance(message.message_segment.data, list)
        ):
            for seg_obj in message.message_segment.data:
                # 确保 seg_obj 是 AicarusSeg 实例或可以转换的字典
                seg = seg_obj if isinstance(seg_obj, Seg) else Seg.from_dict(seg_obj)
                if seg.type == "text" and isinstance(seg.data, str):
                    text_content_parts.append(seg.data)
                elif seg.type == "image":
                    text_content_parts.append("[图片]")  # 简单表示
                # 可以根据需要扩展对其他 Seg 类型的处理

        text_content = "".join(text_content_parts) if text_content_parts else "[消息内容无法解析或非文本]"

        message_entry_for_db = {
            "adapter_message_id": message.message_info.message_id,  # 平台消息ID
            "timestamp": datetime.datetime.fromtimestamp(message.message_info.time / 1000.0).isoformat()
            + "Z",  # 转换为ISO格式时间戳
            "sender_id": sender_id,
            "sender_nickname": sender_nickname,
            "group_id": target_group_id,  # 如果是群消息
            "text_content": text_content,  # 提取或转换后的文本内容
            "raw_aicarus_message": message.to_dict(),  # 存储完整的原始AIcarus消息
        }

    elif message.message_info.interaction_purpose == "platform_notification":
        logger.info(f"收到平台通知: {message.message_segment.to_dict() if message.message_segment else '无内容段'}")
        # 简单记录，或根据具体通知类型触发更复杂的逻辑 (例如，更新群成员列表)
        # 也可以考虑将其存入 adapter_messages 供LLM感知
        # ... (此处可添加逻辑) ...

    elif message.message_info.interaction_purpose == "platform_request":
        logger.info(f"收到平台请求: {message.message_segment.to_dict() if message.message_segment else '无内容段'}")
        # 这类请求（如好友请求、加群请求）通常需要Core做出响应
        # 将请求信息存入 adapter_messages，以便LLM在下一轮思考时看到并决定如何处理
        request_seg = (
            message.message_segment.data[0]
            if message.message_segment.data and isinstance(message.message_segment.data, list)
            else None
        )
        if request_seg:  # 确保 request_seg 是 Seg 或可转换的字典
            seg_to_log = request_seg if isinstance(request_seg, Seg) else Seg.from_dict(request_seg)
            request_summary = f"平台请求: 类型={seg_to_log.type}, 数据={str(seg_to_log.data)[:100]}..."

            message_entry_for_db = {
                "adapter_message_id": message.message_info.message_id,
                "timestamp": datetime.datetime.fromtimestamp(message.message_info.time / 1000.0).isoformat() + "Z",
                "sender_id": message.message_info.user_info.user_id
                if message.message_info.user_info
                else "unknown_requester",
                "text_content": request_summary,  # 用请求摘要作为文本内容
                "is_platform_request": True,  # 特殊标记
                "raw_aicarus_message": message.to_dict(),
            }
            logger.info(f"平台请求 (来自: {message_entry_for_db['sender_id']}) 已记录，将在下一轮思考中处理。")
        else:
            logger.warning("收到的平台请求消息段为空或格式不正确。")

    elif message.message_info.interaction_purpose == "action_response":
        # 处理来自适配器的对先前Core动作的执行结果反馈
        logger.info(
            f"收到来自适配器的动作响应: {message.message_segment.to_dict() if message.message_segment else '无内容段'}"
        )
        original_action_id = message.message_info.message_id  # 假设响应的 message_id 是原动作的 message_id

        if original_action_id and db_instance_for_actions and main_thoughts_collection_name_for_actions:
            response_seg_obj = (
                message.message_segment.data[0]
                if message.message_segment.data and isinstance(message.message_segment.data, list)
                else None
            )
            if response_seg_obj:
                response_seg = (
                    response_seg_obj if isinstance(response_seg_obj, Seg) else Seg.from_dict(response_seg_obj)
                )

                status_update_for_db: dict[str, Any] = {
                    "adapter_response_received_at": datetime.datetime.now(datetime.UTC).isoformat(),
                    "adapter_response_type": response_seg.type,
                }
                if response_seg.type == "action_result:success":
                    status_update_for_db["status_after_adapter_response"] = "ADAPTER_SUCCESS"  # 自定义状态
                    if isinstance(response_seg.data, dict) and response_seg.data.get("details"):
                        status_update_for_db["adapter_response_details"] = response_seg.data.get("details")
                elif response_seg.type == "action_result:failure":
                    status_update_for_db["status_after_adapter_response"] = "ADAPTER_FAILURE"  # 自定义状态
                    if isinstance(response_seg.data, dict):
                        status_update_for_db["adapter_error_message"] = response_seg.data.get("error_message")
                        status_update_for_db["adapter_error_code"] = response_seg.data.get("error_code")

                # 更新数据库中对应 action_id 的动作状态
                update_success = await arangodb_handler.update_action_status_by_action_id(
                    db_instance_for_actions,
                    main_thoughts_collection_name_for_actions,
                    original_action_id,
                    status_update_for_db,
                )
                if update_success:
                    logger.info(f"动作 ID {original_action_id} 的状态已根据适配器响应更新。")
                else:
                    logger.warning(f"更新动作 ID {original_action_id} 的状态失败 (可能未找到匹配文档)。")
            else:
                logger.warning(f"收到的动作响应消息段为空或格式不正确 (原动作ID: {original_action_id})。")
        else:
            logger.warning("收到的动作响应缺少原始动作ID，无法更新数据库。")

    else:
        logger.warning(f"收到未处理的 interaction_purpose: {message.message_info.interaction_purpose}")


async def _core_thinking_loop(
    root_cfg: AlcarusRootConfig, arango_db_instance: StandardDatabase, main_thoughts_collection: StandardCollection
) -> None:
    """核心思考循环。"""
    global \
        current_internal_state, \
        action_id_whose_result_was_loaded, \
        core_comm_layer, \
        db_instance_for_actions, \
        main_thoughts_collection_name_for_actions

    # 初始化全局数据库实例，供其他模块（如 action_handler）使用
    db_instance_for_actions = arango_db_instance
    main_thoughts_collection_name_for_actions = main_thoughts_collection.name

    latest_doc_from_db = await arangodb_handler.get_latest_thought_document_raw(
        arango_db_instance, main_thoughts_collection.name
    )
    # _process_db_document_to_state 现在也处理 adapter_messages
    current_internal_state, action_id_whose_result_was_loaded = _process_db_document_to_state(latest_doc_from_db)

    core_logic_cfg: CoreLogicSettings = root_cfg.core_logic_settings
    time_format_str: str = "%Y年%m月%d日 %H点%M分%S秒"  # 时间格式字符串
    thinking_interval_sec: int = core_logic_cfg.thinking_interval_seconds

    logger.info("\n--- 霜的意识开始流动 (使用 ArangoDB 和 WebSocket) ---")
    loop_count: int = 0

    while not stop_intrusive_thread.is_set():  # 使用全局停止事件
        loop_count += 1
        current_time_formatted_str = datetime.datetime.now().strftime(time_format_str)
        background_action_tasks: set[asyncio.Task] = set()  # 用于异步执行动作

        task_desc_for_prompt = current_internal_state.get("current_task", "")
        current_internal_state["current_task_info_for_prompt"] = (  # 为Prompt准备的任务信息
            f"你当前的目标/任务是：【{task_desc_for_prompt}】"
            if task_desc_for_prompt
            else "你当前没有什么特定的目标或任务。"
        )

        intrusive_thought_to_inject_this_cycle: str = ""  # 本轮要注入的侵入性思维
        intrusive_module_settings_obj: IntrusiveThoughtsSettings = root_cfg.intrusive_thoughts_module_settings
        # 确保侵入性思维集合实例有效
        intrusive_thoughts_collection_instance = await arangodb_handler.ensure_collection_exists(
            arango_db_instance, ARANGODB_COLLECTION_CONFIG["intrusive_thoughts_collection_name"]
        )
        if (
            intrusive_module_settings_obj.enabled
            and intrusive_thoughts_collection_instance is not None
            and random.random() < intrusive_module_settings_obj.insertion_probability
        ):
            random_thought_doc = await arangodb_handler.get_random_intrusive_thought(
                arango_db_instance, intrusive_thoughts_collection_instance.name
            )
            if random_thought_doc and "text" in random_thought_doc:
                intrusive_thought_to_inject_this_cycle = f"你突然有一个神奇的念头：{random_thought_doc['text']}"

        logger.debug(f"\\n[{datetime.datetime.now().strftime('%H:%M:%S')} - 轮次 {loop_count}] 霜正在思考...")
        if intrusive_thought_to_inject_this_cycle:
            logger.debug(f"  注入侵入性思维: {intrusive_thought_to_inject_this_cycle[:60]}...")

        if main_consciousness_llm_client is None:  # 检查主意识LLM客户端是否已初始化
            logger.error("主意识LLM客户端未初始化，无法生成思考。跳过本轮。")
            await asyncio.sleep(thinking_interval_sec)
            continue

        # 调用LLM生成思考
        generated_thought_json, full_prompt_text_sent = await _generate_thought_from_llm(
            llm_client=main_consciousness_llm_client,
            current_state_for_prompt=current_internal_state,
            current_time_str=current_time_formatted_str,
            root_cfg=root_cfg,  # 传递根配置
            intrusive_thought_str=intrusive_thought_to_inject_this_cycle,
        )

        initiated_action_data_for_db: dict[str, Any] | None = None  # 本轮产生的行动数据（用于存DB）
        action_info_for_task: dict[str, Any] | None = None  # 本轮产生的行动信息（用于创建任务）
        saved_doc_key: str | None = None  # 保存到DB后获取的文档key

        if generated_thought_json:  # 如果成功生成了思考
            logger.debug(
                f"  主思维LLM输出的完整JSON:\\n{json.dumps(generated_thought_json, indent=2, ensure_ascii=False)}"
            )
            action_desc_raw = generated_thought_json.get("action_to_take")
            action_desc_from_llm = action_desc_raw.strip() if isinstance(action_desc_raw, str) else ""
            action_motive_raw = generated_thought_json.get("action_motivation")
            action_motive_from_llm = action_motive_raw.strip() if isinstance(action_motive_raw, str) else ""

            if action_desc_from_llm:  # 如果LLM决定要采取行动
                action_id_this_cycle = str(uuid.uuid4())  # 为此行动生成唯一ID
                initiated_action_data_for_db = {
                    "action_description": action_desc_from_llm,
                    "action_motivation": action_motive_from_llm,
                    "action_id": action_id_this_cycle,
                    "status": "PENDING",  # 初始状态为待处理
                    "result_seen_by_shuang": False,  # 结果尚未被霜（LLM）看到
                    "initiated_at": datetime.datetime.now(datetime.UTC).isoformat(),  # 记录行动发起时间
                }
                action_info_for_task = {  # 用于传递给行动处理流程的信息
                    "action_id": action_id_this_cycle,
                    "action_description": action_desc_from_llm,
                    "action_motivation": action_motive_from_llm,
                    "current_thought_context": generated_thought_json.get("think", "无特定思考上下文。"),
                }
                logger.debug(f"  >>> 行动意图产生: '{action_desc_from_llm}' (ID: {action_id_this_cycle[:8]})")

            # 构建要保存到主思考集合的文档
            document_to_save_in_main: dict[str, Any] = {
                "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
                "time_injected_to_prompt": current_time_formatted_str,
                "intrusive_thought_injected": intrusive_thought_to_inject_this_cycle,
                "mood_input": current_internal_state["mood"],
                "previous_thinking_input": current_internal_state["previous_thinking"],
                "thinking_guidance_input": current_internal_state["thinking_guidance"],
                "task_input_info": current_internal_state.get("current_task_info_for_prompt", "无特定任务输入"),
                "action_result_input": current_internal_state.get("action_result_info", ""),
                "pending_action_status_input": current_internal_state.get("pending_action_status", ""),
                "recent_adapter_messages_input": current_internal_state.get(
                    "recent_messages_info", ""
                ),  # 保存注入的adapter消息上下文
                "full_prompt_sent": full_prompt_text_sent if full_prompt_text_sent else "Prompt未能构建",
                "think_output": generated_thought_json.get("think"),
                "emotion_output": generated_thought_json.get("emotion"),
                "next_think_output": generated_thought_json.get("next_think"),
                "to_do_output": generated_thought_json.get("to_do", ""),
                "done_output": generated_thought_json.get("done", False),
                "action_to_take_output": generated_thought_json.get("action_to_take", ""),
                "action_motivation_output": generated_thought_json.get("action_motivation", ""),
                "action_attempted": initiated_action_data_for_db,  # 保存行动的初始数据
                "adapter_messages": [],  # 保存后清空，下一轮从DB加载时是空的 (表示这些消息已被处理)
            }
            if "_llm_usage_info" in generated_thought_json:  # 如果LLM返回了token使用信息
                document_to_save_in_main["_llm_usage_info"] = generated_thought_json["_llm_usage_info"]

            # 保存思考文档到数据库
            saved_doc_key = await arangodb_handler.save_thought_document(
                main_thoughts_collection, document_to_save_in_main
            )
            # 如果上一轮有动作结果被加载并展示给LLM，现在标记它为“已阅”
            if action_id_whose_result_was_loaded:
                await arangodb_handler.mark_action_result_as_seen(
                    arango_db_instance,
                    main_thoughts_collection.name,
                    action_id_whose_result_was_loaded,
                )

            # 如果产生了行动并且文档已成功保存，则异步启动行动处理流程
            if action_info_for_task and saved_doc_key:
                action_task = asyncio.create_task(
                    process_action_flow(
                        action_id=action_info_for_task["action_id"],
                        doc_key_for_updates=saved_doc_key,  # DB文档的key，用于后续更新状态
                        action_description=action_info_for_task["action_description"],
                        action_motivation=action_info_for_task["action_motivation"],
                        current_thought_context=action_info_for_task["current_thought_context"],
                        arango_db_for_updates=arango_db_instance,  # 传递DB实例
                        collection_name_for_updates=main_thoughts_collection.name,  # 传递集合名称
                        comm_layer_for_actions=core_comm_layer,  # 传递通信层实例
                    )
                )
                background_action_tasks.add(action_task)  # 将任务添加到集合中以便追踪
                action_task.add_done_callback(background_action_tasks.discard)  # 任务完成后从集合中移除
                logger.debug(
                    f"      动作 '{action_info_for_task['action_description']}' (ID: {action_info_for_task['action_id'][:8]}, DocKey: {saved_doc_key}) 已异步启动处理。"
                )
            elif action_info_for_task and not saved_doc_key:  # 如果有行动但保存文档失败
                logger.error(
                    f"未能获取保存文档的 _key，无法为动作 ID {action_info_for_task['action_id']} 创建处理任务。"
                )

            # 更新内存中的当前状态，为下一轮思考做准备
            current_internal_state = _update_current_internal_state_after_thought(
                current_internal_state,
                generated_thought_json,
                initiated_action_data_for_db,  # 本轮发起的行动
                action_id_whose_result_was_loaded,  # 上一轮已处理结果的行动ID
            )
        else:  # 如果LLM未能生成思考
            logger.warning("  本轮思考生成失败或无内容。")

        logger.debug(f"  等待 {thinking_interval_sec} 秒...")
        try:
            # 等待一段时间或直到停止事件被设置
            await asyncio.wait_for(
                asyncio.to_thread(stop_intrusive_thread.wait),
                timeout=float(thinking_interval_sec),
            )
            if stop_intrusive_thread.is_set():  # 如果是停止事件导致等待结束
                logger.info("主循环等待被停止事件中断。")
                break
        except TimeoutError:  # 如果是正常超时
            logger.debug(f"等待 {thinking_interval_sec} 秒超时，事件未被设置。继续循环。")
            pass  # 继续下一轮循环
        except asyncio.CancelledError:  # 如果任务被取消
            logger.info("主循环的 sleep (asyncio.wait_for) 被取消，准备退出。")
            stop_intrusive_thread.set()  # 确保停止事件被设置
            break

        if stop_intrusive_thread.is_set():  # 再次检查停止事件
            logger.info("主循环在等待间隔后检测到停止事件，准备退出。")
            break

        # 为下一轮循环加载最新的数据库状态
        logger.debug("主循环：即将调用 arangodb_handler.get_latest_thought_document_raw 来获取 ArangoDB 状态...")
        latest_doc_from_db = await arangodb_handler.get_latest_thought_document_raw(
            arango_db_instance, main_thoughts_collection.name
        )
        current_internal_state, action_id_whose_result_was_loaded = _process_db_document_to_state(latest_doc_from_db)
        logger.debug("主循环：数据库状态获取与处理完成。")


async def start_consciousness_flow() -> None:
    """启动意识流主程序，包括初始化、启动后台任务和主思考循环。"""
    global stop_intrusive_thread, core_comm_layer, db_instance_for_actions, main_thoughts_collection_name_for_actions

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
    except Exception as e_action_init:
        logger.warning(f"警告：行动模块LLM客户端初始化失败，行动相关功能可能无法使用: {e_action_init}", exc_info=True)

    # 初始化数据库连接
    arango_client_instance: ArangoClient | None = None
    arango_db_instance: StandardDatabase | None = None
    main_thoughts_collection_instance: StandardCollection | None = None
    intrusive_thoughts_collection_instance: StandardCollection | None = None

    try:
        db_connection_settings: DatabaseSettings = root_cfg.database
        arango_client_instance, arango_db_instance = await arangodb_handler.connect_to_arangodb(db_connection_settings)

        # 设置全局数据库实例供其他模块使用
        db_instance_for_actions = arango_db_instance

        main_thoughts_coll_name = ARANGODB_COLLECTION_CONFIG["main_thoughts_collection_name"]
        main_thoughts_collection_instance = await arangodb_handler.ensure_collection_exists(
            arango_db_instance, main_thoughts_coll_name
        )
        main_thoughts_collection_name_for_actions = main_thoughts_coll_name

        intrusive_thoughts_coll_name = ARANGODB_COLLECTION_CONFIG["intrusive_thoughts_collection_name"]
        intrusive_thoughts_collection_instance = await arangodb_handler.ensure_collection_exists(
            arango_db_instance, intrusive_thoughts_coll_name
        )

    except (ValueError, RuntimeError) as e_db_connect:
        logger.critical(f"严重：无法连接到 ArangoDB 或确保集合存在，程序无法继续: {e_db_connect}", exc_info=True)
        return

    # 初始化并启动 WebSocket 服务器
    # 从环境变量或配置文件获取 WebSocket 服务器的 host 和 port
    ws_host = os.getenv("CORE_WS_HOST", "127.0.0.1")  # 示例：从环境变量获取，默认为 "127.0.0.1"
    ws_port_str = os.getenv("CORE_WS_PORT", "8077")  # 示例：从环境变量获取，默认为 "8077"
    try:
        ws_port = int(ws_port_str)
    except ValueError:
        logger.critical(f"无效的 CORE_WS_PORT: '{ws_port_str}'。必须是一个整数。")
        return

    # 创建 WebSocket 服务器实例，并传入消息处理回调和数据库实例
    core_comm_layer = CoreWebsocketServer(ws_host, ws_port, handle_incoming_adapter_message, arango_db_instance)
    # 将通信层实例传递给 action_handler 模块
    set_core_communication_layer_for_actions(core_comm_layer)

    server_task = asyncio.create_task(core_comm_layer.start())  # 异步启动服务器

    # 启动侵入性思维生成线程 (如果启用)
    intrusive_module_settings_obj: IntrusiveThoughtsSettings = root_cfg.intrusive_thoughts_module_settings
    intrusive_thread: threading.Thread | None = None
    if intrusive_module_settings_obj.enabled:
        if intrusive_thoughts_llm_client is None:  # 检查LLM客户端是否已初始化
            logger.error("侵入性思维模块已启用，但其LLM客户端未能初始化。模块将不会启动。")
        elif arango_db_instance is None or intrusive_thoughts_collection_instance is None:  # 检查DB和集合是否已初始化
            logger.error("侵入性思维模块已启用，但 ArangoDB 未连接或集合未初始化。模块将不会启动。")
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
                        intrusive_thoughts_llm_client,  # LLM客户端
                        arango_db_instance,  # DB实例
                        intrusive_thoughts_collection_instance.name,  # 集合名称
                        intrusive_settings_dict,  # 模块设置
                        stop_intrusive_thread,  # 停止事件
                        persona_configuration_for_intrusive,  # 人格配置
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
        logger.critical("严重错误：主 ArangoDB 数据库或集合未能初始化，无法开始意识流。")
        if core_comm_layer:
            await core_comm_layer.stop()
        if server_task and not server_task.done():
            server_task.cancel()
        return

    # 启动核心思考循环
    thinking_loop_task = asyncio.create_task(
        _core_thinking_loop(root_cfg, arango_db_instance, main_thoughts_collection_instance)
    )

    try:
        # 等待服务器任务或思考循环任务中任何一个完成（或出错）
        done, pending = await asyncio.wait([server_task, thinking_loop_task], return_when=asyncio.FIRST_COMPLETED)

        # 如果一个任务完成了（或出错了），取消另一个挂起的任务
        for task in pending:
            logger.info(f"一个关键任务已结束，正在取消挂起的任务: {task.get_name()}")
            task.cancel()

        # 检查已完成任务中是否有异常
        for task in done:
            if task.exception():
                logger.critical(
                    f"一个关键任务 ({task.get_name()}) 因异常而结束: {task.exception()}", exc_info=task.exception()
                )

    except KeyboardInterrupt:
        logger.info("\n--- 霜的意识流动被用户手动中断 (KeyboardInterrupt) ---")
    except asyncio.CancelledError:  # 如果 start_consciousness_flow 本身被取消
        logger.info("\n--- 霜的意识流动主任务 (start_consciousness_flow) 被取消 ---")
    except Exception as e_main_flow:
        logger.critical(f"\n--- 意识流动主流程发生意外错误: {e_main_flow} ---", exc_info=True)
    finally:
        logger.info("--- 开始程序清理 (WebSocket Server, ArangoDB, Threads) ---")
        stop_intrusive_thread.set()  # 确保所有使用此事件的循环和线程收到停止信号

        # 优雅停止 WebSocket 服务器
        if core_comm_layer:
            logger.info("正在停止核心 WebSocket 通信层...")
            await core_comm_layer.stop()
        # 如果服务器任务仍在运行（理论上应该在 core_comm_layer.stop() 后结束，或因 _stop_event 结束）
        if server_task and not server_task.done():
            logger.info("正在取消 WebSocket 服务器任务...")
            server_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await server_task  # 等待取消完成

        # 如果思考循环任务仍在运行
        if thinking_loop_task and not thinking_loop_task.done():
            logger.info("正在取消核心思考循环任务...")
            thinking_loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await thinking_loop_task  # 等待取消完成

        # 等待侵入性思维线程结束
        if intrusive_thread is not None and intrusive_thread.is_alive():
            logger.info("等待侵入性思维线程结束...")
            intrusive_thread.join(timeout=5)  # 等待最多5秒
            if intrusive_thread.is_alive():
                logger.warning("警告：侵入性思维线程在超时后仍未结束。")
            else:
                logger.info("侵入性思维线程已成功结束。")

        # ArangoDB 客户端通常不需要显式关闭，连接池会管理连接
        if arango_client_instance is not None:  # arango_client_instance 是 ArangoClient 类型
            logger.info("ArangoDB 客户端通常由其内部连接池管理，无需显式关闭实例。")

        logger.info("程序清理完成。霜的意识已停止流动。")
