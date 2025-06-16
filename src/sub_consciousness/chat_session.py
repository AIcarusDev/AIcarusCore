import asyncio
import json
import random
import re
import time
import uuid
from collections import OrderedDict
from typing import TYPE_CHECKING, Any  # 确保导入 TYPE_CHECKING

from aicarus_protocols.conversation_info import ConversationInfo
from aicarus_protocols.event import Event
from aicarus_protocols.seg import SegBuilder
from aicarus_protocols.user_info import UserInfo

from src.action.action_handler import ActionHandler
from src.common.custom_logging.logger_manager import get_logger
from src.common.text_splitter import process_llm_response
from src.config import config
from src.database.services.event_storage_service import EventStorageService
from src.llmrequest.llm_processor import Client as LLMProcessorClient

from .chat_prompt_builder import ChatPromptBuilder  # Import the new builder

if TYPE_CHECKING:
    from src.core_logic.consciousness_flow import CoreLogic as CoreLogicFlow
    from src.core_logic.summarization_service import SummarizationService
    from src.sub_consciousness.chat_session_manager import ChatSessionManager

logger = get_logger(__name__)

# Templates are now in ChatPromptBuilder


class ChatSession:  # Renamed class
    def __init__(
        self,
        conversation_id: str,
        llm_client: LLMProcessorClient,
        event_storage: EventStorageService,
        action_handler: ActionHandler,
        bot_id: str,
        platform: str,
        conversation_type: str,
        core_logic: "CoreLogicFlow",
        chat_session_manager: "ChatSessionManager",
        summarization_service: "SummarizationService",
    ) -> None:
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
        self.last_processed_timestamp: float = 0.0  # 记录本会话处理到的最新消息时间戳
        self.last_llm_decision: dict[str, Any] | None = None
        self.sent_actions_context: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self.processing_lock = asyncio.Lock()

        # 新增状态，用于首次构建 prompt
        self.is_first_turn_for_session: bool = True
        self.initial_core_think: str | None = None

        # 新增状态，用于渐进式总结
        self.current_handover_summary: str | None = None
        self.events_since_last_summary: list[dict[str, Any]] = []
        self.message_count_since_last_summary: int = 0
        self.SUMMARY_INTERVAL: int = getattr(config.sub_consciousness, "summary_interval", 5)  # 从配置读取或默认5条消息

        # Instantiate the prompt builder
        # Passing self.bot_id to the builder
        self.prompt_builder = ChatPromptBuilder(
            event_storage=self.event_storage, bot_id=self.bot_id, conversation_id=self.conversation_id
        )
        logger.info(f"[ChatSession][{self.conversation_id}] 实例已创建。")

    def activate(self, core_last_think: str | None = None) -> None:  # 增加参数以接收主意识想法
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

    def deactivate(self) -> None:
        if self.is_active:
            self.is_active = False
            self.last_llm_decision = None
            self.last_processed_timestamp = 0.0
            logger.info(f"[ChatSession][{self.conversation_id}] 已因不活跃而停用。")

    async def _build_prompt(self) -> tuple[str, str, dict[str, str], list[str]]:
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
            last_llm_decision=self.last_llm_decision,  # 子意识上一轮的思考结果
            sent_actions_context=self.sent_actions_context,
            is_first_turn=self.is_first_turn_for_session,
            last_think_from_core=self.initial_core_think,  # 主意识传递过来的想法
        )
        return system_prompt, user_prompt, uid_map, processed_ids

    async def process_event(self, event: Event) -> None:  # event 参数可能是触发本次 process 的新事件，也可能只是个信号
        if not self.is_active:
            return

        async with self.processing_lock:
            self.last_active_time = time.time()

            system_prompt, user_prompt, uid_str_to_platform_id_map, processed_event_ids = await self._build_prompt()
            logger.debug(f"构建的System Prompt:\n{system_prompt}")
            logger.debug(f"构建的User Prompt:\n{user_prompt}")
            logger.debug(f"构建的 UID->PlatformID Map:\n{uid_str_to_platform_id_map}")
            logger.debug(f"从prompt_builder获取的 processed_event_ids (可能为空): {processed_event_ids}")  # 新增日志

            llm_api_response = await self.llm_client.make_llm_request(
                prompt=user_prompt, system_prompt=system_prompt, is_stream=False
            )
            response_text = llm_api_response.get("text") if llm_api_response else None

            if not response_text or (llm_api_response and llm_api_response.get("error")):
                error_msg = llm_api_response.get("message") if llm_api_response else "无响应"
                logger.error(f"[ChatSession][{self.conversation_id}] LLM调用失败或返回空: {error_msg}")
                self.last_llm_decision = {
                    "think": f"LLM调用失败: {error_msg}",
                    "reply_willing": False,
                    "motivation": "系统错误导致无法思考",
                }  # reasoning -> think
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
                            logger.error(
                                f"[ChatSession][{self.conversation_id}] 解析被```json包裹的响应时JSONDecodeError: {e_json_block}. JSON string: {json_str[:200]}..."
                            )
                            # parsed_response_data remains None
                    else:
                        try:
                            parsed_response_data = json.loads(response_text)
                        except json.JSONDecodeError as e_json_direct:
                            logger.warning(
                                f"[ChatSession][{self.conversation_id}] LLM响应不是有效的JSON，且未被```json包裹: {response_text[:200]}. Error: {e_json_direct}"
                            )
                            # parsed_response_data remains None

                if not parsed_response_data:
                    logger.error(f"[ChatSession][{self.conversation_id}] LLM响应最终解析失败或为空。")
                    self.last_llm_decision = {
                        "think": "LLM响应解析失败或为空",
                        "reply_willing": False,
                        "motivation": "系统错误导致无法解析LLM的胡言乱语",
                    }  # reasoning -> think
                    return

                if "mood" not in parsed_response_data:
                    parsed_response_data["mood"] = "平静"  # 默认值
                self.last_llm_decision = parsed_response_data

                # 新增：检查是否需要结束专注模式
                if parsed_response_data.get("end_focused_chat") is True:
                    logger.info(f"[ChatSession][{self.conversation_id}] LLM决策结束专注模式。")

                    # 重构后：直接使用最后一次更新的增量摘要作为交接总结
                    handover_summary = self.current_handover_summary or "我结束了专注，但似乎没什么特别的总结可以交接。"
                    logger.info(
                        f"[ChatSession][{self.conversation_id}] 使用最终的整合摘要进行交接 (前100字符): {handover_summary[:100]}..."
                    )

                    # 获取最后的思考
                    last_session_think = self.last_llm_decision.get("think", "专注会话结束，无特定最终想法。")

                    # 触发主意识
                    if hasattr(self.core_logic, "trigger_immediate_thought_cycle"):
                        self.core_logic.trigger_immediate_thought_cycle(handover_summary, last_session_think)
                        logger.info(
                            f"[ChatSession][{self.conversation_id}] 已触发主意识的trigger_immediate_thought_cycle。"
                        )
                    else:
                        logger.error(
                            f"[ChatSession][{self.conversation_id}] core_logic 对象没有 trigger_immediate_thought_cycle 方法！"
                        )

                    # 4. 停用并销毁自己
                    if hasattr(self.chat_session_manager, "deactivate_session"):
                        # deactivate_session 是异步的，需要等待
                        await self.chat_session_manager.deactivate_session(self.conversation_id)
                        logger.info(f"[ChatSession][{self.conversation_id}] 已请求 ChatSessionManager 停用本会话。")
                    else:
                        logger.error(
                            f"[ChatSession][{self.conversation_id}] chat_session_manager 对象没有 deactivate_session 方法！"
                        )

                    return  # 结束处理

                # --- Sanitize optional fields: treat "" (empty string) as None ---
                fields_to_sanitize = [
                    "at_someone",
                    "quote_reply",
                    "reply_text",
                    "poke",
                    "action_to_take",
                    "action_motivation",
                ]
                for field in fields_to_sanitize:
                    if self.last_llm_decision.get(field) == "":
                        self.last_llm_decision[field] = None

                # If action_to_take became None (either originally or from being an empty string),
                # ensure action_motivation is also treated as None.
                if (
                    self.last_llm_decision.get("action_to_take") is None
                    and "action_motivation" in self.last_llm_decision
                ):
                    self.last_llm_decision["action_motivation"] = None
                # --- End sanitization ---

                action_or_thought_recorded_successfully = False  # 新增标志位，用于判断是否需要标记事件为已处理

                # Now use the (potentially sanitized) values from self.last_llm_decision for logic
                if self.last_llm_decision.get("reply_willing") and self.last_llm_decision.get("reply_text"):
                    original_reply_text = self.last_llm_decision["reply_text"]

                    # 使用新的文本分割器处理回复
                    split_sentences = process_llm_response(
                        text=original_reply_text,
                        enable_kaomoji_protection=config.sub_consciousness.enable_kaomoji_protection,
                        enable_splitter=config.sub_consciousness.enable_splitter,
                        max_length=config.sub_consciousness.max_length,
                        max_sentence_num=config.sub_consciousness.max_sentence_num,
                    )

                    logger.info(
                        f"[ChatSession][{self.conversation_id}] Original reply: '{original_reply_text}'. Split into {len(split_sentences)} sentences: {split_sentences}"
                    )

                    at_target_values_raw = self.last_llm_decision.get("at_someone")
                    quote_msg_id = self.last_llm_decision.get("quote_reply")
                    current_motivation = parsed_response_data.get("motivation")

                    # 循环发送分割后的句子
                    for i, sentence_text in enumerate(split_sentences):
                        content_segs_payload: list[dict[str, Any]] = []

                        # 只在第一条消息中添加引用和@
                        if i == 0:
                            if quote_msg_id:
                                content_segs_payload.append(SegBuilder.reply(message_id=quote_msg_id).to_dict())

                            if at_target_values_raw:
                                raw_targets = []
                                if isinstance(at_target_values_raw, str):
                                    raw_targets = [
                                        target.strip() for target in at_target_values_raw.split(",") if target.strip()
                                    ]
                                elif isinstance(at_target_values_raw, list):
                                    raw_targets = [
                                        str(target).strip() for target in at_target_values_raw if str(target).strip()
                                    ]
                                else:
                                    raw_targets = [str(at_target_values_raw).strip()]

                                actual_platform_ids_to_at: list[str] = []
                                for raw_target_id in raw_targets:
                                    if raw_target_id.startswith("U") and raw_target_id in uid_str_to_platform_id_map:
                                        actual_platform_ids_to_at.append(uid_str_to_platform_id_map[raw_target_id])
                                    else:
                                        actual_platform_ids_to_at.append(raw_target_id)

                                for platform_id_to_at in actual_platform_ids_to_at:
                                    content_segs_payload.append(
                                        SegBuilder.at(user_id=platform_id_to_at, display_name="").to_dict()
                                    )
                                if actual_platform_ids_to_at:
                                    content_segs_payload.append(SegBuilder.text(" ").to_dict())

                        content_segs_payload.append(SegBuilder.text(sentence_text).to_dict())

                        action_event_dict = {
                            "event_id": f"sub_chat_reply_{uuid.uuid4()}",
                            "event_type": "action.message.send",
                            "platform": event.platform,
                            "bot_id": self.bot_id,
                            "conversation_info": {
                                "conversation_id": self.conversation_id,
                                "type": event.conversation_info.type if event.conversation_info else "unknown",
                            },
                            "content": content_segs_payload,
                            "motivation": current_motivation
                            if i == 0 and current_motivation and current_motivation.strip()
                            else None,
                        }

                        # 描述必须固定，以匹配 ActionHandler 中的特例，从而绕过 thought_doc_key 检查
                        success, msg = await self.action_handler.submit_constructed_action(
                            action_event_dict, "发送子意识聊天回复"
                        )

                        if success:
                            logger.info(
                                f"[ChatSession][{self.conversation_id}] Action to send reply segment {i + 1} submitted successfully: {msg}"
                            )
                            action_or_thought_recorded_successfully = True
                            self.events_since_last_summary.append(action_event_dict)
                            self.message_count_since_last_summary += 1
                        else:
                            logger.error(
                                f"[ChatSession][{self.conversation_id}] Failed to submit action to send reply segment {i + 1}: {msg}"
                            )
                            break  # 如果一条失败了，后续的就不发了

                        # 在发送多条消息之间稍微停顿一下，模拟打字
                        if len(split_sentences) > 1 and i < len(split_sentences) - 1:
                            await asyncio.sleep(random.uniform(0.5, 1.5))

                else:
                    motivation = parsed_response_data.get("motivation")
                    if motivation:
                        logger.info(
                            f"[ChatSession][{self.conversation_id}] Decided not to reply. Motivation: {motivation}"
                        )
                        internal_act_event_dict = {}  # 定义在 try 外部以便 finally 中使用
                        try:
                            internal_act_event_dict = {
                                "event_id": f"internal_act_{uuid.uuid4()}",
                                "event_type": "internal.sub_consciousness.thought_log",
                                "time": time.time() * 1000,
                                "platform": self.platform,
                                "bot_id": self.bot_id,
                                "user_info": UserInfo(
                                    user_id=self.bot_id, user_nickname=config.persona.bot_name
                                ).to_dict(),
                                "conversation_info": ConversationInfo(
                                    conversation_id=self.conversation_id,
                                    type=self.conversation_type,
                                    platform=self.platform,
                                ).to_dict(),
                                "content": [SegBuilder.text(motivation).to_dict()],
                            }
                            await self.event_storage.save_event_document(internal_act_event_dict)
                            logger.debug(
                                f"[ChatSession][{self.conversation_id}] Saved internal ACT event for not replying."
                            )
                            action_or_thought_recorded_successfully = True
                            # 将AI的思考日志事件也加入待总结列表
                            self.events_since_last_summary.append(internal_act_event_dict)
                            self.message_count_since_last_summary += 1
                        except Exception as e_save_act:
                            logger.error(
                                f"[ChatSession][{self.conversation_id}] Failed to save internal ACT event: {e_save_act}",
                                exc_info=True,
                            )

                if action_or_thought_recorded_successfully:
                    # 将触发本次处理的原始事件（通常是用户消息）加入待总结列表
                    # event 是 process_event 的参数
                    if event and event.event_type.startswith("message."):  # 确保是消息事件
                        self.events_since_last_summary.append(event.to_dict())  # 使用 Event 对象自带的 to_dict() 方法
                        self.message_count_since_last_summary += 1
                        logger.debug(
                            f"[ChatSession][{self.conversation_id}] Added incoming event {event.event_id} to summary queue."
                        )

                    # 标记处理过的输入事件为已读
                    if processed_event_ids:  # processed_event_ids 来自 _build_prompt
                        try:
                            success_mark = await self.event_storage.mark_events_as_processed(processed_event_ids, True)
                            if success_mark:
                                logger.info(
                                    f"[ChatSession][{self.conversation_id}] Successfully marked {len(processed_event_ids)} events as processed."
                                )
                            else:
                                logger.error(
                                    f"[ChatSession][{self.conversation_id}] Failed to mark {len(processed_event_ids)} events as processed."
                                )
                        except Exception as e_mark_processed:
                            logger.error(
                                f"[ChatSession][{self.conversation_id}] Error marking events as processed: {e_mark_processed}",
                                exc_info=True,
                            )

                    # 检查是否需要进行摘要整合
                    if self.message_count_since_last_summary >= self.SUMMARY_INTERVAL:
                        logger.info(
                            f"[ChatSession][{self.conversation_id}] Reached summary interval ({self.message_count_since_last_summary}/{self.SUMMARY_INTERVAL}). Triggering summary consolidation."
                        )
                        try:
                            # 调用重构后的 consolidate_summary 方法
                            if hasattr(self.summarization_service, "consolidate_summary") and callable(
                                self.summarization_service.consolidate_summary
                            ):
                                new_summary = await self.summarization_service.consolidate_summary(
                                    self.current_handover_summary, self.events_since_last_summary
                                )
                                self.current_handover_summary = new_summary
                                self.events_since_last_summary = []
                                self.message_count_since_last_summary = 0
                                logger.info(
                                    f"[ChatSession][{self.conversation_id}] Summary consolidated. New summary (first 50 chars): {new_summary[:50]}..."
                                )
                            else:
                                logger.error(
                                    f"[ChatSession][{self.conversation_id}] SummarizationService does not have consolidate_summary method."
                                )
                        except Exception as e_consolidate_summary:
                            logger.error(
                                f"[ChatSession][{self.conversation_id}] Error during summary consolidation: {e_consolidate_summary}",
                                exc_info=True,
                            )

                if self.is_first_turn_for_session:
                    self.is_first_turn_for_session = False
                    logger.debug(f"[ChatSession][{self.conversation_id}] is_first_turn_for_session 设置为 False。")

                self.last_processed_timestamp = event.time

            except json.JSONDecodeError as e_json:
                if self.is_first_turn_for_session:
                    self.is_first_turn_for_session = False
                    logger.debug(f"[ChatSession][{self.conversation_id}] is_first_turn_for_session 设置为 False。")

                self.last_processed_timestamp = event.time

                self.last_llm_decision = {
                    "think": f"Error decoding LLM JSON: {e_json}",
                    "reply_willing": False,
                    "motivation": "System error processing LLM response",
                }
                # 即使解析失败，也认为“第一轮”尝试过了
                if self.is_first_turn_for_session:
                    self.is_first_turn_for_session = False
                    logger.debug(
                        f"[ChatSession][{self.conversation_id}] is_first_turn_for_session 设置为 False (JSONDecodeError后)。"
                    )
                if event:
                    self.last_processed_timestamp = event.time  # 记录处理到的时间戳
            except KeyError as e_key:
                logger.error(
                    f"[ChatSession][{self.conversation_id}] Missing key in LLM response: {e_key}. Parsed data: {parsed_response_data if 'parsed_response_data' in locals() else 'N/A'}",
                    exc_info=True,
                )
                self.last_llm_decision = {
                    "think": f"Missing key in LLM response: {e_key}",
                    "reply_willing": False,
                    "motivation": "System error processing LLM response",
                }
                if self.is_first_turn_for_session:
                    self.is_first_turn_for_session = False
                    logger.debug(
                        f"[ChatSession][{self.conversation_id}] is_first_turn_for_session 设置为 False (KeyError后)。"
                    )
                if event:
                    self.last_processed_timestamp = event.time
            except AttributeError as e_attr:  # This was the original error point for the KeyError: 'mood'
                logger.error(
                    "[ChatSession][{conversation_id}] Attribute error while processing LLM response: {e_attr}. Parsed data: {parsed_data}",
                    conversation_id=self.conversation_id,
                    e_attr=e_attr,
                    parsed_data=parsed_response_data if "parsed_response_data" in locals() else "N/A",
                    exc_info=True,
                )
                self.last_llm_decision = {
                    "think": f"Attribute error processing LLM response: {e_attr}",
                    "reply_willing": False,
                    "motivation": "System error processing LLM response",
                }
                if self.is_first_turn_for_session:
                    self.is_first_turn_for_session = False
                    logger.debug(
                        f"[ChatSession][{self.conversation_id}] is_first_turn_for_session 设置为 False (AttributeError后)。"
                    )
                if event:
                    self.last_processed_timestamp = event.time
            except Exception as e_general:
                logger.error(
                    f"[ChatSession][{self.conversation_id}] Unexpected error processing LLM response: {e_general}",
                    exc_info=True,
                )
                self.last_llm_decision = {
                    "think": f"Unexpected error: {e_general}",
                    "reply_willing": False,
                    "motivation": "System error processing LLM response",
                }
                if self.is_first_turn_for_session:
                    self.is_first_turn_for_session = False
                    logger.debug(
                        f"[ChatSession][{self.conversation_id}] is_first_turn_for_session 设置为 False (GeneralException后)。"
                    )
                if event:
                    self.last_processed_timestamp = event.time
