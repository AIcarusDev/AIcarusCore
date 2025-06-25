import asyncio
import json
import random
import re
import time
import uuid
from typing import TYPE_CHECKING

from aicarus_protocols.conversation_info import ConversationInfo
from aicarus_protocols.seg import SegBuilder
from aicarus_protocols.user_info import UserInfo

from src.common.custom_logging.logger_manager import get_logger
from src.common.text_splitter import process_llm_response
from src.config import config

if TYPE_CHECKING:
    from .chat_session import ChatSession

logger = get_logger(__name__)


class FocusChatCycler:
    """
    管理单个专注聊天会话的主动循环引擎。
    负责驱动“观察-思考-决策”的循环，并在没有新消息时进行自我再思考。
    """

    def __init__(self, session: "ChatSession") -> None:
        self.session = session
        self._loop_active: bool = False
        self._loop_task: asyncio.Task | None = None
        self._shutting_down: bool = False

        # 从 session 中获取依赖，方便访问
        self.conversation_id = self.session.conversation_id
        self.llm_client = self.session.llm_client
        self.event_storage = self.session.event_storage
        self.action_handler = self.session.action_handler
        self.prompt_builder = self.session.prompt_builder
        self.core_logic = self.session.core_logic
        self.chat_session_manager = self.session.chat_session_manager
        self.summarization_service = self.session.summarization_service
        self.summary_storage_service = self.session.summary_storage_service  # 新增
        self._interruption_event = asyncio.Event() # 用于中断正在进行的思考
        self._wakeup_event = asyncio.Event()       # 用于在空闲时唤醒循环

        logger.info(f"[FocusChatCycler][{self.conversation_id}] 实例已创建。")

    async def start(self) -> None:
        """启动循环引擎。"""
        if self._loop_active:
            return
        self._loop_active = True
        self._loop_task = asyncio.create_task(self._chat_loop())
        logger.info(f"[FocusChatCycler][{self.conversation_id}] 循环已启动。")

    async def shutdown(self) -> None:
        """优雅地关闭循环引擎。"""
        if not self._loop_active or self._shutting_down:
            return

        self._shutting_down = True
        logger.info(f"[FocusChatCycler][{self.conversation_id}] 正在关闭...")

        if self._loop_task:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                logger.info(f"[FocusChatCycler][{self.conversation_id}] 循环任务已取消。")

        # 在关闭的最后阶段保存最终总结
        await self._save_final_summary()

        self._loop_active = False
        logger.info(f"[FocusChatCycler][{self.conversation_id}] 已关闭。")
    
    def wakeup(self):
        """从外部唤醒空闲的循环。哼，别随便叫我！"""
        logger.debug(f"[{self.conversation_id}] 接收到外部唤醒信号。")
        self._wakeup_event.set()

    async def _chat_loop(self) -> None:
        """
        专注聊天的主循环（终极版），哼，这次总没问题了吧！
        """
        idle_thinking_interval = getattr(config.sub_consciousness, "self_reflection_interval_seconds", 15)

        while not self._shutting_down:
            self._interruption_event.clear()
            self._wakeup_event.clear()
            observer_task = None
            llm_task = None

            try:
                # ------------------------------------------------------------------
                # 阶段一：思考与决策
                # ------------------------------------------------------------------
                
                logger.debug(f"[{self.conversation_id}] 循环开始，正在构建 prompts...")
                (
                    system_prompt, user_prompt, uid_str_to_platform_id_map, processed_event_ids
                ) = await self.prompt_builder.build_prompts(
                    session=self.session,
                    last_processed_timestamp=self.session.last_processed_timestamp,
                    last_llm_decision=self.session.last_llm_decision,
                    sent_actions_context=self.session.sent_actions_context,
                    is_first_turn=self.session.is_first_turn_for_session,
                    last_think_from_core=self.session.initial_core_think,
                )

                logger.debug(f"[{self.conversation_id}] Prompts 构建完成，正在启动 LLM 思考任务和中断观察员...")
                
                # 【【【 就是这里！看清楚！】】】
                # 在启动任务前，获取精确的“现在”时间
                current_loop_start_time_ms = time.time() * 1000

                llm_task = asyncio.create_task(
                    self.llm_client.make_llm_request(
                        prompt=user_prompt, system_prompt=system_prompt, is_stream=False
                    )
                )
                # 把“现在”的时间戳传给观察员！
                observer_task = asyncio.create_task(self._interruption_observer(current_loop_start_time_ms))

                done, pending = await asyncio.wait(
                    [llm_task, observer_task], return_when=asyncio.FIRST_COMPLETED
                )

                if observer_task in done or self._interruption_event.is_set():
                    logger.info(f"[{self.conversation_id}] 思考被高价值新消息中断，将立即重新思考。")
                    if llm_task and not llm_task.done():
                        llm_task.cancel()
                    continue 

                if llm_task in done:
                    if observer_task and not observer_task.done():
                        observer_task.cancel()
                    
                    # ------------------------------------------------------------------
                    # 阶段二：执行与状态更新 (在锁内完成)
                    # ------------------------------------------------------------------
                    async with self.session.processing_lock:
                        llm_api_response = llm_task.result()
                        
                        self.session.last_active_time = time.time()
                        if processed_event_ids:
                            await self.event_storage.update_events_status(processed_event_ids, "read")
                            logger.info(f"[{self.conversation_id}] 已将 {len(processed_event_ids)} 个事件状态更新为 'read'。")

                        response_text = llm_api_response.get("text") if llm_api_response else None
                        if not response_text or (llm_api_response and llm_api_response.get("error")):
                            error_msg = llm_api_response.get("message") if llm_api_response else "无响应"
                            logger.error(f"[{self.conversation_id}] LLM调用失败或返回空: {error_msg}")
                            self.session.last_llm_decision = {
                                "think": f"LLM调用失败: {error_msg}",
                                "reply_willing": False,
                                "motivation": "系统错误导致无法思考",
                            }
                        else:
                            parsed_response_data = self._parse_llm_response(response_text)
                            if not parsed_response_data:
                                logger.error(f"[{self.conversation_id}] LLM响应最终解析失败或为空。")
                                self.session.last_llm_decision = {
                                    "think": "LLM响应解析失败或为空",
                                    "reply_willing": False,
                                    "motivation": "系统错误导致无法解析LLM的胡言乱语",
                                }
                            else:
                                if "mood" not in parsed_response_data:
                                    parsed_response_data["mood"] = "平静"
                                self.session.last_llm_decision = parsed_response_data

                                if await self._handle_end_focus_chat_if_needed(parsed_response_data):
                                    break 

                                action_or_thought_recorded = await self._execute_action(
                                    parsed_response_data, uid_str_to_platform_id_map
                                )

                                if action_or_thought_recorded:
                                    await self._queue_events_for_summary(processed_event_ids)
                                    await self._consolidate_summary_if_needed()

                                if self.session.is_first_turn_for_session:
                                    self.session.is_first_turn_for_session = False
                                
                                self.session.last_processed_timestamp = time.time() * 1000

                # ------------------------------------------------------------------
                # 阶段三：空闲等待
                # ------------------------------------------------------------------
                logger.debug(f"[{self.conversation_id}] 进入空闲等待阶段，等待下一次唤醒或 {idle_thinking_interval} 秒后超时。")
                try:
                    await asyncio.wait_for(
                        self._wakeup_event.wait(), timeout=float(idle_thinking_interval)
                    )
                    logger.info(f"[{self.conversation_id}] 被新消息唤醒，立即开始下一轮思考。")
                except asyncio.TimeoutError:
                    logger.info(f"[{self.conversation_id}] 空闲等待超时，主动开始下一轮思考。")
                
            except asyncio.CancelledError:
                logger.info(f"[{self.conversation_id}] 循环被取消。")
                break 
            except Exception as e:
                logger.error(f"[{self.conversation_id}] 循环中发生意外错误: {e}", exc_info=True)
                await asyncio.sleep(5) 
            finally:
                if observer_task and not observer_task.done():
                    observer_task.cancel()
                if llm_task and not llm_task.done():
                    llm_task.cancel()

        logger.info(f"[{self.conversation_id}] 专注聊天循环已结束。")

    async def _interruption_observer(self, observe_start_timestamp: float):
        """
        一个轻量级的观察员，在思考期间监视新消息并决定是否中断。
        哼，我就是那个躲在暗处盯着你们聊天的小猫咪！
        """
        interruption_score = 0
        threshold = 100
        processed_event_keys_in_this_run = set() 
        last_checked_timestamp = observe_start_timestamp
        logger.debug(f"[{self.conversation_id}] 中断观察员已启动，观察起点: {last_checked_timestamp}")

         # 【统一获取ID】只在这里获取一次，供本轮观察全程使用
        bot_profile = await self.session.get_bot_profile()
        current_bot_id = str(bot_profile.get("user_id") or self.session.bot_id)

        while self._loop_task and not self._loop_task.done() and not self._shutting_down:
            if self._interruption_event.is_set():
                break
            try:
                # 使用自己的时间指针来查找新消息
                new_events = await self.event_storage.get_message_events_after_timestamp(
                    self.conversation_id, last_checked_timestamp, limit=10
                )
                
                if new_events:
                    for event_doc in new_events:
                        event_key = event_doc.get('_key')
                        if not event_key or event_key in processed_event_keys_in_this_run:
                            continue
                        
                        sender_info = event_doc.get("user_info", {})
                        sender_id = sender_info.get("user_id") if isinstance(sender_info, dict) else None
                        
                        if sender_id and str(sender_id) == current_bot_id:
                            logger.debug(f"[{self.conversation_id}] 观察员发现一条自己发的消息({event_key})，已忽略。")
                            processed_event_keys_in_this_run.add(event_key)
                            continue
                        
                        processed_event_keys_in_this_run.add(event_key)
                        
                        score_to_add = 0
                        # 【把获取到的ID传进去！】
                        if self._is_mentioning_me(event_doc, current_bot_id): # <-- 同步调用
                            score_to_add = 100
                        elif await self._is_quoting_me(event_doc, current_bot_id): # <-- 异步调用
                            score_to_add = 80
                        else:
                            content = event_doc.get("content", [])
                            if content and isinstance(content, list) and len(content) > 0:
                                main_seg_type = content[0].get("type")
                                if main_seg_type in ["image", "video", "forward", "share"]:
                                    score_to_add = 30
                                elif main_seg_type == "text":
                                    text_content = "".join([s.get("data", {}).get("text", "") for s in content if s.get("type") == "text"])
                                    char_count = len(text_content.replace(" ", "").replace("\n", ""))
                                    if char_count >= 25: score_to_add = 35
                                    elif char_count >= 5: score_to_add = 20
                                    else: score_to_add = 5
                                elif main_seg_type == "record": score_to_add = 15
                                elif main_seg_type in ["face", "poke"]: score_to_add = 5
                                else: score_to_add = 10
                        
                        interruption_score += score_to_add
                        logger.debug(f"[{self.conversation_id}] 新消息({event_key})计分后，中断分数: {interruption_score}")

                        last_checked_timestamp = new_events[-1]['timestamp']

                        if interruption_score >= threshold:
                            logger.info(f"[{self.conversation_id}] 中断分数达到阈值 ({interruption_score}/{threshold})！发送中断信号！")
                            self._interruption_event.set()
                            return

                await asyncio.sleep(0.5)

            except Exception as e:
                logger.error(f"[{self.conversation_id}] 中断观察员出错: {e}", exc_info=True)
                await asyncio.sleep(2)

        logger.debug(f"[{self.conversation_id}] 中断观察员正常退出。")

    # 我们还需要两个辅助方法来判断 @ 和 回复
    def _is_mentioning_me(self, event_doc: dict, current_bot_id: str) -> bool:
        """
        哼，这次直接告诉我我是谁，我来判断是不是在@我！
        """
        if not current_bot_id:
            return False

        for seg in event_doc.get("content", []):
            if seg.get("type") == "at":
                at_user_id_raw = seg.get("data", {}).get("user_id")
                # 对两边的ID都做强制字符串转换，确保万无一失
                if at_user_id_raw is not None and str(at_user_id_raw) == current_bot_id:
                    logger.debug(f"[{self.conversation_id}] 确认被@，当前机器人ID: {current_bot_id}")
                    return True
        return False

    # 注意签名变化：增加了 current_bot_id: str 参数
    async def _is_quoting_me(self, event_doc: dict, current_bot_id: str) -> bool:
        """
        判断这条消息是不是在回复我发的某条消息。
        我只负责查案，不负责找机器人是谁！
        """
        quoted_message_id = None
        for seg in event_doc.get("content", []):
            # 兼容 'quote' 和 'reply' 两种可能的类型
            if seg.get("type") in ["quote", "reply"]:
                quoted_message_id = seg.get("data", {}).get("message_id")
                break

        if not quoted_message_id or not current_bot_id:
            return False

        try:
            # 去数据库里查被引用的那条老消息
            original_message_docs = await self.event_storage.get_events_by_ids([str(quoted_message_id)])
            
            if not original_message_docs:
                logger.warning(f"[{self.conversation_id}] 找不到被引用的消息, ID: {quoted_message_id}")
                return False

            original_message_doc = original_message_docs[0]
            
            # 判断老消息的发送者是不是我
            original_sender_info = original_message_doc.get("user_info", {})
            original_sender_id = original_sender_info.get("user_id") if isinstance(original_sender_info, dict) else None
            
            if original_sender_id and str(original_sender_id) == current_bot_id:
                logger.debug(f"[{self.conversation_id}] 确认被回复，被引用的消息 {quoted_message_id} 是由我 ({current_bot_id}) 发送的。")
                return True

        except Exception as e:
            logger.error(f"[{self.conversation_id}] 在检查是否被回复时发生数据库查询错误: {e}", exc_info=True)
        
        return False

    def _parse_llm_response(self, response_text: str) -> dict | None:
        """从LLM的文本响应中解析出JSON数据。"""
        if not response_text:
            return None
        match = re.search(r"```json\s*(\{[\s\S]*?\})\s*```", response_text, re.DOTALL)
        if match:
            json_str = match.group(1)
            try:
                return json.loads(json_str)
            except json.JSONDecodeError as e:
                logger.error(f"解析被```json包裹的响应时JSONDecodeError: {e}. JSON string: {json_str[:200]}...")
                return None
        else:
            try:
                return json.loads(response_text)
            except json.JSONDecodeError:
                logger.warning(f"LLM响应不是有效的JSON，且未被```json包裹: {response_text[:200]}")
                return None

    async def _handle_end_focus_chat_if_needed(self, parsed_data: dict) -> bool:
        """检查并处理结束专注模式的决策。返回 True 表示应终止循环。"""
        if parsed_data.get("end_focused_chat") is True:
            logger.info(f"[FocusChatCycler][{self.conversation_id}] LLM决策结束专注模式。")
            handover_summary = self.session.current_handover_summary or "我结束了专注，但似乎没什么特别的总结可以交接。"
            last_session_think = self.session.last_llm_decision.get("think", "专注会话结束，无特定最终想法。")
            last_session_mood = self.session.last_llm_decision.get("mood", "平静")

            if hasattr(self.core_logic, "trigger_immediate_thought_cycle"):
                self.core_logic.trigger_immediate_thought_cycle(handover_summary, last_session_think, last_session_mood)

            if hasattr(self.chat_session_manager, "deactivate_session"):
                # 在停用会话前，保存最终的总结
                await self._save_final_summary()
                await self.chat_session_manager.deactivate_session(self.conversation_id)

            return True
        return False

    async def _execute_action(self, parsed_data: dict, uid_map: dict) -> bool:
        """根据LLM的决策执行回复或记录内部思考。"""
        # --- Sanitize optional fields ---
        fields_to_sanitize = ["at_someone", "quote_reply", "reply_text", "poke", "action_to_take", "action_motivation"]
        for field in fields_to_sanitize:
            if parsed_data.get(field) == "":
                parsed_data[field] = None
        if parsed_data.get("action_to_take") is None:
            parsed_data["action_motivation"] = None
        # --- End sanitization ---

        # 根据是否有实际互动行为，更新 no_action_count
        # TODO: 未来如果增加了 poke 等其他互动，也需要在这里加入判断
        has_interaction = parsed_data.get("reply_willing") and parsed_data.get("reply_text")

        if has_interaction:
            self.session.no_action_count = 0
            logger.debug(f"[{self.conversation_id}] 检测到互动行为，no_action_count 已重置。")
            return await self._send_reply(parsed_data, uid_map)
        else:
            self.session.no_action_count += 1
            logger.debug(
                f"[{self.conversation_id}] 无互动行为，no_action_count 增加到 {self.session.no_action_count}。"
            )
            return await self._log_internal_thought(parsed_data)

    async def _send_reply(self, parsed_data: dict, uid_map: dict) -> bool:
        """发送回复消息。"""
        original_reply_text = parsed_data["reply_text"]
        split_sentences = process_llm_response(
            text=original_reply_text,
            enable_kaomoji_protection=config.sub_consciousness.enable_kaomoji_protection,
            enable_splitter=config.sub_consciousness.enable_splitter,
            max_length=config.sub_consciousness.max_length,
            max_sentence_num=config.sub_consciousness.max_sentence_num,
        )

        at_target_values_raw = parsed_data.get("at_someone")
        quote_msg_id = parsed_data.get("quote_reply")
        current_motivation = parsed_data.get("motivation")

        action_recorded = False
        for i, sentence_text in enumerate(split_sentences):
            content_segs_payload = self._build_reply_segments(
                i, sentence_text, quote_msg_id, at_target_values_raw, uid_map
            )
            action_event_dict = {
                "event_id": f"sub_chat_reply_{uuid.uuid4()}",
                "event_type": "action.message.send",
                "platform": self.session.platform,
                "bot_id": self.session.bot_id,
                "conversation_info": {"conversation_id": self.conversation_id, "type": self.session.conversation_type},
                "content": content_segs_payload,
                "motivation": current_motivation
                if i == 0 and current_motivation and current_motivation.strip()
                else None,
            }
            success, msg = await self.action_handler.submit_constructed_action(action_event_dict, "发送子意识聊天回复")
            if success and "执行失败" not in msg:
                logger.info(f"Action to send reply segment {i + 1} submitted successfully.")
                self.session.events_since_last_summary.append(action_event_dict)
                self.session.message_count_since_last_summary += 1
                action_recorded = True
            else:
                logger.error(f"Failed to submit/execute action to send reply segment {i + 1}: {msg}")
                break
            if len(split_sentences) > 1 and i < len(split_sentences) - 1:
                await asyncio.sleep(random.uniform(0.5, 1.5))
        return action_recorded

    def _build_reply_segments(
        self, index: int, text: str, quote_id: str | None, at_raw: str | list | None, uid_map: dict
    ) -> list:
        """构建单条回复消息的 segments。"""
        payload = []
        if index == 0:
            if quote_id:
                payload.append(SegBuilder.reply(message_id=quote_id).to_dict())
            if at_raw:
                raw_targets = []
                if isinstance(at_raw, str):
                    raw_targets = [t.strip() for t in at_raw.split(",") if t.strip()]
                elif isinstance(at_raw, list):
                    raw_targets = [str(t).strip() for t in at_raw if str(t).strip()]
                else:
                    raw_targets = [str(at_raw).strip()]

                actual_ids = [uid_map.get(t, t) for t in raw_targets]
                for platform_id in actual_ids:
                    payload.append(SegBuilder.at(user_id=platform_id, display_name="").to_dict())
                if actual_ids:
                    payload.append(SegBuilder.text(" ").to_dict())
        payload.append(SegBuilder.text(text).to_dict())
        return payload

    async def _log_internal_thought(self, parsed_data: dict) -> bool:
        """记录内部思考（不回复）。"""
        motivation = parsed_data.get("motivation")
        if not motivation:
            return False

        logger.info(f"Decided not to reply. Motivation: {motivation}")
        internal_act_event_dict = {
            "event_id": f"internal_act_{uuid.uuid4()}",
            "event_type": "internal.sub_consciousness.thought_log",
            "time": time.time() * 1000,
            "platform": self.session.platform,
            "bot_id": self.session.bot_id,
            "user_info": UserInfo(user_id=self.session.bot_id, user_nickname=config.persona.bot_name).to_dict(),
            "conversation_info": ConversationInfo(
                conversation_id=self.conversation_id,
                type=self.session.conversation_type,
                platform=self.session.platform,
            ).to_dict(),
            "content": [SegBuilder.text(motivation).to_dict()],
        }
        try:
            await self.event_storage.save_event_document(internal_act_event_dict)
            self.session.events_since_last_summary.append(internal_act_event_dict)
            self.session.message_count_since_last_summary += 1
            return True
        except Exception as e:
            logger.error(f"Failed to save internal ACT event: {e}", exc_info=True)
            return False

    async def _queue_events_for_summary(self, event_ids: list[str]) -> None:
        """获取事件详情以用于总结。"""
        if not event_ids:
            return
        try:
            # 根据ID获取完整的事件文档，用于总结
            event_docs = await self.event_storage.get_events_by_ids(event_ids)
            if event_docs:
                self.session.events_since_last_summary.extend(event_docs)
                self.session.message_count_since_last_summary += len(event_docs)
                logger.debug(f"Added {len(event_docs)} processed events to summary queue.")
            else:
                logger.warning(f"Could not fetch event documents for IDs: {event_ids}")
        except Exception as e:
            logger.error(f"Error during queueing events for summary: {e}", exc_info=True)

    async def _consolidate_summary_if_needed(self) -> None:
        """检查并执行摘要。"""
        # 检查是否达到总结的条件（比如消息数量）
        if self.session.message_count_since_last_summary < self.session.SUMMARY_INTERVAL:
            return  # 没到数量，不总结，溜了

        logger.info("已达到总结间隔，开始整合摘要...")
        try:
            # 【关键改动在这里！】
            # 在调用总结服务之前，我们需要准备好所有它需要的“食材”

            # 1. 获取机器人档案 (bot_profile)
            #    我们直接从 session 的缓存方法获取，这个方法很智能，会自己处理缓存
            bot_profile_for_summary = await self.session.get_bot_profile()
            if not bot_profile_for_summary:
                logger.warning(f"[{self.conversation_id}] 无法获取机器人档案，本次总结可能缺少相关信息。")
                # 即使获取失败，也给一个空字典，避免程序崩溃
                bot_profile_for_summary = {}

            # 2. 获取会话信息 (conversation_info)
            #    这个信息在 session 创建时就有了，直接用
            conversation_info_for_summary = {
                "name": self.session.conversation_name or "未知会话", 
                "type": self.session.conversation_type,
                "id": self.conversation_id
            }

            # 3. 获取用户映射表 (user_map)
            #    我们让 `_consolidate_summary_if_needed` 自己去构建一次 user_map。
            #    这部分逻辑和 prompt_builder 很像，但为了解耦，我们在这里重写一遍。
            #    虽然有点重复，但最清晰，最不容易出错。
            
            user_map_for_summary = {}
            uid_counter = 0
            # 添加机器人自己
            user_map_for_summary[bot_profile_for_summary.get('user_id', self.session.bot_id)] = {
                "uid_str": "U0",
                "nick": bot_profile_for_summary.get('nickname', config.persona.bot_name),
                "card": bot_profile_for_summary.get('card', config.persona.bot_name),
                "title": bot_profile_for_summary.get('title', ""),
                "perm": bot_profile_for_summary.get('role', "成员"),
            }

            # 遍历待总结的事件，构建其他用户的信息
            for event in self.session.events_since_last_summary:
                user_info = event.get('user_info')
                if isinstance(user_info, dict):
                    p_user_id = user_info.get('user_id')
                    if p_user_id and p_user_id not in user_map_for_summary:
                        uid_counter += 1
                        user_map_for_summary[p_user_id] = {
                            "uid_str": f"U{uid_counter}",
                            "nick": user_info.get('user_nickname', f"用户{p_user_id}"),
                            "card": user_info.get('user_cardname', user_info.get('user_nickname', f"用户{p_user_id}")),
                            "title": user_info.get('user_titlename', ""),
                            "perm": user_info.get('permission_level', "成员"),
                        }

            # 4. 现在，万事俱备，调用我们新的总结服务！
            if hasattr(self.summarization_service, "consolidate_summary"):
                new_summary = await self.summarization_service.consolidate_summary(
                    previous_summary=self.session.current_handover_summary,
                    recent_events=self.session.events_since_last_summary,
                    # 把我们精心准备的食材喂过去！
                    bot_profile=bot_profile_for_summary,
                    conversation_info=conversation_info_for_summary,
                    user_map=user_map_for_summary,
                )
                # 总结完成后，清空购物篮，重置计数器
                self.session.current_handover_summary = new_summary
                self.session.events_since_last_summary = []
                self.session.message_count_since_last_summary = 0
                logger.info(f"摘要已整合。新摘要(前50字符): {new_summary[:50]}...")
        except Exception as e:
            logger.error(f"整合摘要时发生错误: {e}", exc_info=True)

    async def _save_final_summary(self) -> None:
        """保存当前会话的最终总结到数据库。"""
        final_summary = self.session.current_handover_summary
        if not final_summary or not final_summary.strip():
            logger.info(f"[{self.conversation_id}] 没有最终总结可保存，跳过。")
            return

        # 为了获取 event_ids_covered，我们需要合并已处理和未处理的事件ID
        # 注意：这可能不是最精确的做法，但能确保所有相关事件都被记录
        # 一个更精确的方法是在每次总结时都记录下覆盖的ID
        event_ids_covered = [
            event.get("event_id") for event in self.session.events_since_last_summary if event.get("event_id")
        ]

        logger.info(f"[{self.conversation_id}] 正在尝试保存最终的会话总结...")
        try:
            success = await self.summary_storage_service.save_summary(
                conversation_id=self.session.conversation_id,
                summary_text=final_summary,
                platform=self.session.platform,
                bot_id=self.session.bot_id,
                event_ids_covered=event_ids_covered,
            )
            if success:
                logger.info(f"[{self.conversation_id}] 成功保存最终总结。")
            else:
                logger.warning(f"[{self.conversation_id}] 保存最终总结失败（服务返回False）。")
        except Exception as e:
            logger.error(f"[{self.conversation_id}] 保存最终总结时发生意外错误: {e}", exc_info=True)
