import asyncio
import contextlib
import datetime
import json # 确保导入
import logging
import os # 确保导入
import random
import re
import threading
import uuid
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse # 确保导入

from arango import ArangoClient
from arango.collection import StandardCollection
from arango.database import StandardDatabase

from src.action.action_handler import initialize_llm_clients_for_action_module, process_action_flow
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
from src.database import arangodb_handler
from src.llmrequest.llm_processor import Client as ProcessorClient
from . import intrusive_thoughts

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

INITIAL_STATE: dict[str, Any] = {
    "mood": "你现在的心情大概是：平静。",
    "previous_thinking": " ",
    "thinking_guidance": " ",
    "current_task": "",
    "action_result_info": "你上一轮没有执行产生结果的特定行动。",
    "pending_action_status": "",
}

ARANGODB_COLLECTION_CONFIG: dict[str, str] = {
    "main_thoughts_collection_name": "thoughts_collection",
    "intrusive_thoughts_collection_name": "intrusive_thoughts_pool",
}

PROMPT_TEMPLATE: str = """当前时间：{current_time}
你是{bot_name}；
{persona_description}

{persona_profile}

{current_task_info}

{action_result_info}
{pending_action_status}

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

main_consciousness_llm_client: ProcessorClient | None = None
intrusive_thoughts_llm_client: ProcessorClient | None = None
# intrusive_thoughts_pool_collection 现在由 intrusive_thoughts 模块内部管理或通过参数传递
stop_intrusive_thread: threading.Event = threading.Event()


# --- 数据库连接和集合管理函数已移至 arangodb_handler.py ---
# _connect_to_arangodb
# _ensure_collection_exists


def _initialize_core_llm_clients(root_cfg: AlcarusRootConfig) -> None:

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
    env_val_abandoned = os.getenv("LLM_ABANDONED_KEYS") # 固定名称
    if env_val_abandoned:
        try:
            keys_from_env = json.loads(env_val_abandoned)
            if isinstance(keys_from_env, list):
                resolved_abandoned_keys = [str(k).strip() for k in keys_from_env if str(k).strip()]
        except json.JSONDecodeError:
            logger.warning(f"环境变量 'LLM_ABANDONED_KEYS' 的值不是有效的JSON列表，将尝试按逗号分隔。值: {env_val_abandoned[:50]}...")
            resolved_abandoned_keys = [k.strip() for k in env_val_abandoned.split(",") if k.strip()]
        if not resolved_abandoned_keys and env_val_abandoned.strip():
             resolved_abandoned_keys = [env_val_abandoned.strip()]


    def _create_single_processor_client(
        purpose_key: str, # 例如 "main_consciousness" 或 "intrusive_thoughts"
        default_provider_name: str, # 例如 "gemini"
    ) -> ProcessorClient | None:
        try:
            # 1. 获取 ModelParams 对象
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
            
            # 2. 准备构造函数参数
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
                "proxy_host": final_proxy_host, # 使用外层函数准备好的代理信息
                "proxy_port": final_proxy_port, # 使用外层函数准备好的代理信息
                "abandoned_keys_config": resolved_abandoned_keys, # 使用外层函数准备好的废弃密钥列表
                **model_specific_kwargs,
            }
            
            final_constructor_args = {k: v for k, v in processor_constructor_args.items() if v is not None}
            client_instance = ProcessorClient(**final_constructor_args) # type: ignore
            
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
        # 假设这些客户端默认使用 "gemini" 提供商
        main_consciousness_llm_client = _create_single_processor_client(
            purpose_key="main_consciousness",
            default_provider_name="gemini"
        )
        if not main_consciousness_llm_client:
            raise RuntimeError("主意识 LLM 客户端初始化失败。")
            
        intrusive_thoughts_llm_client = _create_single_processor_client(
            purpose_key="intrusive_thoughts",
            default_provider_name="gemini"
        )
        if not intrusive_thoughts_llm_client:
            raise RuntimeError("侵入性思维 LLM 客户端初始化失败。")
            
        logger.info("核心LLM客户端 (主意识和侵入性思维) 已成功初始化。")
        
    except Exception as e_init_core:
        logger.critical(f"初始化核心LLM客户端过程中发生严重错误: {e_init_core}", exc_info=True)
        raise RuntimeError(f"核心LLM客户端初始化失败: {e_init_core}") from e_init_core

    try:
        main_consciousness_llm_client = _create_single_processor_client("main_llm_settings")
        if not main_consciousness_llm_client:
            raise RuntimeError("主意识 LLM 客户端初始化失败。")
        intrusive_thoughts_llm_client = _create_single_processor_client("intrusive_llm_settings")
        if not intrusive_thoughts_llm_client:
            raise RuntimeError("侵入性思维 LLM 客户端初始化失败。")
        logger.info("核心LLM客户端 (主意识和侵入性思维) 已成功初始化。")
    except Exception as e_init_core:
        logger.critical(f"初始化核心LLM客户端过程中发生严重错误: {e_init_core}", exc_info=True)
        raise RuntimeError(f"核心LLM客户端初始化失败: {e_init_core}") from e_init_core


def _process_db_document_to_state(latest_document: dict[str, Any] | None) -> tuple[dict[str, Any], str | None]:
    """
    将从数据库获取的原始文档处理成用于Prompt的状态字典。
    返回 (状态字典, action_id_whose_result_is_being_shown)。
    """
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

    if last_action_attempt and isinstance(last_action_attempt, dict):  # 确保是字典
        action_status = last_action_attempt.get("status")
        action_description = last_action_attempt.get("action_description", "某个之前的动作")
        action_id = last_action_attempt.get("action_id")
        was_result_seen = last_action_attempt.get("result_seen_by_shuang", False)

        if action_status in ["COMPLETED_SUCCESS", "COMPLETED_FAILURE"]:
            if not was_result_seen and action_id:
                final_result = last_action_attempt.get("final_result_for_shuang", "动作已完成，但没有具体结果反馈。")
                action_result_info_prompt = (
                    f"你上一轮行动 '{action_description}' "
                    f"(ID: {action_id[:8] if action_id else 'N/A'}) 的结果是：【{str(final_result)[:500]}】"
                )
                action_id_whose_result_is_being_shown = action_id
            elif was_result_seen:
                action_result_info_prompt = "你上一轮的动作结果已处理。"
        elif action_status and action_status not in ["COMPLETED_SUCCESS", "COMPLETED_FAILURE"]:
            action_motivation = last_action_attempt.get("action_motivation", "之前的动机")
            pending_action_status_prompt = (
                f"你之前尝试的动作 '{action_description}' "
                f"(ID: {action_id[:8] if action_id else 'N/A'}) "
                f"(动机: '{action_motivation}') "
                f"目前还在处理中 ({action_status})。"
            )
            action_result_info_prompt = ""

    current_state_for_prompt: dict[str, Any] = {
        "mood": f"你现在的心情大概是：{mood_db}",
        "previous_thinking": prev_think_prompt_db,
        "thinking_guidance": f"经过你上一轮的思考，你目前打算的思考方向是：{guidance_db}",
        "current_task": current_task_db,
        "action_result_info": action_result_info_prompt,
        "pending_action_status": pending_action_status_prompt,
    }
    logger.info("在 _process_db_document_to_state 中：成功处理并返回状态。")
    return current_state_for_prompt, action_id_whose_result_is_being_shown


async def _generate_thought_from_llm(
    llm_client: ProcessorClient,
    current_state_for_prompt: dict[str, Any],
    current_time_str: str,
    root_cfg: AlcarusRootConfig,  # <--- 新增参数：传递加载好的根配置对象
    intrusive_thought_str: str = "",
) -> tuple[dict[str, Any] | None, str | None]:
    # (此函数逻辑不变)
    task_desc = current_state_for_prompt.get("current_task", "")
    task_info_prompt = f"你当前的目标/任务是：【{task_desc}】" if task_desc else "你当前没有什么特定的目标或任务。"

    persona_cfg = root_cfg.persona

    prompt_text = PROMPT_TEMPLATE.format(
        current_time=current_time_str,
        # --- 新增/修改的填充项 ---
        bot_name=persona_cfg.bot_name,
        persona_description=persona_cfg.description,
        persona_profile=persona_cfg.profile,
        # --- 现有填充项保持不变 ---
        current_task_info=task_info_prompt,
        mood=current_state_for_prompt["mood"],
        previous_thinking=current_state_for_prompt["previous_thinking"],
        thinking_guidance=current_state_for_prompt["thinking_guidance"],
        action_result_info=current_state_for_prompt["action_result_info"],
        pending_action_status=current_state_for_prompt["pending_action_status"],
        intrusive_thought=intrusive_thought_str,
    )

    logger.info(
        f"--- 主思维LLM接收到的完整Prompt (模型: {llm_client.llm_client.model_name}) ---\n{prompt_text}\n--- Prompt结束 ---"
    )
    logger.info(f"正在请求 {llm_client.llm_client.provider} API ({llm_client.llm_client.model_name}) 生成主思考...")
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
        json_to_parse = re.sub(r",\s*([\}\]])", r"\1", json_to_parse)
        thought_json: dict[str, Any] = json.loads(json_to_parse)
        logger.info("主思维LLM API 响应已成功解析为JSON。")
        if response_data.get("usage"):
            thought_json["_llm_usage_info"] = response_data["usage"]
        return thought_json, prompt_text
    except json.JSONDecodeError as e:
        logger.error(f"错误：解析主思维LLM的JSON响应失败: {e}")
        logger.error(f"未能解析的文本内容: {raw_response_text}")
        return None, prompt_text
    except Exception as e:
        logger.error(f"错误：调用主思维LLM或处理其响应时发生意外错误: {e}", exc_info=True)
        return None, prompt_text


# --- _save_thought_to_db_async 和 _mark_action_result_as_seen_in_db 已移至 arangodb_handler.py ---


def _update_current_internal_state_after_thought(
    current_state_in_memory: dict[str, Any],
    generated_thought_json: dict[str, Any] | None,
    initiated_action_this_cycle: dict[str, Any] | None,
    action_id_whose_result_was_shown: str | None,
) -> dict[str, Any]:
    # (此函数逻辑不变)
    if not generated_thought_json:
        return current_state_in_memory
    updated_state = current_state_in_memory.copy()
    updated_state["mood"] = f"你现在的心情大概是：{generated_thought_json.get('emotion', '平静，原因不明')}"
    updated_state["previous_thinking"] = (
        f"你的上一轮思考是：{generated_thought_json.get('think', '未能获取思考内容。')}"
    )
    updated_state["thinking_guidance"] = (
        f"经过你上一轮的思考，你目前打算的思考方向是：{generated_thought_json.get('next_think', '随意发散一下吧。')}"
    )
    llm_todo_text = generated_thought_json.get("to_do", "").strip()
    llm_done_flag = generated_thought_json.get("done", False)
    current_task_in_state = updated_state.get("current_task", "")
    if llm_done_flag and current_task_in_state and current_task_in_state == generated_thought_json.get("to_do"):
        logger.info(f"AI标记任务 '{current_task_in_state}' 为已完成。将从内存状态中清除。")
        updated_state["current_task"] = ""
    elif llm_todo_text and llm_todo_text != current_task_in_state:
        logger.info(f"AI将任务从 '{current_task_in_state or '无'}' 更新/设定为 '{llm_todo_text}'。")
        updated_state["current_task"] = llm_todo_text
    if initiated_action_this_cycle:
        action_desc_new = initiated_action_this_cycle.get("action_description", "某个新动作")
        action_id_new = initiated_action_this_cycle.get("action_id", "")[:8]
        updated_state["pending_action_status"] = (
            f"你刚刚决定尝试动作 '{action_desc_new}' (ID: {action_id_new})，目前正在处理中..."
        )
        updated_state["action_result_info"] = ""
    elif action_id_whose_result_was_shown:
        updated_state["pending_action_status"] = ""
        updated_state["action_result_info"] = "你上一轮的动作结果已处理。"
    else:
        updated_state["pending_action_status"] = INITIAL_STATE["pending_action_status"]
        updated_state["action_result_info"] = INITIAL_STATE["action_result_info"]
    return updated_state


async def start_consciousness_flow() -> None:
    global stop_intrusive_thread  # intrusive_thoughts_pool_collection 不再是此模块的全局变量
    try:
        root_cfg: AlcarusRootConfig = get_typed_settings()
        logger.info("应用配置已成功加载并转换为类型化对象。")
    except Exception as e_cfg:
        logger.critical(f"严重：无法加载或解析程序配置，程序无法启动: {e_cfg}", exc_info=True)
        return
    try:
        _initialize_core_llm_clients(root_cfg)
    except RuntimeError as e_llm_init:
        logger.critical(f"严重：核心LLM客户端初始化失败，程序无法继续: {e_llm_init}", exc_info=True)
        return
    try:
        await initialize_llm_clients_for_action_module()
    except Exception as e_action_init:
        logger.warning(f"警告：行动模块LLM客户端初始化失败，行动相关功能可能无法使用: {e_action_init}", exc_info=True)

    arango_client_instance: ArangoClient | None = None  # 重命名以区分 ArangoClient 类型和实例
    arango_db_instance: StandardDatabase | None = None
    main_thoughts_collection_instance: StandardCollection | None = None
    intrusive_thoughts_collection_instance: StandardCollection | None = None  # 用于侵入性思维

    try:
        db_connection_settings: DatabaseSettings = root_cfg.database
        arango_client_instance, arango_db_instance = arangodb_handler.connect_to_arangodb(db_connection_settings)

        main_thoughts_coll_name = ARANGODB_COLLECTION_CONFIG["main_thoughts_collection_name"]
        main_thoughts_collection_instance = arangodb_handler.ensure_collection_exists(
            arango_db_instance, main_thoughts_coll_name
        )

        intrusive_thoughts_coll_name = ARANGODB_COLLECTION_CONFIG["intrusive_thoughts_collection_name"]
        intrusive_thoughts_collection_instance = arangodb_handler.ensure_collection_exists(
            arango_db_instance, intrusive_thoughts_coll_name
        )

    except (ValueError, RuntimeError) as e_db_connect:
        logger.critical(f"严重：无法连接到 ArangoDB 或确保集合存在，程序无法继续: {e_db_connect}", exc_info=True)
        return

    intrusive_module_settings_obj: IntrusiveThoughtsSettings = root_cfg.intrusive_thoughts_module_settings
    intrusive_thread: threading.Thread | None = None
    if intrusive_module_settings_obj.enabled:
        if intrusive_thoughts_llm_client is None:
            logger.error("侵入性思维模块已启用，但其LLM客户端未能初始化。模块将不会启动。")
        elif arango_db_instance is None or intrusive_thoughts_collection_instance is None:
            logger.error("侵入性思维模块已启用，但 ArangoDB 未连接或集合未初始化。模块将不会启动。")
        else:
            try:
                logger.info(f"为侵入性思维模块准备集合: '{intrusive_thoughts_collection_instance.name}'")

                # 将 IntrusiveThoughtsSettings 对象转换为字典传递给后台线程，如果它期望字典
                # 或者修改 background_intrusive_thought_generator 接受 IntrusiveThoughtsSettings 对象
                # 假设它期望字典：
                intrusive_settings_dict = {  # 这个保持不变
                    "generation_interval_seconds": intrusive_module_settings_obj.generation_interval_seconds,
                    "insertion_probability": intrusive_module_settings_obj.insertion_probability,
                }

                # root_cfg 应该已经在此函数作用域内通过 get_typed_settings() 获取
                persona_configuration_for_intrusive = root_cfg.persona  # <--- 获取人格配置

                intrusive_thread = threading.Thread(
                    target=intrusive_thoughts.background_intrusive_thought_generator,
                    args=(
                        intrusive_thoughts_llm_client,
                        arango_db_instance,  # 传递数据库对象
                        intrusive_thoughts_collection_instance.name,  # 传递集合名称
                        intrusive_settings_dict,
                        stop_intrusive_thread,  # 这个是 threading.Event
                        persona_configuration_for_intrusive,  # <--- 新增传递人格配置
                    ),
                    daemon=True,
                )
                intrusive_thread.start()
                logger.info("侵入性思维后台生成线程已启动。")
            except Exception as e_intrusive_init:
                logger.error(f"启动侵入性思维模块时发生错误: {e_intrusive_init}。该模块将被禁用。", exc_info=True)
    else:
        logger.info("侵入性思维模块在配置文件中未启用。")

    if main_thoughts_collection_instance is None or arango_db_instance is None: # 确保检查的是实例
        logger.critical("严重错误：主 ArangoDB 数据库或集合未能初始化，无法开始意识流。")
        return

    latest_doc_from_db = await arangodb_handler.get_latest_thought_document_raw(
        arango_db_instance, main_thoughts_collection_instance.name
    )
    current_internal_state, action_id_whose_result_was_loaded = _process_db_document_to_state(latest_doc_from_db)

    core_logic_cfg: CoreLogicSettings = root_cfg.core_logic_settings
    # time_format_str: str = core_logic_cfg.time_format_string # 这一行被移除
    thinking_interval_sec: int = core_logic_cfg.thinking_interval_seconds

    # --- 硬编码的时间格式化字符串 ---
    TIME_FORMAT_HARDCODED: str = "%Y年%m月%d日 %H点%M分%S秒"
    # ---------------------------------
    logger.info("\n--- 霜的意识开始流动 (使用 ArangoDB) ---")

    try:
        loop_count: int = 0
        while not stop_intrusive_thread.is_set(): # 确保使用的是 stop_intrusive_thread
            loop_count += 1
            current_time_formatted_str = datetime.datetime.now().strftime(TIME_FORMAT_HARDCODED) # <--- 使用硬编码的格式
            background_action_tasks: set[asyncio.Task] = set()
            task_desc_for_prompt = current_internal_state.get("current_task", "")
            current_internal_state["current_task_info_for_prompt"] = (
                f"你当前的目标/任务是：【{task_desc_for_prompt}】"
                if task_desc_for_prompt
                else "你当前没有什么特定的目标或任务。"
            )
            intrusive_thought_to_inject_this_cycle: str = ""
            if (
                intrusive_module_settings_obj.enabled
                and arango_db_instance is not None
                and intrusive_thoughts_collection_instance is not None
                and random.random() < intrusive_module_settings_obj.insertion_probability
            ):
                random_thought_doc = await arangodb_handler.get_random_intrusive_thought(
                    arango_db_instance, intrusive_thoughts_collection_instance.name
                )
                if random_thought_doc and "text" in random_thought_doc:
                    intrusive_thought_to_inject_this_cycle = f"你突然有一个神奇的念头：{random_thought_doc['text']}"

            logger.info(f"\n[{datetime.datetime.now().strftime('%H:%M:%S')} - 轮次 {loop_count}] 霜正在思考...")
            if intrusive_thought_to_inject_this_cycle:
                logger.info(f"  注入侵入性思维: {intrusive_thought_to_inject_this_cycle[:60]}...")

            if main_consciousness_llm_client is None:
                logger.error("主意识LLM客户端未初始化，无法生成思考。跳过本轮。")
                await asyncio.sleep(thinking_interval_sec)
                continue

            generated_thought_json, full_prompt_text_sent = await _generate_thought_from_llm(
                llm_client=main_consciousness_llm_client,  # 明确参数名，好习惯
                current_state_for_prompt=current_internal_state,
                current_time_str=current_time_formatted_str,
                root_cfg=root_cfg,
                intrusive_thought_str=intrusive_thought_to_inject_this_cycle,
            )

            initiated_action_data_for_db: dict[str, Any] | None = None
            action_info_for_task: dict[str, Any] | None = None
            saved_doc_key: str | None = None

            if generated_thought_json:
                logger.info(
                    f"  主思维LLM输出的完整JSON:\n{json.dumps(generated_thought_json, indent=2, ensure_ascii=False)}"
                )
                action_desc_raw = generated_thought_json.get("action_to_take")
                action_desc_from_llm = action_desc_raw.strip() if isinstance(action_desc_raw, str) else ""
                action_motive_raw = generated_thought_json.get("action_motivation")
                action_motive_from_llm = action_motive_raw.strip() if isinstance(action_motive_raw, str) else ""

                if action_desc_from_llm:
                    action_id_this_cycle = str(uuid.uuid4())
                    initiated_action_data_for_db = {
                        "action_description": action_desc_from_llm,
                        "action_motivation": action_motive_from_llm,
                        "action_id": action_id_this_cycle,  # 确保这个ID被 process_action_flow 使用
                        "status": "PENDING",  # 初始状态可以是 PENDING 或 PENDING_DECISION
                        "result_seen_by_shuang": False,
                        "initiated_at": datetime.datetime.now(datetime.UTC).isoformat(),
                    }
                    action_info_for_task = {
                        "action_id": action_id_this_cycle,
                        "action_description": action_desc_from_llm,
                        "action_motivation": action_motive_from_llm,
                        "current_thought_context": generated_thought_json.get("think", "无特定思考上下文。"),
                    }
                    logger.info(f"  >>> 行动意图产生: '{action_desc_from_llm}' (ID: {action_id_this_cycle[:8]})")

                document_to_save_in_main = {  # 构建要保存到主思考集合的文档
                    "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
                    "time_injected_to_prompt": current_time_formatted_str,
                    "intrusive_thought_injected": intrusive_thought_to_inject_this_cycle,
                    "mood_input": current_internal_state["mood"],
                    "previous_thinking_input": current_internal_state["previous_thinking"],
                    "thinking_guidance_input": current_internal_state["thinking_guidance"],
                    "task_input_info": current_internal_state.get("current_task_info_for_prompt", "无特定任务输入"),
                    "action_result_input": current_internal_state.get("action_result_info", ""),
                    "pending_action_status_input": current_internal_state.get("pending_action_status", ""),
                    "full_prompt_sent": full_prompt_text_sent if full_prompt_text_sent else "Prompt未能构建",
                    "think_output": generated_thought_json.get("think"),
                    "emotion_output": generated_thought_json.get("emotion"),
                    "next_think_output": generated_thought_json.get("next_think"),
                    "to_do_output": generated_thought_json.get("to_do", ""),
                    "done_output": generated_thought_json.get("done", False),
                    "action_to_take_output": generated_thought_json.get("action_to_take", ""),
                    "action_motivation_output": generated_thought_json.get("action_motivation", ""),
                    "action_attempted": initiated_action_data_for_db,  # 保存行动的初始数据
                }
                if "_llm_usage_info" in generated_thought_json:
                    document_to_save_in_main["_llm_usage_info"] = generated_thought_json["_llm_usage_info"]

                if main_thoughts_collection_instance is not None:
                    saved_doc_key = await arangodb_handler.save_thought_document(
                        main_thoughts_collection_instance, document_to_save_in_main
                    )
                    if action_id_whose_result_was_loaded and arango_db_instance:
                        await arangodb_handler.mark_action_result_as_seen(
                            arango_db_instance,
                            main_thoughts_collection_instance.name,
                            action_id_whose_result_was_loaded,
                        )
                else:
                    logger.error(
                        "关键错误：main_thoughts_collection_instance 对象为 None，无法保存思考或标记行动结果。"
                    )

                if (
                    action_info_for_task
                    and saved_doc_key
                    and main_thoughts_collection_instance is not None
                    and arango_db_instance is not None
                ):
                    action_task = asyncio.create_task(
                        process_action_flow(
                            action_id=action_info_for_task["action_id"],
                            doc_key_for_updates=saved_doc_key,
                            action_description=action_info_for_task["action_description"],
                            action_motivation=action_info_for_task["action_motivation"],
                            current_thought_context=action_info_for_task["current_thought_context"],
                            arango_db_for_updates=arango_db_instance,
                            collection_name_for_updates=main_thoughts_collection_instance.name,
                        )
                    )
                    background_action_tasks.add(action_task)
                    action_task.add_done_callback(background_action_tasks.discard)
                    logger.info(
                        f"      动作 '{action_info_for_task['action_description']}' (ID: {action_info_for_task['action_id'][:8]}, DocKey: {saved_doc_key}) 已异步启动处理。"
                    )
                elif action_info_for_task and not saved_doc_key:
                    logger.error(
                        f"未能获取保存文档的 _key，无法为动作 ID {action_info_for_task['action_id']} 创建处理任务。"
                    )
                elif action_info_for_task and (main_thoughts_collection_instance is None or arango_db_instance is None):
                    logger.error(
                        "严重错误：ArangoDB 数据库或主集合未初始化，即使已保存思考，也无法为行动处理流程提供数据库更新。行动任务未启动。"
                    )

                current_internal_state = _update_current_internal_state_after_thought(
                    current_internal_state,
                    generated_thought_json,
                    initiated_action_data_for_db,
                    action_id_whose_result_was_loaded,
                )
            else:
                logger.warning("  本轮思考生成失败或无内容。")

            logger.info(f"  等待 {thinking_interval_sec} 秒...")
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(stop_intrusive_thread.wait),
                    timeout=float(thinking_interval_sec),
                )
                if stop_intrusive_thread.is_set():
                    logger.info("主循环等待被停止事件中断（在 stop_intrusive_thread.wait() 完成后检测到）。")
                    break
            except TimeoutError:
                logger.debug(f"等待 {thinking_interval_sec} 秒超时，事件未被设置。继续循环。")
                pass
            except asyncio.CancelledError:
                logger.info("主循环的 sleep (asyncio.wait_for) 被取消，准备退出。")
                stop_intrusive_thread.set()
                break

            if stop_intrusive_thread.is_set():
                logger.info("主循环在等待间隔后检测到停止事件，准备退出。")
                break

            if main_thoughts_collection_instance is not None and arango_db_instance is not None:
                logger.info("主循环：即将调用 arangodb_handler.get_latest_thought_document_raw 来获取 ArangoDB 状态...")
                latest_doc_from_db = await arangodb_handler.get_latest_thought_document_raw(
                    arango_db_instance, main_thoughts_collection_instance.name
                )
                current_internal_state, action_id_whose_result_was_loaded = _process_db_document_to_state(
                    latest_doc_from_db
                )
                logger.info("主循环：数据库状态获取与处理完成。")
            else:
                logger.error("主 ArangoDB 数据库或集合未初始化，无法为下一轮加载最新状态。意识流可能无法正确继续。")

    except KeyboardInterrupt:
        logger.info("\n--- 霜的意识流动被用户手动中断 (KeyboardInterrupt) ---")
        stop_intrusive_thread.set()
    except asyncio.CancelledError:
        logger.info("\n--- 霜的意识流动主任务被取消 ---")
        stop_intrusive_thread.set()
    except Exception as e_main_loop:
        logger.critical(f"\n--- 意识流动主循环发生意外错误: {e_main_loop} ---", exc_info=True)
        stop_intrusive_thread.set()

    finally:
        logger.info("--- 开始程序清理 (ArangoDB) ---")
        if not stop_intrusive_thread.is_set():
            logger.info("在 finally 块中设置停止事件，以确保所有后台任务收到信号。")
            stop_intrusive_thread.set()

        if intrusive_thread is not None and intrusive_thread.is_alive():
            logger.info("等待侵入性思维线程结束...")
            intrusive_thread.join(timeout=5)
            if intrusive_thread.is_alive():
                logger.warning("警告：侵入性思维线程在超时后仍未结束。")
            else:
                logger.info("侵入性思维线程已成功结束。")

        if "background_action_tasks" in locals() and background_action_tasks:
            logger.info(f"等待 {len(background_action_tasks)} 个剩余的后台行动任务完成...")
            try:
                await asyncio.wait(background_action_tasks, timeout=10.0, return_when=asyncio.ALL_COMPLETED)
            except TimeoutError:
                logger.warning("等待后台行动任务完成超时。可能仍有任务在运行。")
                for task in background_action_tasks:
                    if not task.done():
                        task.cancel()
                        logger.info(f"已取消一个未完成的后台行动任务: {task.get_name()}")
                with contextlib.suppress(Exception):  # type: ignore
                    await asyncio.gather(*[t for t in background_action_tasks if not t.done()], return_exceptions=True)
            logger.info("后台行动任务处理完毕。")

        if arango_client_instance is not None:
            logger.info("ArangoDB 客户端通常不需要显式关闭。")

        logger.info("程序清理完成。霜的意识已停止流动。")
