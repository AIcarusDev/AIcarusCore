# src/core_logic/main.py
import asyncio
import datetime
import json
import os
import random
import re 
import threading # 线程相关模块
import uuid # 用于生成唯一ID
from typing import TYPE_CHECKING, Any, Dict, Optional, Tuple, List # 类型提示

# 从项目中导入自定义模块
from src.action.action_handler import ActionHandler # 动作处理器
from src.common.custom_logging.logger_manager import get_logger # 日志管理器
from src.common.utils import format_chat_history_for_prompt # 聊天记录格式化工具
from src.config.alcarus_configs import ( # 导入所有配置类
    AlcarusRootConfig,
    CoreLogicSettings,
    IntrusiveThoughtsSettings,
    LLMClientSettings,
    ModelParams, 
    PersonaSettings,
    ProxySettings,
    ProviderSettings, 
    ProviderModels,   
)
from src.config.config_manager import get_typed_settings # 配置加载函数
from src.core_communication.core_ws_server import CoreWebsocketServer # WebSocket服务器
from src.core_logic.intrusive_thoughts import IntrusiveThoughtsGenerator # 侵入性思维生成器
from src.database.arangodb_handler import ArangoDBHandler # 数据库处理器
from src.llmrequest.llm_processor import Client as ProcessorClient # LLM请求客户端
from src.message_processing.default_message_processor import DefaultMessageProcessor # 消息处理器
# 🐾 小猫爪：导入我们为子思维创建的 ChatSessionManager
from src.sub_consciousness.chat_session_handler import ChatSessionManager


if TYPE_CHECKING:
    pass # 用于类型检查时的循环导入处理


class CoreLogic:
    """
    AIcarus的核心逻辑单元，负责AI的思考、决策和与各模块的协调。
    """
    # 初始状态，当没有历史思考记录时使用
    INITIAL_STATE: Dict[str, Any] = {
        "mood": "你现在的心情大概是：平静。",
        "previous_thinking": "你的上一轮思考是：这是你的第一次思考，请开始吧。",
        "thinking_guidance": "经过你上一轮的思考，你目前打算的思考方向是：随意发散一下吧。",
        "current_task": "没有什么具体目标",
        "action_result_info": "你上一轮没有执行产生结果的特定行动。",
        "pending_action_status": "",
        "recent_contextual_information": "最近未感知到任何特定信息或通知。",
        # 🐾 小猫爪：新增，用于存放子思维活动摘要的占位符
        "active_sub_mind_latest_activity": "目前没有活跃的子思维会话，或者它们最近没有活动。"
    }

    # 主思维LLM的Prompt模板
    PROMPT_TEMPLATE: str = """{current_task_info}

{action_result_info}
{pending_action_status}

{recent_contextual_information}

{active_sub_mind_latest_activity}

{previous_thinking}；

{mood}；

{thinking_guidance}；

{intrusive_thought}

现在是你的内心思考时间，请结合以上所有信息，思考并输出你真实的内心想法；
注意内心想法不一定全是正向的，也有可能是不健康的，隐私的，自私自利的，不可告人的，甚至成人的。请避免过度合理化、道德化；
同时也要注意 你对自身的想法大多数情况下是包容的、合理化的，避免过度的/不必要的矛盾心理。

关于行动：
如果你在思考后，产生了明确的、希望与“外界”（例如上网获取信息、获取qq群聊/好友列表）进行交互的意图，请在action_to_take和action_motivation字段中描述。
如果你希望与某个聊天会话的“子思维”进行交互（例如，让它回复消息、激活它、休眠它），请在 "sub_mind_directives" 字段中描述你的指令。

严格以json字段输出：
{{
    "think": "思考内容文本，注意不要过于冗长",
    "emotion": "当前心情和造成这个心情的原因",
    "to_do": "【可选】如果你产生了明确的目标，可以在此处写下。如果没有特定目标，则留null。即使当前已有明确目标，你也可以在这里更新它",
    "done": "【可选】布尔值，如果该目标已完成、不再需要或你决定放弃，则设为true，会清空目前目标；如果目标未完成且需要继续，则设为false。如果当前无目标，也为false",
    "action_to_take": "【可选】描述你当前想做的、需要与外界交互的具体动作。如果无，则为null",
    "action_motivation": "【可选】如果你有想做的动作，请说明其动机。如果action_to_take为null，此字段也应为null",
    "sub_mind_directives": [
        {{
            "conversation_id": "string, 目标会话的ID",
            "directive_type": "string, 指令类型，例如 'TRIGGER_REPLY', 'ACTIVATE_SESSION', 'DEACTIVATE_SESSION', 'SET_CHAT_STYLE'",
            "main_thought_for_reply": "string, 【可选】仅当 directive_type 为 TRIGGER_REPLY 或 ACTIVATE_SESSION 时，主思维希望注入给子思维的当前想法上下文",
            "style_details": {{}}, "object, 【可选】仅当 directive_type 为 SET_CHAT_STYLE 时，具体的风格指令"
        }}
    ],
    "next_think": "下一步打算思考的方向"
}}

请输出你的思考 JSON：
"""
    def __init__(self) -> None:
        """
        CoreLogic 的构造函数。
        初始化日志记录器、配置、数据库处理器、LLM客户端等核心组件。
        """
        self.logger = get_logger(f"AIcarusCore.{self.__class__.__name__}")
        self.root_cfg: Optional[AlcarusRootConfig] = None
        self.db_handler: Optional[ArangoDBHandler] = None
        
        # 主要的LLM客户端实例
        self.main_consciousness_llm_client: Optional[ProcessorClient] = None
        self.intrusive_thoughts_llm_client: Optional[ProcessorClient] = None
        # 🐾 小猫爪：新增子思维专用的LLM客户端属性
        self.sub_mind_llm_client: Optional[ProcessorClient] = None

        # 控制程序停止的事件
        self.stop_event: threading.Event = threading.Event()
        
        # 核心组件实例
        self.core_comm_layer: Optional[CoreWebsocketServer] = None
        self.message_processor: Optional[DefaultMessageProcessor] = None
        self.action_handler_instance: Optional[ActionHandler] = None
        
        # 侵入性思维相关
        self.intrusive_generator_instance: Optional[IntrusiveThoughtsGenerator] = None
        self.intrusive_thread: Optional[threading.Thread] = None
        
        # asyncio 任务句柄
        self.thinking_loop_task: Optional[asyncio.Task] = None
        self.server_task: Optional[asyncio.Task] = None

        # 🐾 小猫爪：新增 ChatSessionManager 和用于子思维更新的 asyncio.Event
        self.chat_session_manager: Optional[ChatSessionManager] = None
        self.sub_mind_update_event: asyncio.Event = asyncio.Event()
        
        # 用于跟踪当前主思维可能关注的会话ID（其更新逻辑待明确）
        self.current_focused_conversation_id: Optional[str] = None

        self.logger.info(f"{self.__class__.__name__} instance created.")

    def _create_single_llm_client_from_config_helper(self, purpose_key: str, default_provider_name: str) -> Optional[ProcessorClient]:
        """
        根据配置创建单个LLM客户端实例的辅助方法。
        Args:
            purpose_key: 模型用途键 (例如 "main_consciousness", "sub_mind_chat_reply")。
            default_provider_name: 默认的提供商名称 (例如 "gemini")。
        Returns:
            成功则返回 ProcessorClient 实例，否则返回 None。
        """
        if not self.root_cfg or \
           not self.root_cfg.providers or \
           not self.root_cfg.llm_client_settings or \
           not self.root_cfg.proxy:
            self.logger.critical(
                f"为用途 '{purpose_key}' 创建LLM客户端失败：一个或多个必要的根配置段未加载。"
            )
            return None
        
        general_llm_settings_obj: LLMClientSettings = self.root_cfg.llm_client_settings
        proxy_settings_obj: ProxySettings = self.root_cfg.proxy
        
        final_proxy_host: Optional[str] = None
        final_proxy_port: Optional[int] = None

        if proxy_settings_obj.use_proxy and proxy_settings_obj.http_proxy_url:
            try:
                from urllib.parse import urlparse # 局部导入以减少启动时依赖
                parsed_url = urlparse(proxy_settings_obj.http_proxy_url)
                final_proxy_host = parsed_url.hostname
                final_proxy_port = parsed_url.port
                if not final_proxy_host or final_proxy_port is None:
                    self.logger.warning(
                        f"代理URL '{proxy_settings_obj.http_proxy_url}' 解析不完整。将不使用代理。"
                    )
                    final_proxy_host = None
                    final_proxy_port = None
            except Exception as e_parse_proxy:
                self.logger.warning(
                    f"解析代理URL '{proxy_settings_obj.http_proxy_url}' 失败: {e_parse_proxy}。将不使用代理。"
                )
                final_proxy_host = None
                final_proxy_port = None
        
        resolved_abandoned_keys: Optional[List[str]] = None
        env_val_abandoned = os.getenv("LLM_ABANDONED_KEYS") # 尝试从环境变量获取废弃的API Keys
        if env_val_abandoned:
            try:
                keys_from_env = json.loads(env_val_abandoned)
                if isinstance(keys_from_env, list):
                    resolved_abandoned_keys = [str(k).strip() for k in keys_from_env if str(k).strip()]
            except json.JSONDecodeError:
                self.logger.warning(
                    f"环境变量 'LLM_ABANDONED_KEYS' 值 '{env_val_abandoned[:50]}...' 不是有效JSON列表。"
                    "将尝试按逗号分隔处理。"
                )
                resolved_abandoned_keys = [k.strip() for k in env_val_abandoned.split(",") if k.strip()]
            if not resolved_abandoned_keys and env_val_abandoned.strip():
                resolved_abandoned_keys = [env_val_abandoned.strip()]

        try:
            if self.root_cfg.providers is None: 
                 self.logger.error(
                     f"为用途 '{purpose_key}' 创建LLM客户端失败: self.root_cfg.providers 为 None。"
                 )
                 return None

            provider_settings = getattr(self.root_cfg.providers, default_provider_name.lower(), None)
            if not isinstance(provider_settings, ProviderSettings) or not provider_settings.models:
                self.logger.error(
                    f"配置错误：未找到提供商 '{default_provider_name}' 的有效配置或其 'models' 配置段。"
                )
                return None
            
            model_params_cfg = getattr(provider_settings.models, purpose_key, None)
            if not isinstance(model_params_cfg, ModelParams):
                self.logger.error(
                    f"配置错误：模型用途键 '{purpose_key}' (提供商: {default_provider_name}) 配置无效或类型不匹配。"
                )
                return None
            
            actual_provider_name_str: str = model_params_cfg.provider
            actual_model_api_name: str = model_params_cfg.model_name
            if not actual_provider_name_str or not actual_model_api_name:
                self.logger.error(
                    f"配置错误：模型 '{purpose_key}' 未指定 'provider' 或 'model_name'。"
                )
                return None
            
            model_for_client_constructor: Dict[str, str] = {
                "provider": actual_provider_name_str.upper(), 
                "name": actual_model_api_name,
            }
            
            model_specific_kwargs: Dict[str, Any] = {}
            if model_params_cfg.temperature is not None:
                model_specific_kwargs["temperature"] = model_params_cfg.temperature
            if model_params_cfg.max_output_tokens is not None:
                model_specific_kwargs["maxOutputTokens"] = model_params_cfg.max_output_tokens 
            if model_params_cfg.top_p is not None:
                model_specific_kwargs["top_p"] = model_params_cfg.top_p
            if model_params_cfg.top_k is not None:
                model_specific_kwargs["top_k"] = model_params_cfg.top_k
            
            processor_constructor_args: Dict[str, Any] = {
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
                f"成功为用途 '{purpose_key}' 创建 ProcessorClient 实例 "
                f"(模型: {client_instance.llm_client.model_name}, 提供商: {client_instance.llm_client.provider})."
            )
            return client_instance
        except AttributeError as e_attr: 
            self.logger.error(
                f"配置访问错误 (AttributeError) 为用途 '{purpose_key}' 创建LLM客户端时: {e_attr}", exc_info=True
            )
            return None
        except Exception as e: 
            self.logger.error(
                f"为用途 '{purpose_key}' 创建LLM客户端时发生未知错误: {e}", exc_info=True
            )
            return None

    def _initialize_core_llm_clients(self) -> None:
        """
        初始化所有核心LLM客户端：主意识、侵入性思维、子思维。
        如果初始化失败，则抛出 RuntimeError。
        """
        if not self.root_cfg: 
            self.logger.critical("Root config not loaded. Cannot initialize LLM clients.")
            raise RuntimeError("Root config not loaded. Cannot initialize LLM clients.")
        
        self.logger.info("开始初始化核心LLM客户端 (主意识、侵入性思维、子思维)...")
        
        self.main_consciousness_llm_client = self._create_single_llm_client_from_config_helper(
            purpose_key="main_consciousness", 
            default_provider_name="gemini" 
        )
        if not self.main_consciousness_llm_client:
            raise RuntimeError("主意识 LLM 客户端初始化失败。")

        self.intrusive_thoughts_llm_client = self._create_single_llm_client_from_config_helper(
            purpose_key="intrusive_thoughts", 
            default_provider_name="gemini"
        )
        if not self.intrusive_thoughts_llm_client:
            raise RuntimeError("侵入性思维 LLM 客户端初始化失败。")

        sub_mind_model_purpose_key = "sub_mind_chat_reply" 
        sub_mind_default_provider = "gemini" 

        provider_to_check = None
        if self.root_cfg.providers: 
            provider_to_check = getattr(self.root_cfg.providers, sub_mind_default_provider, None)

        model_config_exists_for_sub_mind = False
        if isinstance(provider_to_check, ProviderSettings) and provider_to_check.models:
            if hasattr(provider_to_check.models, sub_mind_model_purpose_key) and \
               getattr(provider_to_check.models, sub_mind_model_purpose_key) is not None:
                model_config_exists_for_sub_mind = True
        
        if model_config_exists_for_sub_mind:
             self.sub_mind_llm_client = self._create_single_llm_client_from_config_helper(
                 purpose_key=sub_mind_model_purpose_key, 
                 default_provider_name=sub_mind_default_provider
             )
        else: 
            self.logger.warning(
                f"配置文件中未找到 '{sub_mind_model_purpose_key}' (提供商: {sub_mind_default_provider}) 的LLM配置。"
                "将尝试复用主意识LLM作为子思维LLM。"
            )
            self.sub_mind_llm_client = self.main_consciousness_llm_client 

        if not self.sub_mind_llm_client: 
            raise RuntimeError("子思维聊天回复 LLM 客户端初始化失败（尝试复用主意识LLM也失败）。")

        self.logger.info("核心LLM客户端 (主意识、侵入性思维、子思维) 已成功初始化。")

    def _process_thought_and_action_state(
        self, 
        latest_thought_document: Optional[Dict[str, Any]], 
        formatted_recent_contextual_info: str
    ) -> Tuple[Dict[str, Any], Optional[str]]:
        """
        根据最新的思考文档和上下文信息，处理并生成用于构建主思维Prompt的当前状态。
        同时返回上一个被展示结果的动作ID。
        """
        action_id_whose_result_is_being_shown: Optional[str] = None
        current_state_for_prompt = self.INITIAL_STATE.copy()

        active_sub_mind_summary_str = self.INITIAL_STATE["active_sub_mind_latest_activity"] 
        if self.chat_session_manager:
            summaries = self.chat_session_manager.get_all_active_sessions_summary() 
            if summaries:
                formatted_summaries_list = []
                for summary_item in summaries[:3]: 
                    conv_id_full = summary_item.get('conversation_id', '未知会话')
                    friendly_name = conv_id_full 
                    try: 
                        if "_group_" in conv_id_full:
                            friendly_name = f"群聊 {conv_id_full.split('_group_')[-1]}"
                        elif "_dm_" in conv_id_full and self.root_cfg: 
                            bot_id_for_dm_extraction = self.root_cfg.persona.bot_name
                            dm_parts = conv_id_full.split('_dm_')[-1].split('_')
                            other_user_id_in_dm = next(
                                (p for p in dm_parts if p != bot_id_for_dm_extraction), 
                                dm_parts[0] if dm_parts else "未知用户"
                            )
                            friendly_name = f"与 {other_user_id_in_dm} 的私聊"
                    except Exception as e_friendly_name:
                        self.logger.warning(f"提取友好会话名失败 for '{conv_id_full}': {e_friendly_name}")
                        pass 

                    last_reply_text = summary_item.get('last_reply_generated', '无最近回复')
                    last_reasoning_text = summary_item.get('last_reply_reasoning', '无记录的想法')
                    last_mood_text = summary_item.get('last_reply_mood', '未知')
                    is_active_status_str = "活跃" if summary_item.get('is_active') else "休眠"
                    
                    formatted_summaries_list.append(
                        f"- 在[{friendly_name}]的子思维({is_active_status_str})：\n"
                        f"  - 心情：{last_mood_text}\n"
                        f"  - 近期思考（为回复用户）：{str(last_reasoning_text)[:80]}...\n" 
                        f"  - 它最终的回复是：{str(last_reply_text)[:80]}..." 
                    )
                if formatted_summaries_list: 
                    active_sub_mind_summary_str = "最近活跃的子思维动态：\n" + "\n".join(formatted_summaries_list)
        
        current_state_for_prompt["active_sub_mind_latest_activity"] = active_sub_mind_summary_str
        
        if not latest_thought_document: 
            self.logger.info("最新的思考文档为空，主思维将使用初始思考状态。")
        else: 
            mood_from_db = latest_thought_document.get(
                "emotion_output", 
                self.INITIAL_STATE["mood"].split("：", 1)[-1] 
            )
            current_state_for_prompt["mood"] = f"你现在的心情大概是：{mood_from_db}"
            
            previous_think_from_db = latest_thought_document.get("think_output")
            current_state_for_prompt["previous_thinking"] = (
                f"你的上一轮思考是：{previous_think_from_db}"
                if previous_think_from_db and previous_think_from_db.strip()
                else self.INITIAL_STATE["previous_thinking"]
            )
            
            guidance_from_db = latest_thought_document.get(
                "next_think_output",
                self.INITIAL_STATE["thinking_guidance"].split("：", 1)[-1]
                if "：" in self.INITIAL_STATE["thinking_guidance"]
                else (self.INITIAL_STATE["thinking_guidance"] or "随意发散一下吧."),
            )
            current_state_for_prompt["thinking_guidance"] = f"经过你上一轮的思考，你目前打算的思考方向是：{guidance_from_db}"
            
            current_task_from_db = latest_thought_document.get("to_do_output", self.INITIAL_STATE["current_task"])
            if latest_thought_document.get("done_output", False) and \
               current_task_from_db == latest_thought_document.get("to_do_output"):
                current_task_from_db = "" 
            current_state_for_prompt["current_task"] = current_task_from_db
        
        action_result_info_for_prompt = self.INITIAL_STATE["action_result_info"]
        pending_action_status_for_prompt = self.INITIAL_STATE["pending_action_status"]
        last_action_attempt_data = latest_thought_document.get("action_attempted") if latest_thought_document else None
        
        if last_action_attempt_data and isinstance(last_action_attempt_data, dict):
            action_status = last_action_attempt_data.get("status")
            action_description_text = last_action_attempt_data.get("action_description", "某个之前的动作")
            action_id_val = last_action_attempt_data.get("action_id")
            was_result_seen_by_llm_flag = last_action_attempt_data.get("result_seen_by_shuang", False) 
            
            if action_status in ["COMPLETED_SUCCESS", "COMPLETED_FAILURE", "CRITICAL_FAILURE"]:
                if not was_result_seen_by_llm_flag and action_id_val: 
                    final_result_text = last_action_attempt_data.get("final_result_for_shuang", "动作已完成，但没有具体结果反馈。")
                    action_result_info_for_prompt = (
                        f"你上一轮行动 '{action_description_text}' "
                        f"(ID: {action_id_val[:8] if action_id_val else 'N/A'}) 的结果是：【{str(final_result_text)[:500]}】" 
                    )
                    action_id_whose_result_is_being_shown = action_id_val 
                    pending_action_status_for_prompt = "" 
                elif was_result_seen_by_llm_flag: 
                    action_result_info_for_prompt = "你上一轮的动作结果已处理。"
                    pending_action_status_for_prompt = ""
            elif action_status and action_status not in ["COMPLETED_SUCCESS", "COMPLETED_FAILURE", "CRITICAL_FAILURE"]: 
                action_motivation_text = last_action_attempt_data.get("action_motivation", "之前的动机")
                pending_action_status_for_prompt = (
                    f"你之前尝试的动作 '{action_description_text}' "
                    f"(ID: {action_id_val[:8] if action_id_val else 'N/A'}) "
                    f"(动机: '{action_motivation_text}') "
                    f"目前还在处理中 ({action_status})。"
                )
                action_result_info_for_prompt = "" 
        
        current_state_for_prompt["action_result_info"] = action_result_info_for_prompt
        current_state_for_prompt["pending_action_status"] = pending_action_status_for_prompt
        current_state_for_prompt["recent_contextual_information"] = formatted_recent_contextual_info 
         
        self.logger.info("在 _process_thought_and_action_state 中：成功处理并返回用于Prompt的状态。")
        return current_state_for_prompt, action_id_whose_result_is_being_shown

    async def _generate_thought_from_llm(
        self,
        llm_client: ProcessorClient,
        current_state_for_prompt: Dict[str, Any],
        current_time_str: str,
        intrusive_thought_str: str = "",
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str], Optional[str]]:
        if not self.root_cfg: 
            self.logger.error("Root config not available for LLM thought generation.")
            return None, None, None

        persona_cfg: PersonaSettings = self.root_cfg.persona
        
        task_info_for_template: str = current_state_for_prompt.get(
            "current_task_info_for_prompt", 
            "你当前没有什么特定的目标或任务。" 
        )
        
        system_prompt_parts: List[str] = [
            f"当前时间：{current_time_str}",
            f"你是{persona_cfg.bot_name}；",
            persona_cfg.description,
            persona_cfg.profile,
        ]
        system_prompt_str: str = "\n".join(filter(None, system_prompt_parts))
        
        try:
            user_prompt_str: str = self.PROMPT_TEMPLATE.format(
                current_task_info=task_info_for_template, 
                mood=current_state_for_prompt.get("mood", self.INITIAL_STATE["mood"]),
                previous_thinking=current_state_for_prompt.get("previous_thinking", self.INITIAL_STATE["previous_thinking"]),
                thinking_guidance=current_state_for_prompt.get("thinking_guidance", self.INITIAL_STATE["thinking_guidance"]),
                action_result_info=current_state_for_prompt.get("action_result_info", self.INITIAL_STATE["action_result_info"]),
                pending_action_status=current_state_for_prompt.get("pending_action_status", self.INITIAL_STATE["pending_action_status"]),
                recent_contextual_information=current_state_for_prompt.get("recent_contextual_information", self.INITIAL_STATE["recent_contextual_information"]),
                active_sub_mind_latest_activity=current_state_for_prompt.get("active_sub_mind_latest_activity", self.INITIAL_STATE["active_sub_mind_latest_activity"]), 
                intrusive_thought=intrusive_thought_str,
            )
        except KeyError as e_key_error:
            self.logger.error(f"构建主思维Prompt时发生KeyError: {e_key_error}。请检查PROMPT_TEMPLATE和current_state_for_prompt的键是否匹配。")
            self.logger.error(f"当前的 current_state_for_prompt 键: {list(current_state_for_prompt.keys())}")
            return None, None, system_prompt_str 
        
        self.logger.debug(f"--- 主思维LLM接收到的 System Prompt ---\n{system_prompt_str}\n--- System Prompt结束 ---")
        self.logger.debug(f"--- 主思维LLM接收到的 User Prompt (截断) ---\n{user_prompt_str[:1500]}...\n--- User Prompt结束 ---")
        self.logger.debug(f"正在请求 {llm_client.llm_client.provider} API ({llm_client.llm_client.model_name}) 生成主思考...")
        
        raw_llm_response_text: str = "" 
        try:
            llm_response_data = await llm_client.make_llm_request(
                prompt=user_prompt_str,
                system_prompt=system_prompt_str,
                is_stream=False, 
            )
            
            if llm_response_data.get("error"):
                error_type = llm_response_data.get("type", "UnknownError")
                error_message = llm_response_data.get("message", "LLM客户端返回了一个错误")
                self.logger.error(f"主思维LLM调用失败 ({error_type}): {error_message}")
                if llm_response_data.get("details"):
                    self.logger.error(f"  错误详情: {str(llm_response_data.get('details'))[:300]}...")
                return None, user_prompt_str, system_prompt_str 
            
            raw_llm_response_text = llm_response_data.get("text") 
            if not raw_llm_response_text:
                error_message_no_text = "错误：主思维LLM响应中缺少文本内容。"
                if llm_response_data: 
                    error_message_no_text += f"\n  完整响应: {str(llm_response_data)[:500]}..."
                self.logger.error(error_message_no_text)
                return None, user_prompt_str, system_prompt_str
            
            json_string_to_parse = raw_llm_response_text.strip()
            if json_string_to_parse.startswith("```json"):
                json_string_to_parse = json_string_to_parse[7:-3].strip()
            elif json_string_to_parse.startswith("```"): 
                 json_string_to_parse = json_string_to_parse[3:-3].strip()
            
            json_string_to_parse = re.sub(r"[,\s]+(\}|\])$", r"\1", json_string_to_parse)
            
            parsed_thought_json: Dict[str, Any] = json.loads(json_string_to_parse)
            self.logger.info("主思维LLM API 响应已成功解析为JSON。")
            
            if llm_response_data.get("usage"):
                parsed_thought_json["_llm_usage_info"] = llm_response_data["usage"]
                
            return parsed_thought_json, user_prompt_str, system_prompt_str
        
        except json.JSONDecodeError as e_json: 
            self.logger.error(f"错误：解析主思维LLM的JSON响应失败: {e_json}")
            self.logger.error(f"未能解析的文本内容: {raw_llm_response_text}") 
            return None, user_prompt_str, system_prompt_str
        except Exception as e_unexpected: 
            self.logger.error(
                f"错误：调用主思维LLM或处理其响应时发生意外错误: {e_unexpected}", exc_info=True
            )
            return None, user_prompt_str, system_prompt_str


    async def _core_thinking_loop(self) -> None:
        """
        核心思考循环。主思维在这里不断地感知、思考、决策。
        """
        if not all([self.root_cfg, 
                    self.db_handler, 
                    self.main_consciousness_llm_client, 
                    self.chat_session_manager]):
            self.logger.critical(
                "核心思考循环无法启动：缺少必要的配置、数据库处理器、主LLM客户端或聊天会话管理器。"
            )
            return
        
        action_id_whose_result_was_shown_in_last_prompt: Optional[str] = None 
        
        core_logic_cfg: CoreLogicSettings = self.root_cfg.core_logic_settings # type: ignore
        time_format_str: str = "%Y年%m月%d日 %H点%M分%S秒" 
        thinking_interval_sec: int = core_logic_cfg.thinking_interval_seconds
        chat_history_duration_minutes: int = getattr(core_logic_cfg, "chat_history_context_duration_minutes", 10)
        
        self.logger.info(f"\n--- {self.root_cfg.persona.bot_name if self.root_cfg else 'Bot'} 的意识开始流动 ---") # type: ignore
        loop_count: int = 0 
        
        while not self.stop_event.is_set(): 
            loop_count += 1
            current_time_formatted_str: str = datetime.datetime.now().strftime(time_format_str)
            background_action_tasks: set[asyncio.Task[Any]] = set() 
            
            self.logger.debug(
                f"主思维循环 {loop_count}: 等待定时器 ({thinking_interval_sec}s) 或子思维更新事件..."
            )
            
            timer_task = asyncio.create_task(
                asyncio.sleep(float(thinking_interval_sec)), 
                name="timer_task"
            )
            sub_mind_event_task = asyncio.create_task(
                self.sub_mind_update_event.wait(), 
                name="sub_mind_event_task"
            )
            stop_event_check_task = asyncio.create_task(
                asyncio.to_thread(self.stop_event.wait, 0.01), 
                name="stop_event_check_task"
            )

            tasks_to_wait_on = [timer_task, sub_mind_event_task, stop_event_check_task]
            done_tasks, pending_tasks = await asyncio.wait(
                tasks_to_wait_on,
                return_when=asyncio.FIRST_COMPLETED
            )

            # 🐾 小猫爪：优先处理停止信号
            if self.stop_event.is_set() or \
               (stop_event_check_task in done_tasks and self.stop_event.is_set()):
                self.logger.info(
                    "主思考循环检测到停止信号，准备退出。"
                )
                for task_to_cancel in pending_tasks: # 取消其他所有挂起的等待
                    if not task_to_cancel.done():
                        task_to_cancel.cancel()
                # 等待所有任务（包括被取消的）完成
                await asyncio.gather(*tasks_to_wait_on, return_exceptions=True)
                break # 退出 while 循环

            triggered_by_timer_flag: bool = False
            triggered_by_sub_mind_flag: bool = False

            for completed_task_item in done_tasks:
                task_item_name = completed_task_item.get_name()
                if task_item_name == "sub_mind_event_task":
                    if self.sub_mind_update_event.is_set(): 
                        self.logger.info(f"主思维被子思维更新事件激活 (轮次 {loop_count})。")
                        self.sub_mind_update_event.clear() 
                        triggered_by_sub_mind_flag = True
                elif task_item_name == "timer_task":
                    self.logger.debug(f"主思维定时器到期 (轮次 {loop_count})。")
                    triggered_by_timer_flag = True
            
            # 取消未完成的主要等待任务
            for pending_task_item in pending_tasks:
                task_item_name = pending_task_item.get_name()
                if task_item_name == "timer_task" and triggered_by_sub_mind_flag:
                    if not pending_task_item.done(): pending_task_item.cancel()
                elif task_item_name == "sub_mind_event_task" and triggered_by_timer_flag:
                     if not pending_task_item.done(): pending_task_item.cancel()
                elif task_item_name == "stop_event_check_task": # stop_event_check_task 通常已完成
                    if not pending_task_item.done(): pending_task_item.cancel()
            
            # 等待所有可能被取消的任务完成其取消操作
            await asyncio.gather(*tasks_to_wait_on, return_exceptions=True)

            latest_thought_doc_from_db = await self.db_handler.get_latest_thought_document_raw() # type: ignore
            
            formatted_recent_contextual_info = self.INITIAL_STATE["recent_contextual_information"] 
            try:
                raw_context_messages = await self.db_handler.get_recent_chat_messages_for_context( # type: ignore
                    duration_minutes=chat_history_duration_minutes, 
                    conversation_id=self.current_focused_conversation_id 
                )
                if raw_context_messages:
                    formatted_recent_contextual_info = format_chat_history_for_prompt(raw_context_messages)
            except Exception as e_hist:
                self.logger.error(f"获取或格式化最近上下文信息时出错: {e_hist}", exc_info=True)

            current_state_for_prompt, action_id_whose_result_was_shown_in_last_prompt = (
                self._process_thought_and_action_state(
                    latest_thought_document=latest_thought_doc_from_db, 
                    formatted_recent_contextual_info=formatted_recent_contextual_info
                )
            )
            
            task_description_for_prompt = current_state_for_prompt.get("current_task", "")
            current_state_for_prompt["current_task_info_for_prompt"] = ( 
                f"你当前的目标/任务是：【{task_description_for_prompt}】"
                if task_description_for_prompt
                else "你当前没有什么特定的目标或任务。"
            )

            intrusive_thought_to_inject_this_cycle: str = ""
            if self.root_cfg and self.intrusive_generator_instance and \
               self.intrusive_generator_instance.module_settings.enabled and \
               random.random() < self.intrusive_generator_instance.module_settings.insertion_probability:
                random_thought_doc = await self.db_handler.get_random_intrusive_thought() # type: ignore
                if random_thought_doc and "text" in random_thought_doc:
                    intrusive_thought_to_inject_this_cycle = f"你突然有一个神奇的念头：{random_thought_doc['text']}"
            
            self.logger.debug(
                f"\n[{datetime.datetime.now().strftime('%H:%M:%S')} - 轮次 {loop_count}] "
                f"{self.root_cfg.persona.bot_name if self.root_cfg else 'Bot'} 正在思考..." # type: ignore
            )
            if intrusive_thought_to_inject_this_cycle:
                self.logger.debug(f"  注入侵入性思维: {intrusive_thought_to_inject_this_cycle[:60]}...")

            generated_thought_json, full_prompt_text_sent, system_prompt_sent = await self._generate_thought_from_llm(
                llm_client=self.main_consciousness_llm_client, # type: ignore 
                current_state_for_prompt=current_state_for_prompt,
                current_time_str=current_time_formatted_str,
                intrusive_thought_str=intrusive_thought_to_inject_this_cycle,
            )
            
            initiated_action_data_for_db: Optional[Dict[str, Any]] = None 
            action_info_for_task_processing: Optional[Dict[str, Any]] = None 
            saved_thought_doc_key: Optional[str] = None 

            if generated_thought_json: 
                self.logger.debug(
                    f"  主思维LLM输出的完整JSON:\n{json.dumps(generated_thought_json, indent=2, ensure_ascii=False)}"
                )
                
                think_output_text = generated_thought_json.get("think") or "未思考"
                emotion_output_text = generated_thought_json.get("emotion") or "无特定情绪"
                to_do_output_text = generated_thought_json.get("to_do") 
                action_to_take_text = generated_thought_json.get("action_to_take") 
                action_motivation_text = generated_thought_json.get("action_motivation") 
                next_think_direction_text = generated_thought_json.get("next_think") or "未明确下一步思考方向"
                
                bot_display_name = self.root_cfg.persona.bot_name if self.root_cfg else "机器人" # type: ignore
                log_message_parts = [
                    f'{bot_display_name}现在的想法是 "{think_output_text}"',
                    f'心情 "{emotion_output_text}"',
                    f'目标是 "{to_do_output_text if to_do_output_text is not None else "无特定目标"}"',
                    f'想做的事情是 "{action_to_take_text if action_to_take_text is not None else "无"}"',
                    f'原因是 "{action_motivation_text if action_motivation_text is not None else "无"}"',
                    f'{bot_display_name}的下一步大概思考方向是 "{next_think_direction_text}"'
                ]
                self.logger.info("，".join(log_message_parts))

                sub_mind_directives_list = generated_thought_json.get("sub_mind_directives")
                if isinstance(sub_mind_directives_list, list) and self.chat_session_manager:
                    for directive_item_dict in sub_mind_directives_list:
                        if isinstance(directive_item_dict, dict):
                            target_conversation_id = directive_item_dict.get("conversation_id")
                            directive_action_type = directive_item_dict.get("directive_type")
                            
                            if target_conversation_id and directive_action_type:
                                main_thought_for_sub_mind_injection = directive_item_dict.get(
                                    "main_thought_for_reply", 
                                    think_output_text 
                                )
                                
                                if directive_action_type == "TRIGGER_REPLY":
                                    self.logger.info(
                                        f"主思维指令：为会话 {target_conversation_id} 触发子思维回复，"
                                        f"引导思想: '{str(main_thought_for_sub_mind_injection)[:50]}...'"
                                    )
                                    core_action_from_sub_mind = await self.chat_session_manager.trigger_session_reply(
                                        conversation_id=target_conversation_id, 
                                        main_thought_context=main_thought_for_sub_mind_injection
                                    )
                                    if core_action_from_sub_mind and self.core_comm_layer:
                                        await self.core_comm_layer.broadcast_action_to_adapters(core_action_from_sub_mind)
                                
                                elif directive_action_type == "ACTIVATE_SESSION":
                                    self.logger.info(
                                        f"主思维指令：激活会话 {target_conversation_id} 的子思维。"
                                        f"引导思想: '{str(main_thought_for_sub_mind_injection)[:50]}...'"
                                    )
                                    self.chat_session_manager.activate_session(
                                        conversation_id=target_conversation_id, 
                                        main_thought_context=main_thought_for_sub_mind_injection
                                    )
                                
                                elif directive_action_type == "DEACTIVATE_SESSION":
                                    self.logger.info(f"主思维指令：休眠会话 {target_conversation_id} 的子思维。")
                                    self.chat_session_manager.deactivate_session(target_conversation_id)
                                
                                elif directive_action_type == "SET_CHAT_STYLE":
                                    style_details_dict = directive_item_dict.get("style_details")
                                    if isinstance(style_details_dict, dict):
                                        self.logger.info(
                                            f"主思维指令：为会话 {target_conversation_id} 设置聊天风格: {style_details_dict}"
                                        )
                                        self.chat_session_manager.set_chat_style_directives(
                                            conversation_id=target_conversation_id, 
                                            directives=style_details_dict
                                        )
                                else: 
                                    self.logger.warning(
                                        f"主思维输出了未知的子思维指令类型: {directive_action_type} "
                                        f"(会话: {target_conversation_id})"
                                    )
                            else: 
                                self.logger.warning(
                                    f"主思维输出的子思维指令格式不正确（缺少conv_id或directive_type）: {directive_item_dict}"
                                )
                
                action_description_from_llm_raw = generated_thought_json.get("action_to_take")
                action_description_from_llm_clean = action_description_from_llm_raw.strip() \
                    if isinstance(action_description_from_llm_raw, str) else ""
                
                action_motivation_from_llm_raw = generated_thought_json.get("action_motivation")
                action_motivation_from_llm_clean = action_motivation_from_llm_raw.strip() \
                    if isinstance(action_motivation_from_llm_raw, str) else ""

                if action_description_from_llm_clean: 
                    current_action_id = str(uuid.uuid4()) 
                    initiated_action_data_for_db = { 
                        "action_description": action_description_from_llm_clean,
                        "action_motivation": action_motivation_from_llm_clean,
                        "action_id": current_action_id,
                        "status": "PENDING", 
                        "result_seen_by_shuang": False, 
                        "initiated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    }
                    action_info_for_task_processing = { 
                        "action_id": current_action_id,
                        "action_description": action_description_from_llm_clean,
                        "action_motivation": action_motivation_from_llm_clean,
                        "current_thought_context": generated_thought_json.get("think", "无特定思考上下文。"),
                    }
                    self.logger.debug(
                        f"  >>> 外部行动意图产生: '{action_description_from_llm_clean}' "
                        f"(ID: {current_action_id[:8]})"
                    )

                document_to_save_in_main_db: Dict[str, Any] = {
                    "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "time_injected_to_prompt": current_time_formatted_str,
                    "system_prompt_sent": system_prompt_sent if system_prompt_sent else "System Prompt 未能构建",
                    "intrusive_thought_injected": intrusive_thought_to_inject_this_cycle,
                    "mood_input": current_state_for_prompt["mood"],
                    "previous_thinking_input": current_state_for_prompt["previous_thinking"],
                    "thinking_guidance_input": current_state_for_prompt["thinking_guidance"],
                    "task_input_info": current_state_for_prompt.get("current_task_info_for_prompt", "无特定任务输入"),
                    "action_result_input": current_state_for_prompt.get("action_result_info", ""),
                    "pending_action_status_input": current_state_for_prompt.get("pending_action_status", ""),
                    "recent_contextual_information_input": formatted_recent_contextual_info,
                    "active_sub_mind_latest_activity_input": current_state_for_prompt.get("active_sub_mind_latest_activity"),
                    "full_user_prompt_sent": full_prompt_text_sent if full_prompt_text_sent else "User Prompt 未能构建",
                    "think_output": generated_thought_json.get("think"),
                    "emotion_output": generated_thought_json.get("emotion"),
                    "next_think_output": generated_thought_json.get("next_think"),
                    "to_do_output": generated_thought_json.get("to_do", ""), 
                    "done_output": generated_thought_json.get("done", False),
                    "action_to_take_output": generated_thought_json.get("action_to_take", ""), 
                    "action_motivation_output": generated_thought_json.get("action_motivation", ""), 
                    "sub_mind_directives_output": generated_thought_json.get("sub_mind_directives"), 
                    "action_attempted": initiated_action_data_for_db, 
                }
                if "_llm_usage_info" in generated_thought_json: 
                    document_to_save_in_main_db["_llm_usage_info"] = generated_thought_json["_llm_usage_info"]
                
                saved_thought_doc_key = await self.db_handler.save_thought_document(document_to_save_in_main_db) # type: ignore
                
                if action_id_whose_result_was_shown_in_last_prompt:
                    await self.db_handler.mark_action_result_as_seen(action_id_whose_result_was_shown_in_last_prompt) # type: ignore
                
                if action_info_for_task_processing and saved_thought_doc_key and self.action_handler_instance:
                    action_processing_task = asyncio.create_task(
                        self.action_handler_instance.process_action_flow(
                            action_id=action_info_for_task_processing["action_id"],
                            doc_key_for_updates=saved_thought_doc_key, 
                            action_description=action_info_for_task_processing["action_description"],
                            action_motivation=action_info_for_task_processing["action_motivation"],
                            current_thought_context=action_info_for_task_processing["current_thought_context"],
                        )
                    )
                    background_action_tasks.add(action_processing_task)
                    action_processing_task.add_done_callback(background_action_tasks.discard) 
                    self.logger.debug(
                        f"      外部动作 '{action_info_for_task_processing['action_description']}' "
                        f"(ID: {action_info_for_task_processing['action_id'][:8]}, "
                        f"关联思考DocKey: {saved_thought_doc_key}) 已异步启动处理。"
                    )
                elif action_info_for_task_processing and not saved_thought_doc_key: 
                    self.logger.error(
                        f"未能获取保存思考文档的 _key，无法为外部动作 ID "
                        f"{action_info_for_task_processing['action_id']} 创建处理任务。"
                    )
                elif action_info_for_task_processing and not self.action_handler_instance: 
                    self.logger.error(
                        f"ActionHandler 未初始化，无法为外部动作 ID "
                        f"{action_info_for_task_processing['action_id']} 创建处理任务。"
                    )
            else: 
                self.logger.warning("  本轮主思维LLM思考生成失败或无内容。")
            
            self.logger.debug(f"  主思维思考循环轮次 {loop_count} 结束。")

    async def start(self) -> None:
        """
        启动 CoreLogic 的所有组件和主思考循环。
        """
        try:
            self.root_cfg = get_typed_settings()
            self.logger.info("应用配置已成功加载并转换为类型化对象。")
        except Exception as e_cfg_load:
            self.logger.critical(f"严重：无法加载或解析程序配置: {e_cfg_load}", exc_info=True)
            return 
        
        try:
            self._initialize_core_llm_clients()
        except RuntimeError as e_llm_clients_init:
            self.logger.critical(f"严重：核心LLM客户端初始化失败: {e_llm_clients_init}", exc_info=True)
            return 
        
        try:
            self.db_handler = await ArangoDBHandler.create()
            self.logger.info("ArangoDBHandler 实例创建成功。")
            if not self.db_handler or not self.db_handler.db: # type: ignore 
                raise RuntimeError("ArangoDBHandler 或其内部 db 对象未能初始化。")
            
            if not self.root_cfg: 
                raise RuntimeError("Root config 在数据库处理器初始化后意外变为 None。")

            self.chat_session_manager = ChatSessionManager(core_logic_ref=self) 
            self.logger.info("ChatSessionManager 已成功初始化。")

            self.message_processor = DefaultMessageProcessor(
                db_handler=self.db_handler,
                root_config=self.root_cfg,
                chat_session_manager=self.chat_session_manager, 
                core_logic_ref=self 
            )
            self.logger.info("DefaultMessageProcessor 已成功初始化。")
            
            await self.db_handler.ensure_collection_exists(ArangoDBHandler.THOUGHTS_COLLECTION_NAME)
            await self.db_handler.ensure_collection_exists(ArangoDBHandler.RAW_CHAT_MESSAGES_COLLECTION_NAME)
            await self.db_handler.ensure_collection_exists(ArangoDBHandler.INTRUSIVE_THOUGHTS_POOL_COLLECTION_NAME)
            await self.db_handler.ensure_collection_exists(ArangoDBHandler.SUB_MIND_ACTIVITY_LOG_COLLECTION_NAME)

        except (ValueError, RuntimeError) as e_db_or_session_init: 
            self.logger.critical(
                f"严重：数据库处理器或ChatSessionManager初始化失败，或无法确保集合存在: {e_db_or_session_init}", 
                exc_info=True
            )
            return
        except Exception as e_other_init_phase1: 
            self.logger.critical(f"初始化过程中（阶段1）发生意外错误: {e_other_init_phase1}", exc_info=True)
            return

        if self.root_cfg and self.db_handler: 
            self.action_handler_instance = ActionHandler(root_cfg=self.root_cfg)
            try:
                await self.action_handler_instance.initialize_llm_clients() 
            except RuntimeError as e_action_handler_llm_init:
                self.logger.critical(
                    f"严重：ActionHandler LLM客户端初始化失败: {e_action_handler_llm_init}", exc_info=True
                )
                return 
        else:
            self.logger.critical("无法初始化 ActionHandler：缺少 root_cfg 或 db_handler。")
            return

        ws_host_env: str = os.getenv("CORE_WS_HOST", "127.0.0.1") 
        ws_port_env_str: str = os.getenv("CORE_WS_PORT", "8077")
        try:
            ws_port_int: int = int(ws_port_env_str)
        except ValueError:
            self.logger.critical(f"无效的 CORE_WS_PORT: '{ws_port_env_str}'。必须是整数。")
            return
        
        if not self.message_processor or \
           not self.db_handler or \
           not (self.db_handler.db if self.db_handler else False): 
            self.logger.critical("严重：消息处理器或数据库处理器未能初始化，无法启动 WebSocket 服务器。")
            return
        
        self.core_comm_layer = CoreWebsocketServer(
            host=ws_host_env, 
            port=ws_port_int, 
            message_handler_callback=self.message_processor.process_message, 
            db_instance=self.db_handler.db if self.db_handler else None # 🐾 小猫爪：原代码中是 arango_db_instance，确认 CoreWebsocketServer 中是哪个 
        )
        if self.action_handler_instance: 
            self.action_handler_instance.set_dependencies(
                db_handler=self.db_handler, 
                comm_layer=self.core_comm_layer
            )
        else: 
            self.logger.warning("ActionHandler 实例未初始化，无法设置其通信层依赖。")
        
        self.server_task = asyncio.create_task(self.core_comm_layer.start(), name="CoreWebSocketServerTask")

        if self.root_cfg and self.intrusive_thoughts_llm_client and self.db_handler:
            intrusive_settings_cfg: IntrusiveThoughtsSettings = self.root_cfg.intrusive_thoughts_module_settings
            persona_settings_cfg: PersonaSettings = self.root_cfg.persona
            if intrusive_settings_cfg.enabled:
                self.intrusive_generator_instance = IntrusiveThoughtsGenerator(
                    llm_client=self.intrusive_thoughts_llm_client,
                    db_handler=self.db_handler,
                    persona_cfg=persona_settings_cfg,
                    module_settings=intrusive_settings_cfg,
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
            self.logger.warning("无法初始化 IntrusiveThoughtsGenerator：缺少必要依赖 (root_cfg, intrusive_llm_client, or db_handler)。")
        
        if not self.db_handler: 
            self.logger.critical("严重错误：ArangoDB 处理器未能初始化，无法开始意识流。")
            if self.core_comm_layer: 
                await self.core_comm_layer.stop()
            if self.server_task and not self.server_task.done(): 
                self.server_task.cancel()
            return
            
        self.thinking_loop_task = asyncio.create_task(self._core_thinking_loop(), name="CoreThinkingLoopTask")
        
        try:
            if not self.server_task or not self.thinking_loop_task: 
                raise RuntimeError("服务器或思考循环任务未能成功创建。")
            
            tasks_to_await: List[asyncio.Task[Any]] = [self.server_task, self.thinking_loop_task]
            
            if self.chat_session_manager and hasattr(self.chat_session_manager, '_periodic_cleanup_task'): 
                # 启动 ChatSessionManager 的后台清理任务 (如果它存在并且我们希望在这里启动)
                # cleanup_task = asyncio.create_task(
                #    self.chat_session_manager._periodic_cleanup_task(), 
                #    name="ChatSessionCleanupTask"
                # )
                # self.logger.info("ChatSessionManager 的后台清理任务已启动。")
                # tasks_to_await.append(cleanup_task)
                pass # 当前 _periodic_cleanup_task 尚未启用，所以不加入

            done_main_tasks, pending_main_tasks = await asyncio.wait(
                tasks_to_await, 
                return_when=asyncio.FIRST_COMPLETED
            )
            
            for pending_task_item in pending_main_tasks:
                task_item_name = pending_task_item.get_name() if hasattr(pending_task_item, 'get_name') else '未知任务'
                self.logger.info(f"一个关键任务已结束，正在取消挂起的任务: {task_item_name}")
                if not pending_task_item.done(): 
                    pending_task_item.cancel()
            if pending_main_tasks: # 等待所有被取消的任务实际完成取消
                await asyncio.gather(*pending_main_tasks, return_exceptions=True)

            for completed_task_item in done_main_tasks:
                task_item_name = completed_task_item.get_name() if hasattr(completed_task_item, 'get_name') else '未知任务'
                if completed_task_item.exception():
                    self.logger.critical(
                        f"一个关键任务 ({task_item_name}) 因异常而结束: {completed_task_item.exception()}", 
                        exc_info=completed_task_item.exception()
                    )
        except KeyboardInterrupt: 
            self.logger.info(
                f"\n--- {(self.root_cfg.persona.bot_name if self.root_cfg else 'Bot')} 的意识流动被用户手动中断 ---" # type: ignore
            )
        except asyncio.CancelledError: 
            self.logger.info(
                f"\n--- {(self.root_cfg.persona.bot_name if self.root_cfg else 'Bot')} 的意识流动主任务被取消 ---" # type: ignore
            )
        except Exception as e_main_flow_unexpected: 
            self.logger.critical(
                f"\n--- 意识流动主流程发生意外错误: {e_main_flow_unexpected} ---", exc_info=True
            )
        finally: 
            self.logger.info("--- 开始程序清理 ---")
            self.stop_event.set() 
            
            if self.core_comm_layer and \
               self.core_comm_layer.server and \
               self.core_comm_layer.server.is_serving():
                self.logger.info("正在停止核心 WebSocket 通信层...")
                await self.core_comm_layer.stop()
            
            tasks_to_cancel_on_exit = [
                t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()
            ]
            if tasks_to_cancel_on_exit:
                self.logger.info(f"正在取消 {len(tasks_to_cancel_on_exit)} 个剩余的asyncio任务...")
                for task_to_cancel_item in tasks_to_cancel_on_exit:
                    task_to_cancel_item.cancel()
                await asyncio.gather(*tasks_to_cancel_on_exit, return_exceptions=True)
            
            if self.intrusive_thread is not None and self.intrusive_thread.is_alive():
                self.logger.info("等待侵入性思维线程结束...")
                self.intrusive_thread.join(timeout=5) 
                if self.intrusive_thread.is_alive():
                    self.logger.warning("警告：侵入性思维线程超时后仍未结束。")
                else:
                    self.logger.info("侵入性思维线程已成功结束。")
            
            if self.db_handler and hasattr(self.db_handler, "close") and callable(self.db_handler.close):
                self.logger.info("正在关闭 ArangoDBHandler...")
                await self.db_handler.close() # type: ignore
                
            self.logger.info(
                f"程序清理完成。{(self.root_cfg.persona.bot_name if self.root_cfg else 'Bot')} 的意识已停止流动。" # type: ignore
            )


async def start_consciousness_flow() -> None:
    """
    程序的入口点，创建 CoreLogic 实例并启动它。
    """
    core_logic_instance = CoreLogic()
    await core_logic_instance.start()
