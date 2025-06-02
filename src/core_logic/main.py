# src/core_logic/main.py
import asyncio
import datetime
import json
import os
import random
import re
import threading
import uuid
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from src.action.action_handler import ActionHandler
from src.common.custom_logging.logger_manager import get_logger
from src.common.utils import format_chat_history_for_prompt
from src.config.alcarus_configs import (
    AlcarusRootConfig,
    CoreLogicSettings,
    IntrusiveThoughtsSettings,
    LLMClientSettings,
    ModelParams,
    PersonaSettings,
    ProxySettings,
)
from src.config.config_manager import get_typed_settings
from src.core_communication.core_ws_server import CoreWebsocketServer
from src.core_logic.intrusive_thoughts import IntrusiveThoughtsGenerator
from src.database.arangodb_handler import ArangoDBHandler
from src.llmrequest.llm_processor import Client as ProcessorClient
from src.message_processing.default_message_processor import DefaultMessageProcessor

if TYPE_CHECKING:
    pass


class CoreLogic:
    INITIAL_STATE: dict[str, Any] = {
        "mood": "你现在的心情大概是：平静。",
        "previous_thinking": "你的上一轮思考是：这是你的第一次思考，请开始吧。",
        "thinking_guidance": "经过你上一轮的思考，你目前打算的思考方向是：随意发散一下吧。",
        "current_task": "没有什么具体目标",
        "action_result_info": "你上一轮没有执行产生结果的特定行动。",
        "pending_action_status": "",
        "recent_contextual_information": "最近未感知到任何特定信息或通知。",
    }

    # ███ 小懒猫改动开始 ███
    # PROMPT_TEMPLATE 修改：移除了 current_time, bot_name, persona_description, persona_profile
    PROMPT_TEMPLATE: str = """{current_task_info}

{action_result_info}
{pending_action_status}

{recent_contextual_information}

{previous_thinking}；

{mood}；

{thinking_guidance}；

{intrusive_thought}

现在是你的内心思考时间，请结合以上所有信息，思考并输出你真实的内心想法；
注意内心想法不一定全是正向的，也有可能是不健康的，隐私的，自私自利的，不可告人的，甚至成人的。请避免过度合理化、道德化；
同时也要注意 你对自身的想法大多数情况下是包容的、合理化的，避免过度的/不必要的矛盾心理。

关于行动：
如果你在思考后，产生了明确的、希望与“外界”（例如上网获取信息、获取qq群聊/好友列表）进行交互的意图，请在action_to_take和action_motivation字段中描述。

严格以json字段输出：
{{
    "think": "思考内容文本，注意不要过于冗长",
    "emotion": "当前心情和造成这个心情的原因",
    "to_do": "【可选】如果你产生了明确的目标，可以在此处写下。如果没有特定目标，则留null。即使当前已有明确目标，你也可以在这里更新它",
    "done": "【可选】布尔值，如果该目标已完成、不再需要或你决定放弃，则设为true，会清空目前目标；如果目标未完成且需要继续，则设为false。如果当前无目标，也为false",
    "action_to_take": "【可选】描述你当前想做的、需要与外界交互的具体动作。如果无，则为null",
    "action_motivation": "【可选】如果你有想做的动作，请说明其动机。如果action_to_take为null，此字段也应为null",
    "next_think": "下一步打算思考的方向"
}}

请输出你的思考 JSON：
"""
    # ███ 小懒猫改动结束 ███

    def __init__(self) -> None:
        self.logger = get_logger(f"AIcarusCore.{self.__class__.__name__}")
        self.root_cfg: AlcarusRootConfig | None = None
        self.db_handler: ArangoDBHandler | None = None
        self.main_consciousness_llm_client: ProcessorClient | None = None
        self.intrusive_thoughts_llm_client: ProcessorClient | None = None
        self.stop_event: threading.Event = threading.Event()
        self.core_comm_layer: CoreWebsocketServer | None = None
        self.message_processor: DefaultMessageProcessor | None = None
        self.current_focused_conversation_id: str | None = None
        self.action_handler_instance: ActionHandler | None = None
        self.intrusive_generator_instance: IntrusiveThoughtsGenerator | None = None
        self.intrusive_thread: threading.Thread | None = None
        self.thinking_loop_task: asyncio.Task | None = None
        self.server_task: asyncio.Task | None = None
        self.logger.info(f"{self.__class__.__name__} instance created.")

    def _initialize_core_llm_clients(self) -> None:
        if not self.root_cfg:
            self.logger.critical("Root config not loaded. Cannot initialize LLM clients.")
            raise RuntimeError("Root config not loaded. Cannot initialize LLM clients.")
        self.logger.info("开始初始化核心LLM客户端 (主意识和侵入性思维)...")
        general_llm_settings_obj: LLMClientSettings = self.root_cfg.llm_client_settings
        proxy_settings_obj: ProxySettings = self.root_cfg.proxy
        final_proxy_host: str | None = None
        final_proxy_port: int | None = None
        if proxy_settings_obj.use_proxy and proxy_settings_obj.http_proxy_url:
            try:
                parsed_url = urlparse(proxy_settings_obj.http_proxy_url)
                final_proxy_host = parsed_url.hostname
                final_proxy_port = parsed_url.port
                if not final_proxy_host or final_proxy_port is None:
                    self.logger.warning(f"代理URL '{proxy_settings_obj.http_proxy_url}' 解析不完整。将不使用代理。")
                    final_proxy_host, final_proxy_port = None, None
            except Exception as e_parse_proxy:
                self.logger.warning(
                    f"解析代理URL '{proxy_settings_obj.http_proxy_url}' 失败: {e_parse_proxy}。将不使用代理。"
                )
                final_proxy_host, final_proxy_port = None, None
        resolved_abandoned_keys: list[str] | None = None
        env_val_abandoned = os.getenv("LLM_ABANDONED_KEYS")
        if env_val_abandoned:
            try:
                keys_from_env = json.loads(env_val_abandoned)
                if isinstance(keys_from_env, list):
                    resolved_abandoned_keys = [str(k).strip() for k in keys_from_env if str(k).strip()]
            except json.JSONDecodeError:
                self.logger.warning(
                    f"环境变量 'LLM_ABANDONED_KEYS' 值不是有效JSON列表。值: {env_val_abandoned[:50]}..."
                )
                resolved_abandoned_keys = [k.strip() for k in env_val_abandoned.split(",") if k.strip()]
            if not resolved_abandoned_keys and env_val_abandoned.strip():
                resolved_abandoned_keys = [env_val_abandoned.strip()]

        def _create_single_processor_client(purpose_key: str, default_provider_name: str) -> ProcessorClient | None:
            try:
                if self.root_cfg is None or self.root_cfg.providers is None:
                    self.logger.error("配置错误：AlcarusRootConfig 中缺少 'providers' 配置段。")
                    return None
                provider_settings = getattr(self.root_cfg.providers, default_provider_name.lower(), None)
                if provider_settings is None or provider_settings.models is None:
                    self.logger.error(
                        f"配置错误：未找到提供商 '{default_provider_name}' 的有效配置或其 'models' 配置段。"
                    )
                    return None
                model_params_cfg = getattr(provider_settings.models, purpose_key, None)
                if not isinstance(model_params_cfg, ModelParams):
                    self.logger.error(f"配置错误：模型用途键 '{purpose_key}' 配置无效或类型不匹配。")
                    return None
                actual_provider_name_str: str = model_params_cfg.provider
                actual_model_api_name: str = model_params_cfg.model_name
                if not actual_provider_name_str or not actual_model_api_name:
                    self.logger.error(f"配置错误：模型 '{purpose_key}' 未指定 'provider' 或 'model_name'。")
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
                self.logger.info(
                    f"成功为用途 '{purpose_key}' 创建 ProcessorClient 实例 (模型: {client_instance.llm_client.model_name}, 提供商: {client_instance.llm_client.provider})."
                )
                return client_instance
            except AttributeError as e_attr:
                self.logger.error(
                    f"配置访问错误 (AttributeError) 为用途 '{purpose_key}' 创建LLM客户端时: {e_attr}", exc_info=True
                )
                return None
            except Exception as e:
                self.logger.error(f"为用途 '{purpose_key}' 创建LLM客户端时发生未知错误: {e}", exc_info=True)
                return None

        try:
            self.main_consciousness_llm_client = _create_single_processor_client("main_consciousness", "gemini")
            if not self.main_consciousness_llm_client:
                raise RuntimeError("主意识 LLM 客户端初始化失败。")
            self.intrusive_thoughts_llm_client = _create_single_processor_client("intrusive_thoughts", "gemini")
            if not self.intrusive_thoughts_llm_client:
                raise RuntimeError("侵入性思维 LLM 客户端初始化失败。")
            self.logger.info("核心LLM客户端 (主意识和侵入性思维) 已成功初始化。")
        except RuntimeError:
            raise
        except Exception as e_init_core:
            self.logger.critical(f"初始化核心LLM客户端过程中发生未预期的严重错误: {e_init_core}", exc_info=True)
            raise RuntimeError(f"核心LLM客户端初始化因意外错误失败: {e_init_core}") from e_init_core

    def _process_thought_and_action_state(
        self, latest_thought_document: dict[str, Any] | None, formatted_recent_contextual_info: str
    ) -> tuple[dict[str, Any], str | None]:
        action_id_whose_result_is_being_shown: str | None = None
        state_from_initial = self.INITIAL_STATE.copy()
        if not latest_thought_document:
            self.logger.info("最新的思考文档为空，使用初始思考状态。")
            mood_for_prompt = state_from_initial["mood"]
            previous_thinking_for_prompt = state_from_initial["previous_thinking"]
            thinking_guidance_for_prompt = state_from_initial["thinking_guidance"]
            current_task_for_prompt = state_from_initial["current_task"]
        else:
            mood_db = latest_thought_document.get("emotion_output", state_from_initial["mood"].split("：", 1)[-1])
            mood_for_prompt = f"你现在的心情大概是：{mood_db}"
            prev_think_db = latest_thought_document.get("think_output")
            previous_thinking_for_prompt = (
                f"你的上一轮思考是：{prev_think_db}"
                if prev_think_db and prev_think_db.strip()
                else state_from_initial["previous_thinking"]
            )
            guidance_db = latest_thought_document.get(
                "next_think_output",
                state_from_initial["thinking_guidance"].split("：", 1)[-1]
                if "：" in state_from_initial["thinking_guidance"]
                else (state_from_initial["thinking_guidance"] or "随意发散一下吧."),
            )
            thinking_guidance_for_prompt = f"经过你上一轮的思考，你目前打算的思考方向是：{guidance_db}"
            current_task_for_prompt = latest_thought_document.get("to_do_output", state_from_initial["current_task"])
            if latest_thought_document.get(
                "done_output", False
            ) and current_task_for_prompt == latest_thought_document.get("to_do_output"):
                current_task_for_prompt = ""
        action_result_info_prompt = state_from_initial["action_result_info"]
        pending_action_status_prompt = state_from_initial["pending_action_status"]
        last_action_attempt = latest_thought_document.get("action_attempted") if latest_thought_document else None
        if last_action_attempt and isinstance(last_action_attempt, dict):
            action_status = last_action_attempt.get("status")
            action_description = last_action_attempt.get("action_description", "某个之前的动作")
            action_id = last_action_attempt.get("action_id")
            was_result_seen_by_llm = last_action_attempt.get("result_seen_by_shuang", False)
            if action_status in ["COMPLETED_SUCCESS", "COMPLETED_FAILURE", "CRITICAL_FAILURE"]:
                if not was_result_seen_by_llm and action_id:
                    final_result = last_action_attempt.get(
                        "final_result_for_shuang", "动作已完成，但没有具体结果反馈。"
                    )
                    action_result_info_prompt = (
                        f"你上一轮行动 '{action_description}' "
                        f"(ID: {action_id[:8] if action_id else 'N/A'}) 的结果是：【{str(final_result)[:500]}】"
                    )
                    action_id_whose_result_is_being_shown = action_id
                    pending_action_status_prompt = ""
                elif was_result_seen_by_llm:
                    action_result_info_prompt = "你上一轮的动作结果已处理。"
                    pending_action_status_prompt = ""
            elif action_status and action_status not in ["COMPLETED_SUCCESS", "COMPLETED_FAILURE", "CRITICAL_FAILURE"]:
                action_motivation = last_action_attempt.get("action_motivation", "之前的动机")
                pending_action_status_prompt = (
                    f"你之前尝试的动作 '{action_description}' "
                    f"(ID: {action_id[:8] if action_id else 'N/A'}) "
                    f"(动机: '{action_motivation}') "
                    f"目前还在处理中 ({action_status})。"
                )
                action_result_info_prompt = ""
        state_for_prompt: dict[str, Any] = {
            "mood": mood_for_prompt,
            "previous_thinking": previous_thinking_for_prompt,
            "thinking_guidance": thinking_guidance_for_prompt,
            "current_task": current_task_for_prompt,
            "action_result_info": action_result_info_prompt,
            "pending_action_status": pending_action_status_prompt,
            "recent_contextual_information": formatted_recent_contextual_info,
        }
        self.logger.info("在 _process_thought_and_action_state 中：成功处理并返回用于Prompt的状态。")
        return state_for_prompt, action_id_whose_result_is_being_shown

    async def _generate_thought_from_llm(
        self,
        llm_client: ProcessorClient,
        current_state_for_prompt: dict[str, Any],
        current_time_str: str,
        intrusive_thought_str: str = "",
    ) -> tuple[dict[str, Any] | None, str | None, str | None]:  # 返回值增加了 system_prompt
        if not self.root_cfg:
            self.logger.error("Root config not available for LLM thought generation.")
            return None, None, None

        persona_cfg = self.root_cfg.persona
        task_desc = current_state_for_prompt.get("current_task", "")
        task_info_prompt = f"你当前的目标/任务是：【{task_desc}】" if task_desc else "你当前没有什么特定的目标或任务。"

        # ███ 小懒猫改动开始 ███
        # 组装 system_prompt
        system_prompt_parts = [
            f"当前时间：{current_time_str}",
            f"你是{persona_cfg.bot_name}；",
            persona_cfg.description,
            persona_cfg.profile,
        ]
        system_prompt_str = "\n".join(filter(None, system_prompt_parts))  # 过滤掉空字符串并用换行符连接
        # ███ 小懒猫改动结束 ███

        prompt_text = self.PROMPT_TEMPLATE.format(
            # 移除了 current_time, bot_name, persona_description, persona_profile
            current_task_info=task_info_prompt,
            mood=current_state_for_prompt.get("mood", self.INITIAL_STATE["mood"]),
            previous_thinking=current_state_for_prompt.get(
                "previous_thinking", self.INITIAL_STATE["previous_thinking"]
            ),
            thinking_guidance=current_state_for_prompt.get(
                "thinking_guidance", self.INITIAL_STATE["thinking_guidance"]
            ),
            action_result_info=current_state_for_prompt.get(
                "action_result_info", self.INITIAL_STATE["action_result_info"]
            ),
            pending_action_status=current_state_for_prompt.get(
                "pending_action_status", self.INITIAL_STATE["pending_action_status"]
            ),
            recent_contextual_information=current_state_for_prompt.get(
                "recent_contextual_information", self.INITIAL_STATE["recent_contextual_information"]
            ),
            intrusive_thought=intrusive_thought_str,
        )
        # ███ 小懒猫改动开始 ███
        self.logger.debug(
            f"--- 主思维LLM接收到的 System Prompt (模型: {llm_client.llm_client.model_name}) ---\n{system_prompt_str}\n--- System Prompt结束 ---"
        )
        # ███ 小懒猫改动结束 ███
        self.logger.debug(
            f"--- 主思维LLM接收到的 User Prompt (模型: {llm_client.llm_client.model_name}) ---\n{prompt_text}\n--- User Prompt结束 ---"
        )
        self.logger.debug(
            f"正在请求 {llm_client.llm_client.provider} API ({llm_client.llm_client.model_name}) 生成主思考..."
        )
        raw_response_text: str = ""
        try:
            # ███ 小懒猫改动开始 ███
            # 调用 make_llm_request 时传入 system_prompt_str
            response_data = await llm_client.make_llm_request(
                prompt=prompt_text,
                system_prompt=system_prompt_str,  # 把 system_prompt 传进去！
                is_stream=False,
            )
            # ███ 小懒猫改动结束 ███

            if response_data.get("error"):
                error_type = response_data.get("type", "UnknownError")
                error_msg = response_data.get("message", "LLM客户端返回了一个错误")
                self.logger.error(f"主思维LLM调用失败 ({error_type}): {error_msg}")
                if response_data.get("details"):
                    self.logger.error(f"  错误详情: {str(response_data.get('details'))[:300]}...")
                return None, prompt_text, system_prompt_str  # 返回 system_prompt
            raw_response_text = response_data.get("text")  # type: ignore
            if not raw_response_text:
                error_msg = "错误：主思维LLM响应中缺少文本内容。"
                if response_data:
                    error_msg += f"\n  完整响应: {str(response_data)[:500]}..."
                self.logger.error(error_msg)
                return None, prompt_text, system_prompt_str  # 返回 system_prompt
            json_to_parse = raw_response_text.strip()
            if json_to_parse.startswith("```json"):
                json_to_parse = json_to_parse[7:-3].strip()
            elif json_to_parse.startswith("```"):
                json_to_parse = json_to_parse[3:-3].strip()
            json_to_parse = re.sub(r"[,\s]+(\}|\])$", r"\1", json_to_parse)
            thought_json: dict[str, Any] = json.loads(json_to_parse)
            self.logger.info("主思维LLM API 响应已成功解析为JSON。")
            if response_data.get("usage"):
                thought_json["_llm_usage_info"] = response_data["usage"]
            return thought_json, prompt_text, system_prompt_str  # 返回 system_prompt
        except json.JSONDecodeError as e:
            self.logger.error(f"错误：解析主思维LLM的JSON响应失败: {e}")
            self.logger.error(f"未能解析的文本内容: {raw_response_text}")
            return None, prompt_text, system_prompt_str  # 返回 system_prompt
        except Exception as e:
            self.logger.error(f"错误：调用主思维LLM或处理其响应时发生意外错误: {e}", exc_info=True)
            return None, prompt_text, system_prompt_str  # 返回 system_prompt

    async def _core_thinking_loop(self) -> None:
        if not self.root_cfg or not self.db_handler or not self.main_consciousness_llm_client:
            self.logger.critical("核心思考循环无法启动：缺少必要的配置、数据库处理器或主LLM客户端。")
            return
        action_id_whose_result_was_shown_in_last_prompt: str | None = None
        core_logic_cfg: CoreLogicSettings = self.root_cfg.core_logic_settings
        time_format_str: str = "%Y年%m月%d日 %H点%M分%S秒"
        thinking_interval_sec: int = core_logic_cfg.thinking_interval_seconds

        chat_history_duration_minutes: int = getattr(core_logic_cfg, "chat_history_context_duration_minutes", 10)
        self.logger.info(
            f"聊天记录上下文时长配置为: {chat_history_duration_minutes} 分钟 (如果配置中未找到则使用默认值)。"
        )

        self.logger.info(f"\n--- {self.root_cfg.persona.bot_name} 的意识开始流动 (更新上下文描述) ---")
        loop_count: int = 0
        while not self.stop_event.is_set():
            loop_count += 1
            current_time_formatted_str = datetime.datetime.now().strftime(time_format_str)
            background_action_tasks: set[asyncio.Task] = set()
            latest_thought_doc_from_db = await self.db_handler.get_latest_thought_document_raw()

            formatted_recent_contextual_info = self.INITIAL_STATE["recent_contextual_information"]
            try:
                raw_context_messages = await self.db_handler.get_recent_chat_messages_for_context(
                    duration_minutes=chat_history_duration_minutes, conversation_id=self.current_focused_conversation_id
                )
                self.logger.info(f"从数据库获取到的原始上下文消息数量: {len(raw_context_messages)}")
                if raw_context_messages:
                    self.logger.debug(f"获取到的原始上下文消息样本 (前2条): {raw_context_messages[:2]}")
                else:
                    self.logger.warning("注意：从数据库未能获取到任何用于上下文的原始消息。")
                if raw_context_messages:
                    formatted_recent_contextual_info = format_chat_history_for_prompt(raw_context_messages)
                else:
                    self.logger.debug(f"在过去 {chat_history_duration_minutes} 分钟内未找到用于上下文的信息。")
            except Exception as e_hist:
                self.logger.error(f"获取或格式化最近上下文信息时出错: {e_hist}", exc_info=True)

            current_state_for_prompt, action_id_whose_result_was_shown_in_last_prompt = (
                self._process_thought_and_action_state(latest_thought_doc_from_db, formatted_recent_contextual_info)
            )
            task_desc_for_prompt = current_state_for_prompt.get("current_task", "")
            current_state_for_prompt["current_task_info_for_prompt"] = (
                f"你当前的目标/任务是：【{task_desc_for_prompt}】"
                if task_desc_for_prompt
                else "你当前没有什么特定的目标或任务。"
            )

            intrusive_thought_to_inject_this_cycle: str = ""
            if (
                self.intrusive_generator_instance
                and self.intrusive_generator_instance.module_settings.enabled
                and random.random() < self.intrusive_generator_instance.module_settings.insertion_probability
            ):
                random_thought_doc = await self.db_handler.get_random_intrusive_thought()
                if random_thought_doc and "text" in random_thought_doc:
                    intrusive_thought_to_inject_this_cycle = f"你突然有一个神奇的念头：{random_thought_doc['text']}"
            self.logger.debug(
                f"\n[{datetime.datetime.now().strftime('%H:%M:%S')} - 轮次 {loop_count}] {self.root_cfg.persona.bot_name} 正在思考..."
            )
            if intrusive_thought_to_inject_this_cycle:
                self.logger.debug(f"  注入侵入性思维: {intrusive_thought_to_inject_this_cycle[:60]}...")

            # ███ 小懒猫改动开始 ███
            # _generate_thought_from_llm 现在返回三个值
            generated_thought_json, full_prompt_text_sent, system_prompt_sent = await self._generate_thought_from_llm(
                llm_client=self.main_consciousness_llm_client,  # type: ignore
                current_state_for_prompt=current_state_for_prompt,
                current_time_str=current_time_formatted_str,
                intrusive_thought_str=intrusive_thought_to_inject_this_cycle,
            )
            # ███ 小懒猫改动结束 ███

            initiated_action_data_for_db: dict[str, Any] | None = None
            action_info_for_task: dict[str, Any] | None = None
            saved_thought_doc_key: str | None = None
            if generated_thought_json:
                self.logger.debug(
                    f"  主思维LLM输出的完整JSON:\n{json.dumps(generated_thought_json, indent=2, ensure_ascii=False)}"
                )
                think_output = generated_thought_json.get("think") or "未思考"
                emotion_output = generated_thought_json.get("emotion") or "无特定情绪"
                to_do_output = generated_thought_json.get("to_do")
                action_to_take_output = generated_thought_json.get("action_to_take")
                action_motivation_output = generated_thought_json.get("action_motivation")
                next_think_output = generated_thought_json.get("next_think") or "未明确下一步思考方向"
                bot_name_for_log = self.root_cfg.persona.bot_name if self.root_cfg else "机器人"
                log_message = (
                    f'{bot_name_for_log}现在的想法是 "{think_output}"，'
                    f'心情 "{emotion_output}"，'
                    f'目标是 "{to_do_output if to_do_output is not None else "无特定目标"}"，'
                    f'想做的事情是 "{action_to_take_output if action_to_take_output is not None else "无"}"，'
                    f'原因是 "{action_motivation_output if action_motivation_output is not None else "无"}"，'
                    f'{bot_name_for_log}的下一步大概思考方向是 "{next_think_output}"'
                )
                self.logger.info(log_message)
                action_desc_raw = generated_thought_json.get("action_to_take")
                action_desc_from_llm = action_desc_raw.strip() if isinstance(action_desc_raw, str) else ""
                action_motive_raw = generated_thought_json.get("action_motivation")
                action_motive_from_llm = action_motive_raw.strip() if isinstance(action_motive_raw, str) else ""
                if action_desc_from_llm:
                    action_id_this_cycle = str(uuid.uuid4())
                    initiated_action_data_for_db = {
                        "action_description": action_desc_from_llm,
                        "action_motivation": action_motive_from_llm,
                        "action_id": action_id_this_cycle,
                        "status": "PENDING",
                        "result_seen_by_shuang": False,
                        "initiated_at": datetime.datetime.now(datetime.UTC).isoformat(),
                    }
                    action_info_for_task = {
                        "action_id": action_id_this_cycle,
                        "action_description": action_desc_from_llm,
                        "action_motivation": action_motive_from_llm,
                        "current_thought_context": generated_thought_json.get("think", "无特定思考上下文。"),
                    }
                    self.logger.debug(f"  >>> 行动意图产生: '{action_desc_from_llm}' (ID: {action_id_this_cycle[:8]})")

                # ███ 小懒猫改动开始 ███
                # 保存思考文档时，也记录下发送的 system_prompt
                document_to_save_in_main: dict[str, Any] = {
                    "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
                    "time_injected_to_prompt": current_time_formatted_str,  # 这个是当时 system_prompt 里的时间
                    "system_prompt_sent": system_prompt_sent
                    if system_prompt_sent
                    else "System Prompt 未能构建",  # 保存 system_prompt
                    "intrusive_thought_injected": intrusive_thought_to_inject_this_cycle,
                    "mood_input": current_state_for_prompt["mood"],
                    "previous_thinking_input": current_state_for_prompt["previous_thinking"],
                    "thinking_guidance_input": current_state_for_prompt["thinking_guidance"],
                    "task_input_info": current_state_for_prompt.get("current_task_info_for_prompt", "无特定任务输入"),
                    "action_result_input": current_state_for_prompt.get("action_result_info", ""),
                    "pending_action_status_input": current_state_for_prompt.get("pending_action_status", ""),
                    "recent_contextual_information_input": formatted_recent_contextual_info,
                    "full_user_prompt_sent": full_prompt_text_sent
                    if full_prompt_text_sent
                    else "User Prompt 未能构建",  # 修改键名以区分
                    "think_output": generated_thought_json.get("think"),
                    "emotion_output": generated_thought_json.get("emotion"),
                    "next_think_output": generated_thought_json.get("next_think"),
                    "to_do_output": generated_thought_json.get("to_do", ""),
                    "done_output": generated_thought_json.get("done", False),
                    "action_to_take_output": generated_thought_json.get("action_to_take", ""),
                    "action_motivation_output": generated_thought_json.get("action_motivation", ""),
                    "action_attempted": initiated_action_data_for_db,
                }
                # ███ 小懒猫改动结束 ███

                if "_llm_usage_info" in generated_thought_json:
                    document_to_save_in_main["_llm_usage_info"] = generated_thought_json["_llm_usage_info"]
                saved_thought_doc_key = await self.db_handler.save_thought_document(document_to_save_in_main)
                if action_id_whose_result_was_shown_in_last_prompt:
                    await self.db_handler.mark_action_result_as_seen(action_id_whose_result_was_shown_in_last_prompt)
                if action_info_for_task and saved_thought_doc_key and self.action_handler_instance:
                    action_task = asyncio.create_task(
                        self.action_handler_instance.process_action_flow(
                            action_id=action_info_for_task["action_id"],
                            doc_key_for_updates=saved_thought_doc_key,
                            action_description=action_info_for_task["action_description"],
                            action_motivation=action_info_for_task["action_motivation"],
                            current_thought_context=action_info_for_task["current_thought_context"],
                        )
                    )
                    background_action_tasks.add(action_task)
                    action_task.add_done_callback(background_action_tasks.discard)
                    self.logger.debug(
                        f"      动作 '{action_info_for_task['action_description']}' (ID: {action_info_for_task['action_id'][:8]}, 关联思考DocKey: {saved_thought_doc_key}) 已异步启动处理。"
                    )
                elif action_info_for_task and not saved_thought_doc_key:
                    self.logger.error(
                        f"未能获取保存思考文档的 _key，无法为动作 ID {action_info_for_task['action_id']} 创建处理任务。"
                    )
                elif action_info_for_task and not self.action_handler_instance:
                    self.logger.error(
                        f"ActionHandler 未初始化，无法为动作 ID {action_info_for_task['action_id']} 创建处理任务。"
                    )
            else:
                self.logger.warning("  本轮思考生成失败或无内容。")
            self.logger.debug(f"  等待 {thinking_interval_sec} 秒...")
            try:
                await asyncio.wait_for(asyncio.to_thread(self.stop_event.wait), timeout=float(thinking_interval_sec))
                if self.stop_event.is_set():
                    self.logger.info("主思考循环等待被停止事件中断。")
                    break
            except TimeoutError:
                self.logger.debug(f"等待 {thinking_interval_sec} 秒超时，事件未被设置。继续下一轮循环。")
            except asyncio.CancelledError:
                self.logger.info("主思考循环的 sleep 被取消，准备退出。")
                self.stop_event.set()
                break
            if self.stop_event.is_set():
                self.logger.info("主思考循环在等待间隔后检测到停止事件，准备退出。")
                break

    async def start(self) -> None:
        try:
            self.root_cfg = get_typed_settings()
            self.logger.info("应用配置已成功加载并转换为类型化对象。")
        except Exception as e_cfg:
            self.logger.critical(f"严重：无法加载或解析程序配置: {e_cfg}", exc_info=True)
            return
        try:
            self._initialize_core_llm_clients()
        except RuntimeError as e_llm_init:
            self.logger.critical(f"严重：核心LLM客户端初始化失败: {e_llm_init}", exc_info=True)
            return
        try:
            self.db_handler = await ArangoDBHandler.create()
            self.logger.info("ArangoDBHandler 实例创建成功。")
            if not self.db_handler or not self.db_handler.db:
                raise RuntimeError("ArangoDBHandler 或其内部 db 对象未能初始化。")
            if not self.root_cfg:
                raise RuntimeError("Root config 未能初始化。")
            self.message_processor = DefaultMessageProcessor(db_handler=self.db_handler, root_config=self.root_cfg)
            self.logger.info("DefaultMessageProcessor 已成功初始化。")
            await self.db_handler.ensure_collection_exists(ArangoDBHandler.THOUGHTS_COLLECTION_NAME)
            await self.db_handler.ensure_collection_exists(ArangoDBHandler.RAW_CHAT_MESSAGES_COLLECTION_NAME)
            await self.db_handler.ensure_collection_exists(ArangoDBHandler.INTRUSIVE_THOUGHTS_POOL_COLLECTION_NAME)
        except (ValueError, RuntimeError) as e_db_connect:
            self.logger.critical(f"严重：无法连接到 ArangoDB 或确保集合存在: {e_db_connect}", exc_info=True)
            return
        except Exception as e_init_other:
            self.logger.critical(f"初始化过程中发生意外错误: {e_init_other}", exc_info=True)
            return
        if self.root_cfg and self.db_handler:
            self.action_handler_instance = ActionHandler(root_cfg=self.root_cfg)
            try:
                await self.action_handler_instance.initialize_llm_clients()
            except RuntimeError as e_ah_llm_init:
                self.logger.critical(f"严重：ActionHandler LLM客户端初始化失败: {e_ah_llm_init}", exc_info=True)
                return
        else:
            self.logger.critical("无法初始化 ActionHandler：缺少 root_cfg 或 db_handler。")
            return
        ws_host = os.getenv("CORE_WS_HOST", "127.0.0.1")
        ws_port_str = os.getenv("CORE_WS_PORT", "8077")
        try:
            ws_port = int(ws_port_str)
        except ValueError:
            self.logger.critical(f"无效的 CORE_WS_PORT: '{ws_port_str}'。")
            return
        if not self.message_processor or not self.db_handler or not self.db_handler.db:
            self.logger.critical("严重：消息处理器或数据库处理器未能初始化，无法启动 WebSocket。")
            return
        self.core_comm_layer = CoreWebsocketServer(
            ws_host, ws_port, self.message_processor.process_message, self.db_handler.db
        )
        if self.action_handler_instance:
            self.action_handler_instance.set_dependencies(db_handler=self.db_handler, comm_layer=self.core_comm_layer)
        else:
            self.logger.warning("ActionHandler 实例未初始化，无法设置其通信层依赖。")
        self.server_task = asyncio.create_task(self.core_comm_layer.start())
        if self.root_cfg and self.intrusive_thoughts_llm_client and self.db_handler:
            intrusive_settings: IntrusiveThoughtsSettings = self.root_cfg.intrusive_thoughts_module_settings
            persona_settings: PersonaSettings = self.root_cfg.persona
            if intrusive_settings.enabled:
                self.intrusive_generator_instance = IntrusiveThoughtsGenerator(
                    llm_client=self.intrusive_thoughts_llm_client,
                    db_handler=self.db_handler,
                    persona_cfg=persona_settings,
                    module_settings=intrusive_settings,
                    stop_event=self.stop_event,
                )
                self.intrusive_thread = self.intrusive_generator_instance.start_background_generation()
                if self.intrusive_thread:
                    self.logger.info("侵入性思维后台生成线程已通过 IntrusiveThoughtsGenerator 启动。")
                else:
                    self.logger.warning("未能启动侵入性思维后台生成线程。")
            else:
                self.logger.info("侵入性思维模块在配置文件中未启用。")
        else:
            self.logger.warning("无法初始化 IntrusiveThoughtsGenerator：缺少必要依赖。")
        if not self.db_handler:
            self.logger.critical("严重错误：ArangoDB 处理器未能初始化，无法开始意识流。")
            if self.core_comm_layer:
                await self.core_comm_layer.stop()
            if self.server_task and not self.server_task.done():
                self.server_task.cancel()
            return
        self.thinking_loop_task = asyncio.create_task(self._core_thinking_loop())
        try:
            if not self.server_task or not self.thinking_loop_task:
                raise RuntimeError("服务器或思考循环任务未能成功创建。")
            done, pending = await asyncio.wait(
                [self.server_task, self.thinking_loop_task], return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                self.logger.info(f"一个关键任务已结束，正在取消挂起的任务: {task.get_name()}")
                task.cancel()  # type: ignore
            for task in done:
                if task.exception():
                    self.logger.critical(
                        f"一个关键任务 ({task.get_name()}) 因异常而结束: {task.exception()}", exc_info=task.exception()
                    )  # type: ignore
        except KeyboardInterrupt:
            self.logger.info(
                f"\n--- {(self.root_cfg.persona.bot_name if self.root_cfg else 'Bot')} 的意识流动被用户手动中断 ---"
            )
        except asyncio.CancelledError:
            self.logger.info(
                f"\n--- {(self.root_cfg.persona.bot_name if self.root_cfg else 'Bot')} 的意识流动主任务被取消 ---"
            )
        except Exception as e_main_flow:
            self.logger.critical(f"\n--- 意识流动主流程发生意外错误: {e_main_flow} ---", exc_info=True)
        finally:
            self.logger.info("--- 开始程序清理 ---")
            self.stop_event.set()
            if self.core_comm_layer:
                self.logger.info("正在停止核心 WebSocket 通信层...")
                await self.core_comm_layer.stop()
            if self.server_task and not self.server_task.done():
                self.logger.info("正在取消 WebSocket 服务器任务...")
                self.server_task.cancel()
                await asyncio.gather(self.server_task, return_exceptions=True)
            if self.thinking_loop_task and not self.thinking_loop_task.done():
                self.logger.info("正在取消核心思考循环任务...")
                self.thinking_loop_task.cancel()
                await asyncio.gather(self.thinking_loop_task, return_exceptions=True)
            if self.intrusive_thread is not None and self.intrusive_thread.is_alive():
                self.logger.info("等待侵入性思维线程结束...")
                self.intrusive_thread.join(timeout=5)
                if self.intrusive_thread.is_alive():
                    self.logger.warning("警告：侵入性思维线程超时后仍未结束。")
                else:
                    self.logger.info("侵入性思维线程已成功结束。")
            if self.db_handler and hasattr(self.db_handler, "close") and callable(self.db_handler.close):
                self.logger.info("正在关闭 ArangoDBHandler...")
                await self.db_handler.close()
            self.logger.info(
                f"程序清理完成。{(self.root_cfg.persona.bot_name if self.root_cfg else 'Bot')} 的意识已停止流动。"
            )


async def start_consciousness_flow() -> None:
    core_logic_instance = CoreLogic()
    await core_logic_instance.start()
