# 文件: src/core_logic/consciousness_flow.py (最终完美版 V1.3)
import asyncio
import contextlib
import datetime
import threading
import uuid
from typing import TYPE_CHECKING, Optional

from aicarus_protocols import Event, Seg, extract_text_from_content
from src.action.action_handler import ActionHandler
from src.common.custom_logging.logging_config import get_logger
from src.config import config
from src.core_communication.core_ws_server import CoreWebsocketServer
from src.core_logic.decision_dispatcher import process_llm_decision
from src.core_logic.intrusive_thoughts import IntrusiveThoughtsGenerator
from src.core_logic.prompt_builder import ThoughtPromptBuilder
from src.core_logic.state_manager import AIStateManager
from src.core_logic.thought_generator import ThoughtGenerator
from src.core_logic.thought_persistor import ThoughtPersistor
from src.database import ThoughtStorageService
from src.database.models import ThoughtChainDocument

if TYPE_CHECKING:
    from src.focus_chat_mode.chat_session import ChatSession
    from src.focus_chat_mode.chat_session_manager import ChatSessionManager

logger = get_logger(__name__)


class CoreLogic:
    """核心逻辑处理类 (V2.3 - 最终完美版)。
    修复了所有已知的中断和时间戳相关的竞态问题。
    """

    def __init__(
        self,
        core_comm_layer: CoreWebsocketServer,
        action_handler_instance: ActionHandler,
        state_manager: AIStateManager,
        chat_session_manager: "ChatSessionManager",
        thought_storage_service: ThoughtStorageService,
        thought_generator: ThoughtGenerator,
        thought_persistor: ThoughtPersistor,
        prompt_builder: ThoughtPromptBuilder,
        stop_event: threading.Event,
        immediate_thought_trigger: asyncio.Event,
        intrusive_generator_instance: IntrusiveThoughtsGenerator | None = None,
    ) -> None:
        self.core_comm_layer = core_comm_layer
        self.action_handler_instance = action_handler_instance
        self.state_manager = state_manager
        self.chat_session_manager = chat_session_manager
        self.thought_generator = thought_generator
        self.thought_persistor = thought_persistor
        self.thought_storage_service = thought_storage_service
        self.prompt_builder = prompt_builder
        self.stop_event = stop_event
        self.immediate_thought_trigger = immediate_thought_trigger
        self.intrusive_generator_instance = intrusive_generator_instance
        self.thinking_loop_task: asyncio.Task | None = None
        self._last_interrupt_context_text: str | None = None
        logger.info(f"{self.__class__.__name__} 已创建 (最终完美版 V1.3)")

    def trigger_immediate_thought_cycle(self) -> None:
        logger.info("接收到立即思考触发信号，主意识将被唤醒。")
        self.immediate_thought_trigger.set()

    def _get_current_session(self) -> Optional["ChatSession"]:
        if not self.chat_session_manager:
            return None
        focus_path = self.chat_session_manager.current_focus_path
        if not focus_path or "." not in focus_path:
            return None
        conv_id = focus_path.split(".")[-1]
        return self.chat_session_manager.sessions.get(conv_id)

    async def _core_thinking_loop(self) -> None:
        thinking_interval_sec = config.core_logic_settings.thinking_interval_seconds
        logger.info(f"=== {config.persona.bot_name} 的统一意识流【竞速模式】开始运行 ===")

        while not self.stop_event.is_set():
            main_task: asyncio.Task | None = None
            sentry_task: asyncio.Task | None = None

            try:
                session = self._get_current_session()

                initial_context_text = "..."  # 默认值
                if session:
                    if self._last_interrupt_context_text:
                        # 如果有中断烙印，就用它！
                        initial_context_text = self._last_interrupt_context_text
                        logger.info(
                            f"[{session.conversation_id}] 使用了上一次中断的记忆烙印作为上下文: '{initial_context_text[:50]}...'"
                        )
                        # 用完就烧掉，避免重复使用
                        self._last_interrupt_context_text = None
                    else:
                        # 没有烙印，才去读数据库这个慢速记忆
                        initial_context_text = (
                            await self.prompt_builder.get_last_valid_text_message(
                                session.conversation_id
                            )
                            or "..."
                        )

                main_task = asyncio.create_task(self._run_full_thought_cycle(session))

                tasks_to_race = {main_task}
                if session:
                    sentry_task = asyncio.create_task(
                        self._listen_for_interruptions(session, initial_context_text)
                    )
                    tasks_to_race.add(sentry_task)

                done, pending = await asyncio.wait(
                    tasks_to_race, return_when=asyncio.FIRST_COMPLETED
                )

                for task in pending:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task

                if sentry_task and sentry_task in done:
                    interrupting_event_doc = await sentry_task
                    if interrupting_event_doc and session:
                        logger.warning(
                            f"[{session.conversation_id}] 中断哨兵获胜！思考-行动主任务被中断。"
                        )
                        session.interruption_context = {
                            "was_interrupted": True,
                            "interrupting_event_doc": interrupting_event_doc,
                        }
                        interrupting_ts = interrupting_event_doc.get("timestamp")
                        if interrupting_ts:
                            session.last_processed_timestamp = interrupting_ts
                            logger.info(
                                f"[{session.conversation_id}] 任务被中断，全局时间戳被强制更新至中断事件的时间: {interrupting_ts}"
                            )

                        # =======================【 记忆写入·将刺激烙印在海马体！】=======================
                        # 把中断消息的文本内容，直接写入我们的大脑皮层！
                        event_obj = Event.from_dict(interrupting_event_doc)
                        self._last_interrupt_context_text = event_obj.get_text_content()
                        logger.debug(
                            f"[{session.conversation_id}] 已将中断消息文本 '{self._last_interrupt_context_text}' 烙印到短期记忆中。"
                        )
                        # ======================================================================

                if main_task in done:
                    last_processed_ts_from_task = await main_task
                    if session and last_processed_ts_from_task:
                        if last_processed_ts_from_task > session.last_processed_timestamp:
                            session.last_processed_timestamp = last_processed_ts_from_task
                            logger.info(
                                f"[{session.conversation_id}] 主任务正常完成，全局时间戳已更新至: {last_processed_ts_from_task}"
                            )
                        else:
                            logger.debug(
                                f"[{session.conversation_id}] 主任务完成，但返回的时间戳不新，不更新全局时间戳。"
                            )
                    if session:
                        session.interruption_context = None

                    self._last_interrupt_context_text = None

                await self._wait_for_next_cycle(thinking_interval_sec)

            except asyncio.CancelledError:
                logger.info("统一意识流主循环被取消。")
                break
            except Exception as e:
                logger.error(f"统一意识流主循环发生严重错误: {e}", exc_info=True)
                await asyncio.sleep(10)
            finally:
                if main_task and not main_task.done():
                    main_task.cancel()
                if sentry_task and not sentry_task.done():
                    sentry_task.cancel()

        logger.info(f"--- {config.persona.bot_name} 的统一意识流已停止 ---")

    async def _run_full_thought_cycle(self, session: Optional["ChatSession"]) -> float | None:
        """执行完整的思考循环，包括生成思考、处理中断和执行动作."""
        focus_path = (
            self.chat_session_manager.current_focus_path if self.chat_session_manager else None
        )

        (
            prompt_components,
            processed_raw_events,
        ) = await self.prompt_builder.build_prompts_components(
            focus_path=focus_path,
            session=session,
            handover_result=session.pending_handover_result if session else None,
        )
        if session:
            session.pending_handover_result = None

        system_prompt, user_prompt, response_schema = self.prompt_builder.finalize_prompts(
            prompt_components
        )
        self.prompt_builder.is_context_switch_flag = False

        generated_thought_json = await self.thought_generator.generate_thought(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            image_inputs=prompt_components.image_references,
            response_schema=response_schema,
            focus_path=focus_path,
        )
        if not generated_thought_json:
            return None

        new_thought_pearl = ThoughtChainDocument(
            _key=str(uuid.uuid4()),
            timestamp=datetime.datetime.now(datetime.UTC).isoformat(),
            mood=generated_thought_json.get("internal_state", {}).get("mood", "平静"),
            think=generated_thought_json.get("internal_state", {}).get("think", "无"),
            goal=generated_thought_json.get("internal_state", {}).get("goal"),
            source_type="core_unified",
            source_id=focus_path,
            action_id=str(uuid.uuid4())
            if generated_thought_json.get("action")
            or generated_thought_json.get("consciousness_control")
            else None,
            action_payload=generated_thought_json,
        )
        saved_key = await self.thought_storage_service.save_thought_and_link(new_thought_pearl)
        if not saved_key:
            return None

        await process_llm_decision(
            decision_json=generated_thought_json,
            focus_manager=self.chat_session_manager,
            action_handler=self.action_handler_instance,
            core_logic=self,
            source_thought_key=saved_key,
            source_action_id=new_thought_pearl.action_id,
            current_focus_path=focus_path,
            # =======================【 注入春药！】=======================
            session=session,
            processed_events_this_turn=processed_raw_events,
            # =============================================================
        )

        if processed_raw_events:
            # 返回这批处理过的事件里最新的那个时间戳
            return max(event.time for event in processed_raw_events)
        elif session:
            # 如果没有新事件被处理（比如只是自我思考），也返回当前的时间戳，
            # 这样下一轮的哨兵就知道从哪里开始了
            return session.last_processed_timestamp
        return None

    async def _listen_for_interruptions(
        self, session: "ChatSession", initial_context_text: str
    ) -> dict | None:
        """纯粹的中断监听器（哨兵），它现在接收一个固定的初始上下文。"""
        try:
            # 哨兵的“记忆”在它诞生时就被决定了，就是 initial_context_text！
            context_text = initial_context_text
            last_checked_timestamp = session.last_processed_timestamp

            while True:
                # 哨兵现在用它被注入的、永不改变的初始记忆去检查新消息
                interrupting_event, latest_ts_in_batch = await self._check_for_interruptions(
                    session, context_text, last_checked_timestamp
                )

                if interrupting_event:
                    return interrupting_event

                if latest_ts_in_batch:
                    last_checked_timestamp = latest_ts_in_batch

                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            return None
        except Exception as e:
            logger.error(f"[{session.conversation_id}] 中断哨兵任务异常: {e}", exc_info=True)
            return None

    async def _check_for_interruptions(
        self, session: "ChatSession", context_text: str, since_timestamp: float
    ) -> tuple[dict | None, float | None]:
        new_events = await session.event_storage.get_message_events_after_timestamp(
            session.conversation_id,
            since_timestamp,
            limit=10,
            status="unread",
        )
        if not new_events:
            return None, None

        latest_timestamp_in_this_batch = max(event.get("timestamp", 0.0) for event in new_events)
        bot_profile = await session.get_bot_profile()
        current_bot_id = str(bot_profile.get("user_id") or session.bot_id)

        for event_doc in new_events:
            sender_id = event_doc.get("user_info", {}).get("user_id")
            if sender_id and str(sender_id) == current_bot_id:
                continue
            text_content = extract_text_from_content(
                [Seg.from_dict(c) for c in event_doc.get("content", [])]
            )
            message_to_check = {"speaker_id": str(sender_id), "text": text_content}
            if not message_to_check.get("text"):
                continue

            if session.intelligent_interrupter.should_interrupt(
                new_message=message_to_check,
                context_message_text=context_text,
            ):
                logger.info(
                    f"[{session.conversation_id}] IIS决策：中断！元凶ID: {event_doc.get('_key')}"
                )
                return event_doc, latest_timestamp_in_this_batch

        return None, latest_timestamp_in_this_batch

    async def _wait_for_next_cycle(self, interval: float) -> None:
        try:
            await asyncio.wait_for(self.immediate_thought_trigger.wait(), timeout=interval)
        except TimeoutError:
            logger.info(f"思考间隔时间到达 ({interval}s)，开始新一轮思考。")
        else:
            logger.info("被动思考被触发，立即开始新一轮思考。")
        finally:
            if self.immediate_thought_trigger.is_set():
                self.immediate_thought_trigger.clear()

    async def start_thinking_loop(self) -> asyncio.Task:
        logger.info(f"=== {config.persona.bot_name} 的大脑准备开始持续思考 ===")
        self.thinking_loop_task = asyncio.create_task(self._core_thinking_loop())
        return self.thinking_loop_task

    async def stop(self) -> None:
        logger.info(f"--- {config.persona.bot_name} 的意识流动正在停止 ---")
        self.stop_event.set()
        if self.thinking_loop_task and not self.thinking_loop_task.done():
            self.thinking_loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.thinking_loop_task
            logger.info("主思考循环任务已被取消。")
