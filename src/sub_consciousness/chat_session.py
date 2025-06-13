# -*- coding: utf-8 -*-
import asyncio
import time
import json
import re 
import uuid 
from datetime import datetime
from typing import Optional, Dict, Any, List ,Tuple, TYPE_CHECKING # 确保导入 TYPE_CHECKING
from collections import OrderedDict

from aicarus_protocols.event import Event
from aicarus_protocols.seg import Seg, SegBuilder
from aicarus_protocols.common import extract_text_from_content
from aicarus_protocols.user_info import UserInfo
from aicarus_protocols.conversation_info import ConversationInfo

from src.llmrequest.llm_processor import Client as LLMProcessorClient
from src.database.services.event_storage_service import EventStorageService
from src.action.action_handler import ActionHandler
from src.common.custom_logging.logger_manager import get_logger
from src.config import config
from .chat_prompt_builder import ChatPromptBuilder # Import the new builder

if TYPE_CHECKING:
    from src.core_logic.consciousness_flow import CoreLogic as CoreLogicFlow
    from src.sub_consciousness.chat_session_manager import ChatSessionManager
    from src.core_logic.summarization_service import SummarizationService

logger = get_logger(__name__)

# Templates are now in ChatPromptBuilder

class ChatSession: # Renamed class
    def __init__(
        self,
        conversation_id: str,
        llm_client: LLMProcessorClient,
        event_storage: EventStorageService,
        action_handler: ActionHandler,
        bot_id: str, 
        platform: str, 
        conversation_type: str,
        core_logic: 'CoreLogicFlow', 
        chat_session_manager: 'ChatSessionManager', 
        summarization_service: 'SummarizationService' 
    ):
        self.conversation_id: str = conversation_id
        self.llm_client: LLMProcessorClient = llm_client
        self.event_storage: EventStorageService = event_storage
        self.action_handler: ActionHandler = action_handler
        self.bot_id: str = bot_id
        self.platform: str = platform
        self.conversation_type: str = conversation_type
        
        # 新增的依赖实例
        self.core_logic = core_logic
        self.chat_session_manager = chat_session_manager
        self.summarization_service = summarization_service

        self.is_active: bool = False
        self.last_active_time: float = 0.0
        self.last_processed_timestamp: float = 0.0 # 记录本会话处理到的最新消息时间戳
        self.last_llm_decision: Optional[Dict[str, Any]] = None
        self.sent_actions_context: OrderedDict[str, Dict[str, Any]] = OrderedDict()
        self.processing_lock = asyncio.Lock()

        # 新增状态，用于首次构建 prompt
        self.is_first_turn_for_session: bool = True
        self.initial_core_think: Optional[str] = None

        # 新增状态，用于渐进式总结
        self.current_handover_summary: Optional[str] = None
        self.events_since_last_summary: List[Dict[str, Any]] = []
        self.message_count_since_last_summary: int = 0
        self.SUMMARY_INTERVAL: int = getattr(config.sub_consciousness, "summary_interval", 5) # 从配置读取或默认5条消息
        
        # Instantiate the prompt builder
        # Passing self.bot_id to the builder
        self.prompt_builder = ChatPromptBuilder(
            event_storage=self.event_storage,
            bot_id=self.bot_id, 
            conversation_id=self.conversation_id
        )
        logger.info(f"[ChatSession][{self.conversation_id}] 实例已创建。")

    def activate(self, core_last_think: Optional[str] = None): # 增加参数以接收主意识想法
        if not self.is_active:
            self.is_active = True
            self.is_first_turn_for_session = True 
            self.initial_core_think = core_last_think
            self.last_active_time = time.time()
            
            # 重置渐进式总结状态
            self.current_handover_summary = None
            self.events_since_last_summary = []
            self.message_count_since_last_summary = 0
            logger.info(
                f"[ChatSession][{self.conversation_id}] 已激活。首次处理: {self.is_first_turn_for_session}, "
                f"主意识想法: '{core_last_think}', 渐进式总结状态已重置。"
            )
        else:
            self.initial_core_think = core_last_think 
            self.is_first_turn_for_session = True 
            # 如果重复激活，也重置总结状态，视为一个新的专注周期开始
            self.current_handover_summary = None
            self.events_since_last_summary = []
            self.message_count_since_last_summary = 0
            logger.info(
                f"[ChatSession][{self.conversation_id}] 已是激活状态，重新设置首次处理标记、主意识想法: '{core_last_think}', "
                f"并重置渐进式总结状态。"
            )

    def deactivate(self):
        if self.is_active:
            self.is_active = False
            self.last_llm_decision = None 
            self.last_processed_timestamp = 0.0
            logger.info(f"[ChatSession][{self.conversation_id}] 已因不活跃而停用。")

    async def _build_prompt(self) -> Tuple[str, str, Dict[str, str], List[str]]:
        # Assuming self.prompt_builder.build_prompts will be updated to return processed_event_ids
        # This change is anticipatory for when ChatPromptBuilder is modified.
        # For now, this might cause a runtime error if ChatPromptBuilder doesn't return 4 items.
        # Or, more likely, a type error if it returns 3 and we try to unpack 4.
        # We'll handle the actual return from prompt_builder later.
        # For now, let's assume it returns what we need for the logic below.
        # Placeholder:
        # system_prompt, user_prompt, uid_map, processed_ids = await self.prompt_builder.build_prompts(...)
        # return system_prompt, user_prompt, uid_map, processed_ids
        # Actual call, assuming it might not yet return 4 items, we'll mock the 4th for now
        # ChatPromptBuilder.build_prompts 现在需要 is_first_turn 和 last_think_from_core
        # 并且返回4个值
        system_prompt, user_prompt, uid_map, processed_ids = await self.prompt_builder.build_prompts(
            last_processed_timestamp=self.last_processed_timestamp,
            last_llm_decision=self.last_llm_decision, # 子意识上一轮的思考结果
            sent_actions_context=self.sent_actions_context,
            is_first_turn=self.is_first_turn_for_session,
            last_think_from_core=self.initial_core_think # 主意识传递过来的想法
        )
        return system_prompt, user_prompt, uid_map, processed_ids


    async def process_event(self, event: Event): # event 参数可能是触发本次 process 的新事件，也可能只是个信号
        if not self.is_active:
            return

        async with self.processing_lock:
            self.last_active_time = time.time()
            
            system_prompt, user_prompt, uid_str_to_platform_id_map, processed_event_ids = await self._build_prompt()
            logger.debug(f"构建的System Prompt:\n{system_prompt}")
            logger.debug(f"构建的User Prompt:\n{user_prompt}")
            logger.debug(f"构建的 UID->PlatformID Map:\n{uid_str_to_platform_id_map}")
            logger.debug(f"从prompt_builder获取的 processed_event_ids (可能为空): {processed_event_ids}") # 新增日志
            
            llm_api_response = await self.llm_client.make_llm_request(
                prompt=user_prompt, 
                system_prompt=system_prompt, 
                is_stream=False
            )
            response_text = llm_api_response.get("text") if llm_api_response else None
            
            if not response_text or (llm_api_response and llm_api_response.get("error")):
                error_msg = llm_api_response.get('message') if llm_api_response else '无响应'
                logger.error(f"[ChatSession][{self.conversation_id}] LLM调用失败或返回空: {error_msg}")
                self.last_llm_decision = {"think": f"LLM调用失败: {error_msg}", "reply_willing": False, "motivation": "系统错误导致无法思考"} # reasoning -> think
                return
            
            try:
                parsed_response_data = None
                if response_text:
                    match = re.search(r"```json\s*(\{[\s\S]*?\})\s*```", response_text, re.DOTALL)
                    if match:
                        json_str = match.group(1)
                        try:
                            parsed_response_data = json.loads(json_str)
                        except json.JSONDecodeError as e_json_block:
                            logger.error(f"[ChatSession][{self.conversation_id}] 解析被```json包裹的响应时JSONDecodeError: {e_json_block}. JSON string: {json_str[:200]}...")
                            # parsed_response_data remains None
                    else: 
                        try:
                            parsed_response_data = json.loads(response_text)
                        except json.JSONDecodeError as e_json_direct:
                             logger.warning(f"[ChatSession][{self.conversation_id}] LLM响应不是有效的JSON，且未被```json包裹: {response_text[:200]}. Error: {e_json_direct}")
                             # parsed_response_data remains None
                
                if not parsed_response_data:
                    logger.error(f"[ChatSession][{self.conversation_id}] LLM响应最终解析失败或为空。")
                    self.last_llm_decision = {"think": "LLM响应解析失败或为空", "reply_willing": False, "motivation": "系统错误导致无法解析LLM的胡言乱语"} # reasoning -> think
                    return

                self.last_llm_decision = parsed_response_data

                # 新增：检查是否需要结束专注模式
                if parsed_response_data.get("end_focused_chat") is True:
                    logger.info(f"[ChatSession][{self.conversation_id}] LLM决策结束专注模式。")
                    handover_summary = "未能生成交接总结。" # 默认值
                    try:
                        # 1. 调用总结服务
                        # 获取当前会话的完整历史记录用于总结
                        # 注意：limit 参数需要合理设置，或者 SummarizationService 内部处理超长历史
                        # fetch_all_event_types=True 确保获取所有相关事件进行总结
                        # 也可以考虑只传 message.* 类型的事件给总结服务
                        conversation_history_events = await self.event_storage.get_recent_chat_message_documents(
                            conversation_id=self.conversation_id, 
                            limit=config.sub_consciousness.get("summary_history_limit", 200), # 从配置读取或使用默认值
                            fetch_all_event_types=True 
                        )
                        logger.debug(f"[ChatSession][{self.conversation_id}] 获取到 {len(conversation_history_events)} 条事件用于总结。")

                        if hasattr(self.summarization_service, 'summarize_conversation') and callable(getattr(self.summarization_service, 'summarize_conversation')):
                            handover_summary = await self.summarization_service.summarize_conversation(conversation_history_events)
                            logger.info(f"[ChatSession][{self.conversation_id}] 生成交接总结 (前100字符): {handover_summary[:100]}...")
                        else:
                            logger.error(f"[ChatSession][{self.conversation_id}] SummarizationService 没有 summarize_conversation 方法或该方法不可调用！")
                            handover_summary = f"对会话 {self.conversation_id} 的专注交互已结束（总结服务异常）。"
                    except Exception as e_summarize:
                        logger.error(f"[ChatSession][{self.conversation_id}] 调用总结服务时发生错误: {e_summarize}", exc_info=True)
                        handover_summary = f"对会话 {self.conversation_id} 的专注交互已结束（总结时发生错误）。"
                    
                    # 2. 获取最后的思考
                    last_session_think = self.last_llm_decision.get("think", "专注会话结束，无特定最终想法。")
                    
                    # 3. 触发主意识
                    if hasattr(self.core_logic, 'trigger_immediate_thought_cycle'):
                        self.core_logic.trigger_immediate_thought_cycle(handover_summary, last_session_think)
                        logger.info(f"[ChatSession][{self.conversation_id}] 已触发主意识的trigger_immediate_thought_cycle。")
                    else:
                        logger.error(f"[ChatSession][{self.conversation_id}] core_logic 对象没有 trigger_immediate_thought_cycle 方法！")
                    
                    # 4. 停用并销毁自己
                    if hasattr(self.chat_session_manager, 'deactivate_session'):
                        # deactivate_session 应该是同步的，它只是从管理器中移除并调用 session.deactivate()
                        self.chat_session_manager.deactivate_session(self.conversation_id) 
                        logger.info(f"[ChatSession][{self.conversation_id}] 已请求 ChatSessionManager 停用本会话。")
                    else:
                        logger.error(f"[ChatSession][{self.conversation_id}] chat_session_manager 对象没有 deactivate_session 方法！")
                    
                    return # 结束处理

                # --- Sanitize optional fields: treat "" (empty string) as None ---
                fields_to_sanitize = ["at_someone", "quote_reply", "reply_text", "poke", "action_to_take", "action_motivation"]
                for field in fields_to_sanitize:
                    if self.last_llm_decision.get(field) == "":
                        self.last_llm_decision[field] = None
                
                # If action_to_take became None (either originally or from being an empty string), 
                # ensure action_motivation is also treated as None.
                if self.last_llm_decision.get("action_to_take") is None and "action_motivation" in self.last_llm_decision:
                    self.last_llm_decision["action_motivation"] = None
                # --- End sanitization ---

                action_or_thought_recorded_successfully = False # 新增标志位，用于判断是否需要标记事件为已处理

                # Now use the (potentially sanitized) values from self.last_llm_decision for logic
                if self.last_llm_decision.get("reply_willing") and self.last_llm_decision.get("reply_text"): # Check against sanitized reply_text
                    reply_text_content = self.last_llm_decision["reply_text"] # Known to be non-empty and not None if condition met
                    at_target_values_raw = self.last_llm_decision.get("at_someone") # Will be None if originally null or ""
                    quote_msg_id = self.last_llm_decision.get("quote_reply") # Will be None if originally null or ""

                    content_segs_payload: List[Dict[str, Any]] = []
                    
                    if quote_msg_id:
                        content_segs_payload.append(SegBuilder.reply(message_id=quote_msg_id).to_dict())
                    
                    at_added_flag = False
                    if at_target_values_raw:
                        raw_targets = []
                        if isinstance(at_target_values_raw, str):
                            raw_targets = [target.strip() for target in at_target_values_raw.split(',') if target.strip()]
                        elif isinstance(at_target_values_raw, list):
                            raw_targets = [str(target).strip() for target in at_target_values_raw if str(target).strip()]
                        elif at_target_values_raw: # Single non-string, non-list value (should be string as per prompt)
                            raw_targets = [str(at_target_values_raw).strip()]

                        actual_platform_ids_to_at: List[str] = []
                        for raw_target_id in raw_targets:
                            if raw_target_id.startswith("U") and raw_target_id in uid_str_to_platform_id_map:
                                actual_id = uid_str_to_platform_id_map[raw_target_id]
                                actual_platform_ids_to_at.append(actual_id)
                                logger.info(f"[ChatSession][{self.conversation_id}] Converted at_target '{raw_target_id}' to platform ID '{actual_id}'.")
                            elif re.match(r"^\d+$", raw_target_id): # If it's already a numeric ID (potential QQ)
                                actual_platform_ids_to_at.append(raw_target_id)
                            else:
                                logger.warning(f"[ChatSession][{self.conversation_id}] Invalid or unmappable at_target_id '{raw_target_id}' from LLM. Skipping.")
                        
                        for platform_id_to_at in actual_platform_ids_to_at:
                            content_segs_payload.append(SegBuilder.at(user_id=platform_id_to_at, display_name="").to_dict())
                            at_added_flag = True
                    
                    if at_added_flag and reply_text_content: 
                        content_segs_payload.append(SegBuilder.text(" ").to_dict())
                    
                    if reply_text_content: 
                        content_segs_payload.append(SegBuilder.text(reply_text_content).to_dict())
                    elif at_added_flag and not reply_text_content: 
                        if not content_segs_payload or \
                           not (content_segs_payload[-1].get("type") == "text" and content_segs_payload[-1].get("data", {}).get("text") == " "):
                            content_segs_payload.append(SegBuilder.text(" ").to_dict())

                    platform_for_action = event.platform 
                    conv_type_for_action = event.conversation_info.type if event.conversation_info else "unknown"
                    
                    current_motivation = parsed_response_data.get("motivation") # 获取动机

                    action_event_dict = {
                        "event_id": f"sub_chat_reply_{uuid.uuid4()}",
                        "event_type": "action.message.send", 
                        "platform": platform_for_action,
                        "bot_id": self.bot_id, # Use self.bot_id
                        "conversation_info": {"conversation_id": self.conversation_id, "type": conv_type_for_action},
                        "content": content_segs_payload,
                        # 如果有动机，就把它加到要发送的动作事件里
                        "motivation": current_motivation if current_motivation and current_motivation.strip() else None
                    }
                    
                    logger.info(f"[ChatSession][{self.conversation_id}] Decided to reply: {reply_text_content}")
                    
                    success, msg = await self.action_handler.submit_constructed_action(
                        action_event_dict, 
                        "发送子意识聊天回复"
                    )
                    if success:
                        logger.info(f"[ChatSession][{self.conversation_id}] Action to send reply submitted successfully: {msg}")
                        action_or_thought_recorded_successfully = True 
                        # motivation 已经包含在 action_event_dict 中，sent_actions_context 仍然用于临时辅助显示，直到 prompt_builder 完全依赖事件本身
                        if current_motivation: # current_motivation 是从 parsed_response_data.get("motivation") 获取的
                            action_event_id = action_event_dict['event_id']
                            self.sent_actions_context[action_event_id] = {
                                "motivation": current_motivation,
                                "reply_text": reply_text_content 
                            }
                            if len(self.sent_actions_context) > 10: self.sent_actions_context.popitem(last=False)
                        
                        # 将AI的回复事件也加入待总结列表
                        # action_event_dict 现在可能包含 motivation
                        self.events_since_last_summary.append(action_event_dict)
                        self.message_count_since_last_summary +=1
                    else:
                        logger.error(f"[ChatSession][{self.conversation_id}] Failed to submit action to send reply: {msg}")
                else: 
                    motivation = parsed_response_data.get("motivation")
                    if motivation: 
                        logger.info(f"[ChatSession][{self.conversation_id}] Decided not to reply. Motivation: {motivation}")
                        internal_act_event_dict = {} # 定义在 try 外部以便 finally 中使用
                        try:
                            internal_act_event_dict = {
                                "event_id": f"internal_act_{uuid.uuid4()}",
                                "event_type": "internal.sub_consciousness.thought_log",
                                "time": time.time() * 1000, 
                                "platform": self.platform,
                                "bot_id": self.bot_id, 
                                "user_info": UserInfo(user_id=self.bot_id, user_nickname=config.persona.bot_name).to_dict(), 
                                "conversation_info": ConversationInfo(conversation_id=self.conversation_id, type=self.conversation_type, platform=self.platform).to_dict(),
                                "content": [SegBuilder.text(motivation).to_dict()]
                            }
                            await self.event_storage.save_event_document(internal_act_event_dict)
                            logger.debug(f"[ChatSession][{self.conversation_id}] Saved internal ACT event for not replying.")
                            action_or_thought_recorded_successfully = True 
                            # 将AI的思考日志事件也加入待总结列表
                            self.events_since_last_summary.append(internal_act_event_dict)
                            self.message_count_since_last_summary +=1
                        except Exception as e_save_act:
                            logger.error(f"[ChatSession][{self.conversation_id}] Failed to save internal ACT event: {e_save_act}", exc_info=True)
                
                if action_or_thought_recorded_successfully:
                    # 将触发本次处理的原始事件（通常是用户消息）加入待总结列表
                    # event 是 process_event 的参数
                    if event and event.event_type.startswith("message."): # 确保是消息事件
                         self.events_since_last_summary.append(event.to_dict()) # 使用 Event 对象自带的 to_dict() 方法
                         self.message_count_since_last_summary +=1
                         logger.debug(f"[ChatSession][{self.conversation_id}] Added incoming event {event.event_id} to summary queue.")
                    
                    # 标记处理过的输入事件为已读
                    if processed_event_ids: # processed_event_ids 来自 _build_prompt
                        try:
                            success_mark = await self.event_storage.mark_events_as_processed(processed_event_ids, True)
                            if success_mark: logger.info(f"[ChatSession][{self.conversation_id}] Successfully marked {len(processed_event_ids)} events as processed.")
                            else: logger.error(f"[ChatSession][{self.conversation_id}] Failed to mark {len(processed_event_ids)} events as processed.")
                        except Exception as e_mark_processed:
                            logger.error(f"[ChatSession][{self.conversation_id}] Error marking events as processed: {e_mark_processed}", exc_info=True)

                    # 检查是否需要进行微总结
                    if self.message_count_since_last_summary >= self.SUMMARY_INTERVAL:
                        logger.info(f"[ChatSession][{self.conversation_id}] Reached summary interval ({self.message_count_since_last_summary}/{self.SUMMARY_INTERVAL}). Triggering incremental summary.")
                        try:
                            if hasattr(self.summarization_service, 'summarize_incrementally') and callable(getattr(self.summarization_service, 'summarize_incrementally')):
                                new_summary = await self.summarization_service.summarize_incrementally(
                                    self.current_handover_summary, 
                                    self.events_since_last_summary
                                )
                                self.current_handover_summary = new_summary
                                self.events_since_last_summary = []
                                self.message_count_since_last_summary = 0
                                logger.info(f"[ChatSession][{self.conversation_id}] Incremental summary updated. New summary (first 50 chars): {new_summary[:50]}...")
                            else:
                                logger.error(f"[ChatSession][{self.conversation_id}] SummarizationService does not have summarize_incrementally method.")
                        except Exception as e_inc_summary:
                            logger.error(f"[ChatSession][{self.conversation_id}] Error during incremental summarization: {e_inc_summary}", exc_info=True)
                
                if self.is_first_turn_for_session:
                    self.is_first_turn_for_session = False
                    logger.debug(f"[ChatSession][{self.conversation_id}] is_first_turn_for_session 设置为 False。")
                            
                self.last_processed_timestamp = event.time 
            
            except json.JSONDecodeError as e_json:
                if self.is_first_turn_for_session:
                    self.is_first_turn_for_session = False
                    logger.debug(f"[ChatSession][{self.conversation_id}] is_first_turn_for_session 设置为 False。")
                            
                self.last_processed_timestamp = event.time 
            
                self.last_llm_decision = {"think": f"Error decoding LLM JSON: {e_json}", "reply_willing": False, "motivation": "System error processing LLM response"}
                # 即使解析失败，也认为“第一轮”尝试过了
                if self.is_first_turn_for_session:
                    self.is_first_turn_for_session = False
                    logger.debug(f"[ChatSession][{self.conversation_id}] is_first_turn_for_session 设置为 False (JSONDecodeError后)。")
                if event: self.last_processed_timestamp = event.time # 记录处理到的时间戳
            except KeyError as e_key:
                logger.error(f"[ChatSession][{self.conversation_id}] Missing key in LLM response: {e_key}. Parsed data: {parsed_response_data if 'parsed_response_data' in locals() else 'N/A'}", exc_info=True)
                self.last_llm_decision = {"think": f"Missing key in LLM response: {e_key}", "reply_willing": False, "motivation": "System error processing LLM response"}
                if self.is_first_turn_for_session:
                    self.is_first_turn_for_session = False
                    logger.debug(f"[ChatSession][{self.conversation_id}] is_first_turn_for_session 设置为 False (KeyError后)。")
                if event: self.last_processed_timestamp = event.time
            except AttributeError as e_attr: # This was the original error point for the KeyError: 'mood'
                logger.error(f"[ChatSession][{self.conversation_id}] Attribute error while processing LLM response: {e_attr}. Parsed data: {parsed_response_data if 'parsed_response_data' in locals() else 'N/A'}", exc_info=True)
                self.last_llm_decision = {"think": f"Attribute error processing LLM response: {e_attr}", "reply_willing": False, "motivation": "System error processing LLM response"}
                if self.is_first_turn_for_session:
                    self.is_first_turn_for_session = False
                    logger.debug(f"[ChatSession][{self.conversation_id}] is_first_turn_for_session 设置为 False (AttributeError后)。")
                if event: self.last_processed_timestamp = event.time
            except Exception as e_general: 
                logger.error(f"[ChatSession][{self.conversation_id}] Unexpected error processing LLM response: {e_general}", exc_info=True)
                self.last_llm_decision = {"think": f"Unexpected error: {e_general}", "reply_willing": False, "motivation": "System error processing LLM response"}
                if self.is_first_turn_for_session:
                    self.is_first_turn_for_session = False
                    logger.debug(f"[ChatSession][{self.conversation_id}] is_first_turn_for_session 设置为 False (GeneralException后)。")
                if event: self.last_processed_timestamp = event.time
