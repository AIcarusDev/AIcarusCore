# 文件: src/core_logic/consciousness_flow.py (重构后完整版)
import asyncio
import contextlib
import datetime
import json
import random
import threading
import uuid
import time
from typing import TYPE_CHECKING, Any, List, Optional, Tuple, Dict

from src.action.action_handler import ActionHandler # type: ignore
from src.common.custom_logging.logger_manager import get_logger # type: ignore
# 确保导入我们新加的 format_platform_status_summary
from src.common.utils import format_messages_for_llm_context, format_platform_status_summary # type: ignore
from src.config import config  # 直接导入配置对象
from src.core_communication.core_ws_server import CoreWebsocketServer # type: ignore
from src.core_logic.intrusive_thoughts import IntrusiveThoughtsGenerator # type: ignore
from src.llmrequest.llm_processor import Client as ProcessorClient # type: ignore
from src.database.services.event_storage_service import EventStorageService # type: ignore
from src.database.services.thought_storage_service import ThoughtStorageService # type: ignore
from aicarus_protocols import Event as ProtocolEvent, SegBuilder, ConversationInfo as ProtocolConversationInfo, ConversationType

# 导入新的小弟们，以后活都给它们干
from .state_manager import AIStateManager
from .prompt_builder import ThoughtPromptBuilder


logger = get_logger("AIcarusCore.CoreLogicFlow")

class CoreLogic:
    """
    AI的核心逻辑流，现在是个只管指挥的包工头，清爽多了。
    """
    def __init__(
        self,
        event_storage_service: EventStorageService,
        thought_storage_service: ThoughtStorageService,
        main_consciousness_llm_client: ProcessorClient,
        core_comm_layer: CoreWebsocketServer, # CoreWebsocketServer 实例
        action_handler_instance: ActionHandler,
        intrusive_generator_instance: Optional[IntrusiveThoughtsGenerator],
        stop_event: threading.Event,
        immediate_thought_trigger: asyncio.Event,
        intrusive_thoughts_llm_client: Optional[ProcessorClient] = None, 
    ) -> None:
        self.logger = logger
        self.event_storage_service = event_storage_service
        self.thought_storage_service = thought_storage_service
        self.main_consciousness_llm_client = main_consciousness_llm_client
        self.core_comm_layer = core_comm_layer # 保存 CoreWebsocketServer 实例
        self.action_handler_instance = action_handler_instance
        self.intrusive_generator_instance = intrusive_generator_instance
        self.stop_event = stop_event
        self.immediate_thought_trigger = immediate_thought_trigger          
        self.state_manager = AIStateManager(thought_storage_service)
        self.prompt_builder = ThoughtPromptBuilder()

        self.thinking_loop_task: Optional[asyncio.Task] = None
        self.logger.info(f"{self.__class__.__name__} (包工头版) 已创建，小弟们已就位！")

    async def _gather_context(self) -> Tuple[str, str, List[str]]:
        """
        专门负责从 event_storage_service 获取上下文，并整合平台状态摘要。
        """
        chat_history_duration_minutes: int = getattr(config.core_logic_settings, "chat_history_context_duration_minutes", 10)
        
        master_chat_history_str: str = "你和电脑主人之间最近没有聊天记录。"
        initial_empty_context_info: str = self.state_manager.INITIAL_STATE["recent_contextual_information"] # "无最近信息。"
        
        image_list_for_llm_from_history: List[str] = []

        # 1. 获取与主人的聊天记录 (simple style)
        try:
            master_messages = await self.event_storage_service.get_recent_chat_message_documents(
                duration_minutes=chat_history_duration_minutes,
                conversation_id="master_chat"
            )
            if master_messages:
                master_chat_history_str, _ = format_messages_for_llm_context(
                    master_messages, 
                    style='simple',
                    desired_history_span_minutes=chat_history_duration_minutes,
                    image_placeholder_key=getattr(config.core_logic_settings, "llm_image_placeholder_key", "llm_image_placeholder"),
                    image_placeholder_value=getattr(config.core_logic_settings, "llm_image_placeholder_value", "[IMAGE_HERE]")
                )
        except Exception as e:
            self.logger.error(f"获取或格式化【主人】聊天记录时出错: {e}", exc_info=True)

        # 2. 获取其他上下文信息 (包括系统事件，用于状态摘要和YAML聊天记录)
        formatted_recent_contextual_info = initial_empty_context_info # 默认值
        try:
            # 2.1 专门获取系统生命周期事件 (用于状态摘要)
            system_lifecycle_events_raw: List[Dict[str, Any]] = await self.event_storage_service.get_recent_chat_message_documents(
                duration_minutes=chat_history_duration_minutes,
                conversation_id="system_events",
                fetch_all_event_types=True # 确保获取所有类型的系统事件
            ) or []
            self.logger.debug(f"获取到 {len(system_lifecycle_events_raw)} 条用于状态摘要的系统事件。")
            # 临时的调试日志，记得事后移除哦，主人！
            if system_lifecycle_events_raw:
                self.logger.info(f"【调试】获取到的 system_lifecycle_events_raw 内容 (前3条): {json.dumps(system_lifecycle_events_raw[:3], ensure_ascii=False, indent=2)}")
            else:
                self.logger.info("【调试】system_lifecycle_events_raw 为空或None。")

            # 2.2 获取其他聊天事件 (用于YAML上下文)，需要排除 master_chat 和 system_events
            all_other_events_excluding_master: List[Dict[str, Any]] = await self.event_storage_service.get_recent_chat_message_documents(
                duration_minutes=chat_history_duration_minutes,
                exclude_conversation_id="master_chat",
                fetch_all_event_types=False # 其他聊天上下文通常只需要 message.% 类型
            ) or []
            
            other_chat_events_for_yaml_raw: List[Dict[str, Any]] = []
            if all_other_events_excluding_master:
                for event_dict in all_other_events_excluding_master:
                    conv_info = event_dict.get("conversation_info")
                    # 确保不重复包含 system_events (虽然理论上 fetch_all_event_types=False 已经过滤了非message类型)
                    if not (isinstance(conv_info, dict) and conv_info.get("conversation_id") == "system_events"):
                        other_chat_events_for_yaml_raw.append(event_dict)
            self.logger.debug(f"获取到 {len(other_chat_events_for_yaml_raw)} 条用于YAML的其他聊天事件 (已手动排除system_events)。")
            
            current_connections_info: Dict[str, Dict[str, Any]] = {}
            if hasattr(self.core_comm_layer, 'adapter_clients_info') and isinstance(self.core_comm_layer.adapter_clients_info, dict):
                 current_connections_info = self.core_comm_layer.adapter_clients_info
            else:
                self.logger.warning("CoreWebsocketServer 实例没有 adapter_clients_info 属性或其类型不正确，无法获取实时连接状态。")

            platform_status_summary_str = format_platform_status_summary(
                current_connections_info,
                system_lifecycle_events_raw, 
                status_timespan_minutes=chat_history_duration_minutes
            )

            other_chats_yaml_str = "" 
            temp_image_list: List[str] = [] 
            if other_chat_events_for_yaml_raw:
                other_chats_yaml_str, temp_image_list = format_messages_for_llm_context(
                    other_chat_events_for_yaml_raw, 
                    style='yaml',
                    image_placeholder_key=getattr(config.core_logic_settings, "llm_image_placeholder_key", "llm_image_placeholder"),
                    image_placeholder_value=getattr(config.core_logic_settings, "llm_image_placeholder_value", "[IMAGE_HERE]"),
                    desired_history_span_minutes=chat_history_duration_minutes,
                    max_messages_per_group=getattr(config.core_logic_settings, "max_messages_per_group_in_yaml", 20)
                )
                image_list_for_llm_from_history.extend(temp_image_list)
            
            final_context_parts = []
            default_status_summary_empty_msg = f"平台连接状态摘要 (基于最近{chat_history_duration_minutes}分钟及当前状态): (无活动或无近期状态变更)"
            if platform_status_summary_str and platform_status_summary_str.strip() and platform_status_summary_str != default_status_summary_empty_msg :
                final_context_parts.append(platform_status_summary_str)
            
            default_yaml_empty_msg = f"在最近{chat_history_duration_minutes}分钟内没有找到相关的聊天记录。" 
            if other_chats_yaml_str and other_chats_yaml_str.strip() and other_chats_yaml_str != default_yaml_empty_msg:
                 final_context_parts.append(other_chats_yaml_str)

            if final_context_parts:
                formatted_recent_contextual_info = "\n\n".join(final_context_parts)
            # else: formatted_recent_contextual_info 保持为 initial_empty_context_info

        except Exception as e:
            self.logger.error(f"获取或格式化【其他渠道】上下文或平台状态摘要时出错: {e}", exc_info=True)

        return master_chat_history_str, formatted_recent_contextual_info, image_list_for_llm_from_history

    async def _generate_thought_from_llm(self, system_prompt: str, user_prompt: str, image_inputs: List[str]) -> Optional[Dict[str, Any]]:
        """
        调用LLM，现在这个函数变简单了，只管调用，不用管怎么拼咒语。
        """
        try:
            response_data = await self.main_consciousness_llm_client.make_llm_request(
                prompt=user_prompt,
                system_prompt=system_prompt,
                is_stream=False,
                image_inputs=image_inputs or None,
                is_multimodal=bool(image_inputs)
            )

            if response_data.get("error"):
                self.logger.error(f"主思维LLM调用失败: {response_data.get('message', '未知错误')}")
                return None
            
            raw_text = response_data.get("text", "")
            if not raw_text:
                self.logger.error("主思维LLM响应中缺少文本内容。")
                return None
            
            parsed_json = self.prompt_builder.parse_llm_response(raw_text)
            
            if parsed_json is None:
                self.logger.error("解析LLM的JSON响应失败，它返回了None。这说明LLM没按规矩办事。")
                return None 
            
            if response_data.get("usage"):
                parsed_json["_llm_usage_info"] = response_data.get("usage")
            
            self.logger.info("主思维LLM API 的回应已成功解析为JSON。")
            return parsed_json

        except Exception as e:
            self.logger.error(f"调用LLM或解析响应时发生意外错误: {e}", exc_info=True)
            return None

    async def _process_and_store_thought(self, thought_json: Dict, prompts: Dict, context: Dict) -> Optional[str]:
        """
        处理并存储思考结果，烦人的数据整理活。
        """
        action_desc_from_llm = thought_json.get("action_to_take", "").strip()
        action_motive_from_llm = thought_json.get("action_motivation", "").strip()

        initiated_action_data_for_db = None
        if action_desc_from_llm and action_desc_from_llm.lower() != "null":
            action_id_this_cycle = str(uuid.uuid4())
            thought_json["action_id"] = action_id_this_cycle 
            initiated_action_data_for_db = {
                "action_description": action_desc_from_llm,
                "action_motivation": action_motive_from_llm,
                "action_id": action_id_this_cycle,
                "status": "PENDING",
                "result_seen_by_shuang": False,
                "initiated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            }
        
        document_to_save = {
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "time_injected_to_prompt": prompts["current_time"],
            "system_prompt_sent": prompts["system"],
            "full_user_prompt_sent": prompts["user"],
            "intrusive_thought_injected": context["intrusive_thought"],
            "recent_contextual_information_input": context["recent_context"], 
            "think_output": thought_json.get("think"),
            "emotion_output": thought_json.get("emotion"),
            "next_think_output": thought_json.get("next_think"),
            "to_do_output": thought_json.get("to_do", ""),
            "done_output": thought_json.get("done", False),
            "action_to_take_output": thought_json.get("action_to_take", ""),
            "action_motivation_output": thought_json.get("action_motivation", ""),
            "action_attempted": initiated_action_data_for_db,
            "image_inputs_count": len(context["images"]),
            "image_inputs_preview": [img[:100] for img in context["images"][:3]],
            "_llm_usage_info": thought_json.get("_llm_usage_info")
        }
        
        saved_key = await self.thought_storage_service.save_main_thought_document(document_to_save)
        if not saved_key:
            self.logger.error("保存思考文档失败！")
            return None
            
        return saved_key

    def _dispatch_action(self, thought_json: Dict, saved_thought_key: str, recent_context: str):
        action_desc = thought_json.get("action_to_take", "").strip()
        if action_desc and action_desc.lower() != "null" and self.action_handler_instance:
            action_id = thought_json.get("action_id", str(uuid.uuid4())) 
            self.logger.info(f"产生了行动意图，开始分发任务: {action_desc}")
            action_task = asyncio.create_task(
                self.action_handler_instance.process_action_flow(
                    action_id=action_id,
                    doc_key_for_updates=saved_thought_key,
                    action_description=action_desc,
                    action_motivation=thought_json.get("action_motivation", "没有明确动机。"),
                    current_thought_context=thought_json.get("think", "无特定思考上下文。"),
                    relevant_adapter_messages_context=recent_context 
                )
            )
            action_task.add_done_callback(lambda t: self.logger.info(f"动作任务 {t} 已结束。"))

    async def _reply_to_master(self, content_str: str): 
        if not content_str or not content_str.strip() or content_str.strip().lower() == "null":
            self.logger.info(f"AI 决定不回复主人，因为内容无效 (空, 全是空格, 或 'null'): '{content_str[:50]}...'")
            return
        
        self.logger.info(f"AI 决定回复主人: {content_str[:50]}...")
        reply_event = ProtocolEvent(
            event_id=f"event_master_reply_{uuid.uuid4()}",
            event_type="action.masterui.text",
            time=int(time.time() * 1000),
            platform="master_ui",
            bot_id=config.persona.bot_name,
            conversation_info=ProtocolConversationInfo(
                conversation_id="master_chat", type="private"
            ),
            content=[SegBuilder.text(content_str)] 
        )
        master_adapter_id = "master_ui_adapter" 
        send_success = await self.core_comm_layer.send_action_to_adapter_by_id(master_adapter_id, reply_event)
        if not send_success:
            self.logger.error(f"向主人UI (adapter_id: {master_adapter_id}) 发送回复失败了，呜呜呜，主人会不会收不到我的爱意呀？")

    async def _core_thinking_loop(self) -> None:
        thinking_interval_sec = config.core_logic_settings.thinking_interval_seconds
        while not self.stop_event.is_set():
            current_time_str = datetime.datetime.now().strftime("%Y年%m月%d日 %H点%M分%S秒")
            master_chat_str, other_context_str, image_list = await self._gather_context()
            current_state, action_id_seen = await self.state_manager.get_current_state_for_prompt(other_context_str)
            if action_id_seen: pass            
            intrusive_thought_str = ""
            if self.intrusive_generator_instance and config.intrusive_thoughts_module_settings.enabled and random.random() < config.intrusive_thoughts_module_settings.insertion_probability:
                random_thought_doc = await self.thought_storage_service.get_random_unused_intrusive_thought_document()
                if random_thought_doc and random_thought_doc.get("text"):
                    intrusive_thought_str = f"你突然有一个神奇的念头：{random_thought_doc['text']}"

            system_prompt = self.prompt_builder.build_system_prompt(current_time_str)
            user_prompt = self.prompt_builder.build_user_prompt(current_state, master_chat_str, intrusive_thought_str)
            
            logger.debug(f"系统提示: {system_prompt}")
            logger.debug(f"用户提示: {user_prompt}") 
            self.logger.info(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {config.persona.bot_name} 开始思考...")
            generated_thought = await self._generate_thought_from_llm(system_prompt, user_prompt, image_list)

            if generated_thought:
                self.logger.info(f"思考完成: {generated_thought.get('think', '无内容')[:50]}...")
                await self._reply_to_master(generated_thought.get("reply_to_master", ""))
                saved_key = await self._process_and_store_thought(
                    generated_thought, 
                    prompts={"system": system_prompt, "user": user_prompt, "current_time": current_time_str},
                    context={"recent_context": other_context_str, "images": image_list, "intrusive_thought": intrusive_thought_str}
                )
                if saved_key:
                    self._dispatch_action(generated_thought, saved_key, other_context_str)

            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.immediate_thought_trigger.wait(), timeout=float(thinking_interval_sec))
                self.immediate_thought_trigger.clear()
                self.logger.info("被动思考被触发，立即开始新一轮思考。")
            if self.stop_event.is_set(): break
        self.logger.info(f"--- {config.persona.bot_name} 的意识流动已停止 ---")

    async def start_thinking_loop(self) -> asyncio.Task:
        self.logger.info(f"=== {config.persona.bot_name} (包工头版) 的大脑准备开始持续思考 ===")
        self.thinking_loop_task = asyncio.create_task(self._core_thinking_loop())
        return self.thinking_loop_task

    async def stop(self) -> None:
        self.logger.info(f"--- {config.persona.bot_name} 的意识流动正在停止 ---")
        self.stop_event.set()
        if self.thinking_loop_task and not self.thinking_loop_task.done():
            self.thinking_loop_task.cancel()
            try: await self.thinking_loop_task
            except asyncio.CancelledError: self.logger.info("主思考循环任务已被取消。")
