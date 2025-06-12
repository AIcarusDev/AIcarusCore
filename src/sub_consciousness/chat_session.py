# -*- coding: utf-8 -*-
import asyncio
import time
import json
import re 
import uuid 
from datetime import datetime
from typing import Optional, Dict, Any, List ,Tuple
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

logger = get_logger(__name__)

# Templates are now in ChatPromptBuilder

class ChatSession: # Renamed class
    def __init__(
        self,
        conversation_id: str,
        llm_client: LLMProcessorClient,
        event_storage: EventStorageService,
        action_handler: ActionHandler,
        bot_id: str, # Renamed parameter from bot_qq_id to bot_id
        platform: str, 
        conversation_type: str 
    ):
        self.conversation_id: str = conversation_id
        self.llm_client: LLMProcessorClient = llm_client
        self.event_storage: EventStorageService = event_storage
        self.action_handler: ActionHandler = action_handler
        self.bot_id: str = bot_id # Use self.bot_id
        self.platform: str = platform
        self.conversation_type: str = conversation_type
        self.is_active: bool = False
        self.last_active_time: float = 0.0
        self.last_processed_timestamp: float = 0.0
        self.last_llm_decision: Optional[Dict[str, Any]] = None
        self.sent_actions_context: OrderedDict[str, Dict[str, Any]] = OrderedDict()
        self.processing_lock = asyncio.Lock()
        
        # Instantiate the prompt builder
        # Passing self.bot_id to the builder
        self.prompt_builder = ChatPromptBuilder(
            event_storage=self.event_storage,
            bot_id=self.bot_id, 
            conversation_id=self.conversation_id
        )
        logger.info(f"[ChatSession][{self.conversation_id}] 实例已创建。")

    def activate(self):
        if not self.is_active:
            self.is_active = True
            self.last_active_time = time.time()
            logger.info(f"[ChatSession][{self.conversation_id}] 已激活。")

    def deactivate(self):
        if self.is_active:
            self.is_active = False
            self.last_llm_decision = None 
            self.last_processed_timestamp = 0.0
            logger.info(f"[ChatSession][{self.conversation_id}] 已因不活跃而停用。")

    async def _build_prompt(self) -> Tuple[str, str]:
        return await self.prompt_builder.build_prompts(
            last_processed_timestamp=self.last_processed_timestamp,
            last_llm_decision=self.last_llm_decision,
            sent_actions_context=self.sent_actions_context
        )

    async def process_event(self, event: Event):
        if not self.is_active:
            return

        async with self.processing_lock:
            self.last_active_time = time.time()
            
            # _build_prompt now returns a 3-tuple including the uid_str_to_platform_id_map
            system_prompt, user_prompt, uid_str_to_platform_id_map = await self._build_prompt()
            logger.debug(f"构建的System Prompt:\n{system_prompt}")
            logger.debug(f"构建的User Prompt:\n{user_prompt}")
            logger.debug(f"构建的 UID->PlatformID Map:\n{uid_str_to_platform_id_map}")
            
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

                    action_event_dict = {
                        "event_id": f"sub_chat_reply_{uuid.uuid4()}",
                        "event_type": "action.message.send", 
                        "platform": platform_for_action,
                        "bot_id": self.bot_id, # Use self.bot_id
                        "conversation_info": {"conversation_id": self.conversation_id, "type": conv_type_for_action},
                        "content": content_segs_payload 
                    }
                    
                    logger.info(f"[ChatSession][{self.conversation_id}] Decided to reply: {reply_text_content}")
                    
                    success, msg = await self.action_handler.submit_constructed_action(
                        action_event_dict, 
                        "发送子意识聊天回复"
                    )
                    if success:
                        logger.info(f"[ChatSession][{self.conversation_id}] Action to send reply submitted successfully: {msg}")
                        if parsed_response_data.get("motivation"):
                            action_event_id = action_event_dict['event_id']
                            self.sent_actions_context[action_event_id] = {
                                "motivation": parsed_response_data.get("motivation"),
                                "reply_text": reply_text_content 
                            }
                            if len(self.sent_actions_context) > 10:
                                self.sent_actions_context.popitem(last=False) 
                    else:
                        logger.error(f"[ChatSession][{self.conversation_id}] Failed to submit action to send reply: {msg}")
                else:
                    motivation = parsed_response_data.get("motivation")
                    if motivation:
                        logger.info(f"[ChatSession][{self.conversation_id}] Decided not to reply. Motivation: {motivation}")
                        try:
                            internal_act_event_dict = {
                                "event_id": f"internal_act_{uuid.uuid4()}",
                                "event_type": "internal.sub_consciousness.thought_log",
                                "time": time.time() * 1000, 
                                "platform": self.platform,
                                "bot_id": self.bot_id, # Use self.bot_id
                                "user_info": UserInfo(user_id=self.bot_id, user_nickname=config.persona.bot_name).to_dict(), 
                                "conversation_info": ConversationInfo(conversation_id=self.conversation_id, type=self.conversation_type, platform=self.platform).to_dict(),
                                "content": [SegBuilder.text(motivation).to_dict()]
                            }
                            await self.event_storage.save_event_document(internal_act_event_dict)
                            logger.debug(f"[ChatSession][{self.conversation_id}] Saved internal ACT event for not replying.")
                        except Exception as e_save_act:
                            logger.error(f"[ChatSession][{self.conversation_id}] Failed to save internal ACT event: {e_save_act}", exc_info=True)
                            
                self.last_processed_timestamp = event.time 
            
            except json.JSONDecodeError as e_json:
                logger.error(f"[ChatSession][{self.conversation_id}] Error decoding LLM response JSON: {e_json}. Response text (first 200 chars): {response_text[:200]}...", exc_info=True)
                self.last_llm_decision = {"think": f"Error decoding LLM JSON: {e_json}", "reply_willing": False, "motivation": "System error processing LLM response"} # reasoning -> think
            except KeyError as e_key:
                logger.error(f"[ChatSession][{self.conversation_id}] Missing key in LLM response: {e_key}. Parsed data: {parsed_response_data if 'parsed_response_data' in locals() else 'N/A'}", exc_info=True)
                self.last_llm_decision = {"think": f"Missing key in LLM response: {e_key}", "reply_willing": False, "motivation": "System error processing LLM response"} # reasoning -> think
            except AttributeError as e_attr:
                logger.error(f"[ChatSession][{self.conversation_id}] Attribute error while processing LLM response: {e_attr}. Parsed data: {parsed_response_data if 'parsed_response_data' in locals() else 'N/A'}", exc_info=True)
                self.last_llm_decision = {"think": f"Attribute error processing LLM response: {e_attr}", "reply_willing": False, "motivation": "System error processing LLM response"} # reasoning -> think
            except Exception as e_general: 
                logger.error(f"[ChatSession][{self.conversation_id}] Unexpected error processing LLM response: {e_general}", exc_info=True)
                self.last_llm_decision = {"think": f"Unexpected error: {e_general}", "reply_willing": False, "motivation": "System error processing LLM response"} # reasoning -> think
