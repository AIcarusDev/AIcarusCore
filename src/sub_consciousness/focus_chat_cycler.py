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

    async def _wait_for_new_event_or_timeout(self, timeout: int = 15) -> bool:
        """
        等待新事件或超时。
        返回: True 如果有新事件, False 如果超时。
        """
        wait_start_time = time.monotonic()
        while time.monotonic() - wait_start_time < timeout:
            if self._shutting_down:
                return False

            has_new = await self.event_storage.has_new_events_since(
                self.conversation_id, self.session.last_processed_timestamp
            )
            if has_new:
                logger.debug(f"[FocusChatCycler][{self.conversation_id}] 检测到新事件，中断等待。")
                return True

            await asyncio.sleep(1)  # 检查间隔

        logger.debug(f"[FocusChatCycler][{self.conversation_id}] 等待超时，进入下一轮思考。")
        return False

    async def _chat_loop(self) -> None:
        """专注聊天的主循环。"""
        while not self._shutting_down:
            try:
                # --- 关键改动：把耗时操作拿到锁外面！ ---
                # 1. 先把所有需要的数据都准备好，这叫“备菜”
                #    get_bot_profile() 可能会有网络IO，必须在锁外面！
                #    我们让 prompt_builder 在外面就把所有东西都构建好。
                (
                    system_prompt,
                    user_prompt,
                    uid_str_to_platform_id_map,
                    processed_event_ids,
                ) = await self.prompt_builder.build_prompts(
                    session=self.session,
                    last_processed_timestamp=self.session.last_processed_timestamp,
                    last_llm_decision=self.session.last_llm_decision,
                    sent_actions_context=self.session.sent_actions_context,
                    is_first_turn=self.session.is_first_turn_for_session,
                    last_think_from_core=self.session.initial_core_think,
                )
                
                # 2. 调用 LLM，这也是耗时操作，同样在锁外面
                logger.debug(f"构建的System Prompt:\n{system_prompt}")
                logger.debug(f"构建的User Prompt:\n{user_prompt}")
                llm_api_response = await self.llm_client.make_llm_request(
                    prompt=user_prompt, system_prompt=system_prompt, is_stream=False
                )
                response_text = llm_api_response.get("text") if llm_api_response else None

                # 3. 解析 LLM 响应，这也是纯计算，在锁外面
                parsed_response_data = self._parse_llm_response(response_text)

                # --- 现在，所有准备工作都做完了，才进入锁内去修改状态！ ---
                async with self.session.processing_lock:
                    self.session.last_active_time = time.time()

                    # 在这里处理事件状态更新，因为这会影响下一轮的 last_processed_timestamp
                    if processed_event_ids:
                        await self.event_storage.update_events_status(processed_event_ids, "read")
                        logger.info(f"已将 {len(processed_event_ids)} 个事件状态更新为 'read'。")

                    if not response_text or (llm_api_response and llm_api_response.get("error")):
                        error_msg = llm_api_response.get("message") if llm_api_response else "无响应"
                        logger.error(f"[FocusChatCycler][{self.conversation_id}] LLM调用失败或返回空: {error_msg}")
                        self.session.last_llm_decision = {
                            "think": f"LLM调用失败: {error_msg}",
                            "reply_willing": False,
                            "motivation": "系统错误导致无法思考",
                        }
                        await asyncio.sleep(5)
                        continue # continue 会自动释放锁并开始下一次循环

                    if not parsed_response_data:
                        logger.error(f"[FocusChatCycler][{self.conversation_id}] LLM响应最终解析失败或为空。")
                        self.session.last_llm_decision = {
                            "think": "LLM响应解析失败或为空",
                            "reply_willing": False,
                            "motivation": "系统错误导致无法解析LLM的胡言乱语",
                        }
                        await asyncio.sleep(5)
                        continue

                    if "mood" not in parsed_response_data:
                        parsed_response_data["mood"] = "平静"
                    self.session.last_llm_decision = parsed_response_data

                    # 检查结束和执行动作的逻辑可以放在锁内，因为它们依赖刚更新的 decision
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
                    
                    # 更新时间戳也必须在锁内，因为它是一个重要的状态
                    self.session.last_processed_timestamp = time.time() * 1000

                    # 根据决策决定下一步动作
                    should_wait = not parsed_response_data.get("reply_willing")

                # --- 锁已经释放了，现在可以安全地进行等待 ---
                if should_wait:
                    await self._wait_for_new_event_or_timeout()
                else:
                    await asyncio.sleep(1)

            except asyncio.CancelledError:
                logger.info(f"[FocusChatCycler][{self.conversation_id}] 循环被取消。")
                break
            except Exception as e:
                logger.error(f"[FocusChatCycler][{self.conversation_id}] 循环中发生意外错误: {e}", exc_info=True)
                await asyncio.sleep(5)

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
