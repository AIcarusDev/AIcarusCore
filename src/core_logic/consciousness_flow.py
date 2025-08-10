# src/core_logic/consciousness_flow.py
import asyncio
import contextlib
import datetime
import threading
import time
import uuid
from typing import TYPE_CHECKING, Optional

from aicarus_protocols import Event, Seg, extract_text_from_content
from src.action.action_handler import ActionHandler
from src.common.custom_logging.logging_config import get_logger
from src.common.interruption_broker import InterruptionEventBroker
from src.common.utils import build_conversation_entity_uid, parse_focus_path
from src.config import config
from src.core_communication.core_ws_server import CoreWebsocketServer
from src.core_logic.decision_dispatcher import process_llm_decision
from src.core_logic.intrusive_thoughts import IntrusiveThoughtsGenerator
from src.core_logic.prompt_builder import PromptBuilderError, ThoughtPromptBuilder
from src.core_logic.state_manager import AIStateManager
from src.core_logic.thought_generator import ThoughtGenerator
from src.core_logic.thought_persistor import ThoughtPersistor
from src.database import ThoughtStorageService
from src.database.models import ThoughtChainDocument
from src.focus_chat_mode.components import PromptComponents

if TYPE_CHECKING:
    from src.focus_chat_mode.chat_session import ChatSession
    from src.focus_chat_mode.chat_session_manager import ChatSessionManager

logger = get_logger(__name__)


class ThoughtGenerationError(Exception):
    """Custom exception for critical failures during thought generation or persistence."""

    pass


class CoreLogic:
    """核心逻辑处理类."""

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
        interruption_broker: "InterruptionEventBroker",
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
        self.interruption_broker = interruption_broker
        self.immediate_thought_trigger = immediate_thought_trigger
        self.intrusive_generator_instance = intrusive_generator_instance
        self.thinking_loop_task: asyncio.Task | None = None
        self._last_interrupt_context_text: str | None = None
        self._last_shown_unread_summary: str | None = None
        logger.info(f"{self.__class__.__name__} 已创建 (最终完美版 V1.3)")

    def trigger_immediate_thought_cycle(self) -> None:
        """立即触发思考循环，唤醒主意识."""
        logger.info("接收到立即思考触发信号，主意识将被唤醒。")
        self.immediate_thought_trigger.set()

    def _get_current_session(self) -> Optional["ChatSession"]:
        """获取当前焦点会话，如果没有则返回None."""
        if not self.chat_session_manager:
            return None

        focus_entry = self.chat_session_manager.current_focus_path

        if not focus_entry:
            return None

        focus_path_str = (
            focus_entry.get("target_path")
            if isinstance(focus_entry, dict)
            else str(focus_entry)
            if focus_entry is not None
            else None
        )

        if not focus_path_str:
            return None

        level, platform_id, conv_id_part = parse_focus_path(focus_path_str)

        if level != "cellular" or not platform_id or not conv_id_part:
            return None

        try:
            # 在分割前，验证会话部分是否包含预期的分隔符
            if "." not in conv_id_part:
                logger.error(f"无效的焦点路径会话部分: '{conv_id_part}'。它必须是 'type.id' 格式。")
                return None

            # 1. 将路径的会话部分 (e.g., 'group.123456') 分割成类型和ID
            conv_type, actual_id = conv_id_part.split(".", 1)

            # 2. 根据平台ID、类型和真实ID，重新组装出完整的实体UID
            #    这与 ChatSessionManager.sessions 字典的 key 格式完全匹配
            session_key = build_conversation_entity_uid(platform_id, conv_type, actual_id)
            return self.chat_session_manager.sessions.get(session_key)
        except (ValueError, IndexError):
            logger.error(
                f"无法从无效的焦点路径 '{focus_path_str}' 中解析会话信息。"
                f"路径的会话部分 ('{conv_id_part}') 必须是 'type.id' 格式。"
            )
            return None

    async def _core_thinking_loop(self) -> None:
        """核心思考循环. 只负责维持循环和处理顶层异常."""
        # --- [采纳] 使用三元表达式简化赋值 ---
        is_continuous = config.core_logic_settings.enable_continuous_thinking
        active_interval = (
            config.core_logic_settings.continuous_thinking_interval_seconds
            if is_continuous
            else config.core_logic_settings.thinking_interval_seconds
        )
        mode_desc = (
            f"连续思考模式 (间隔: {active_interval}s)"
            if is_continuous
            else f"标准间隔模式 (间隔: {active_interval}s)"
        )

        logger.info(f"=== {config.persona.bot_name} 苏醒了 ({mode_desc}) ===")

        # 进入主循环，直到 stop_event 被设置
        while not self.stop_event.is_set():
            try:
                # 开始一轮思考循环
                await self._prepare_and_run_race()
                # 等待下一个思考周期的开始
                await self._wait_for_next_cycle(active_interval)

            except asyncio.CancelledError:
                logger.info("统一意识流主循环被取消。")
                break
            except Exception as e:
                logger.error(f"统一意识流主循环发生严重错误: {e}", exc_info=True)
                await asyncio.sleep(10)

        logger.info(f"--- {config.persona.bot_name} 的统一意识流已停止 ---")

    async def _prepare_and_run_race(self) -> None:
        """准备并执行“主任务”与“哨兵任务”的竞速."""
        main_task: asyncio.Task | None = None
        sentry_task: asyncio.Task | None = None
        session = self._get_current_session()
        try:
            race_start_timestamp = time.time() * 1000.0
            main_task = asyncio.create_task(self._run_full_thought_cycle(session))
            tasks_to_race: set[asyncio.Task] = {main_task}

            if session:
                sentry_task = asyncio.create_task(
                    self._listen_for_interruptions(
                        session, self._last_interrupt_context_text, race_start_timestamp
                    )
                )
                tasks_to_race.add(sentry_task)
            done, pending = await asyncio.wait(tasks_to_race, return_when=asyncio.FIRST_COMPLETED)
            await self._handle_race_outcome(done, pending, session, main_task, sentry_task)
        finally:
            for task in {main_task, sentry_task}:
                if task and not task.done():
                    task.cancel()

    async def _handle_race_outcome(
        self,
        done: set[asyncio.Task],
        pending: set[asyncio.Task],
        session: Optional["ChatSession"],
        main_task: asyncio.Task,
        sentry_task: asyncio.Task | None,
    ) -> None:
        """处理竞速比赛的结果."""
        for task in pending:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if sentry_task and sentry_task in done:
            await self._process_sentry_victory(sentry_task, session)

        if main_task in done:
            await self._process_main_task_victory(main_task, session)

    async def _process_sentry_victory(
        self, sentry_task: asyncio.Task, session: Optional["ChatSession"]
    ) -> None:
        """专门处理“哨兵”胜利的场景（即发生中断）."""
        if not session:
            return
        interrupting_event_doc = await sentry_task
        if not interrupting_event_doc:
            return

        logger.warning(f"[{session.conversation_id}] 中断哨兵获胜！思考-行动主任务被中断。")
        session.interruption_context = {
            "was_interrupted": True,
            "interrupting_event_doc": interrupting_event_doc,
        }
        if interrupting_ts := interrupting_event_doc.get("timestamp"):
            session.last_processed_timestamp = interrupting_ts
        self._last_interrupt_context_text = Event.from_dict(
            interrupting_event_doc
        ).get_text_content()
        self.trigger_immediate_thought_cycle()

    async def _process_main_task_victory(
        self, main_task: asyncio.Task, session: Optional["ChatSession"]
    ) -> None:
        """专门处理“主任务”胜利的场景（即正常完成思考）."""
        last_processed_ts_from_task = await main_task
        if (
            session
            and last_processed_ts_from_task
            and (last_processed_ts_from_task > session.last_processed_timestamp)
        ):
            session.last_processed_timestamp = last_processed_ts_from_task
        if session:
            session.interruption_context = None
        self._last_interrupt_context_text = None

    async def _generate_and_persist_thought(
        self,
        prompt_components: PromptComponents,
        focus_path_str: str | None,
    ) -> tuple[ThoughtChainDocument, str]:
        """生成思考，创建文档，并将其持久化到思想链中。失败时会引发ThoughtGenerationError."""
        system_prompt, user_prompt, response_schema = self.prompt_builder.finalize_prompts(
            prompt_components
        )
        self.prompt_builder.is_context_switch_flag = False

        generated_thought_json = await self.thought_generator.generate_thought(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            image_inputs=prompt_components.image_references,
            response_schema=response_schema,
            focus_path=focus_path_str,
        )
        if not generated_thought_json:
            raise ThoughtGenerationError("LLM未能生成有效的思考JSON。")

        action_payload = generated_thought_json.get("action") or generated_thought_json.get(
            "consciousness_control"
        )
        new_thought_pearl = ThoughtChainDocument(
            _key=str(uuid.uuid4()),
            timestamp=datetime.datetime.now(datetime.UTC).isoformat(),
            mood=generated_thought_json.get("internal_state", {}).get("mood", "平静"),
            think=generated_thought_json.get("internal_state", {}).get("think", "无"),
            intent=generated_thought_json.get("internal_state", {}).get("intent"),
            source_type="core_unified",
            source_id=focus_path_str,
            action_id=str(uuid.uuid4()) if action_payload else None,
            action_payload=generated_thought_json,
        )
        saved_key = await self.thought_storage_service.save_thought_and_link(new_thought_pearl)
        if not saved_key:
            raise ThoughtGenerationError("未能将新的思考持久化到数据库。")

        return new_thought_pearl, saved_key

    async def _run_full_thought_cycle(self, session: Optional["ChatSession"]) -> float | None:
        """执行完整的思考循环（重构后），主要负责编排."""
        focus_path_str = (
            self.chat_session_manager.current_focus_path.get("target_path")
            if self.chat_session_manager and self.chat_session_manager.current_focus_path
            else "core"
        )
        try:
            (
                prompt_components,
                processed_raw_events,
                summary_actually_shown,
            ) = await self.prompt_builder.build_prompts_components(
                level=parse_focus_path(focus_path_str)[0],
                focus_path=focus_path_str,
                session=session,
                handover_result=session.pending_handover_result if session else None,
                last_shown_core_summary=self._last_shown_unread_summary,
            )
            self._last_shown_unread_summary = summary_actually_shown
        except PromptBuilderError as e:
            logger.error(f"构建Prompt失败，中止本轮思考循环: {e}")
            return None

        if session:
            session.pending_handover_result = None

        try:
            new_thought_pearl, saved_key = await self._generate_and_persist_thought(
                prompt_components, focus_path_str
            )
        except ThoughtGenerationError as e:
            logger.error(f"核心思考过程失败，中止本轮循环: {e}")
            return None

        await process_llm_decision(
            decision_json=new_thought_pearl.action_payload,
            focus_manager=self.chat_session_manager,
            action_handler=self.action_handler_instance,
            core_logic=self,
            source_thought_key=saved_key,
            source_action_id=new_thought_pearl.action_id,
            current_focus_path=focus_path_str,
            session=session,
            processed_events_this_turn=processed_raw_events,
        )

        if processed_raw_events:
            return max(event.time for event in processed_raw_events)
        return session.last_processed_timestamp if session else None

    async def _listen_for_interruptions(
        self, session: "ChatSession", initial_context_text: str | None, start_timestamp: float
    ) -> dict | None:
        """纯粹的中断监听器（哨兵），现在通过订阅事件代理来工作."""
        subscription_queue = None
        try:
            subscription_queue = await self.interruption_broker.subscribe(session)
            context_text = initial_context_text
            bot_profile = await session.get_bot_profile()
            current_bot_id = str(bot_profile.get("user_id") or session.bot_id)

            while True:
                new_event_doc = await subscription_queue.get()
                if new_event_doc.get("timestamp", 0) <= start_timestamp:
                    continue
                interrupting_event, new_context = self._evaluate_interrupt(
                    new_event_doc, context_text, current_bot_id, session
                )
                if interrupting_event:
                    return interrupting_event
                if new_context:
                    context_text = new_context
        except asyncio.CancelledError:
            return None
        except Exception as e:
            logger.error(f"[{session.conversation_id}] 中断哨兵任务异常: {e}", exc_info=True)
            return None
        finally:
            if session:
                await self.interruption_broker.unsubscribe(session)

    def _evaluate_interrupt(
        self, event_doc: dict, context_text: str, current_bot_id: str, session: "ChatSession"
    ) -> tuple[dict | None, str | None]:
        """对单个事件进行中断评估的辅助函数."""
        sender_id = (event_doc.get("user_info") or {}).get("user_id")
        if sender_id and str(sender_id) == current_bot_id:
            return None, None

        text_content = extract_text_from_content(
            [Seg.from_dict(c) for c in (event_doc.get("content") or []) if isinstance(c, dict)]
        )
        if not text_content:
            return None, None

        message_to_check = {"speaker_id": str(sender_id), "text": text_content}
        if session.intelligent_interrupter.should_interrupt(
            new_message=message_to_check, context_message_text=context_text
        ):
            logger.info(
                f"[{session.conversation_id}] IIS决策：中断！元凶ID: {event_doc.get('_key')}"
            )
            return event_doc, text_content
        return None, text_content

    async def _wait_for_next_cycle(self, interval: float) -> None:
        """等待下一个思考周期，可以被 immediate_thought_trigger 立即中断."""
        try:
            await asyncio.wait_for(self.immediate_thought_trigger.wait(), timeout=interval)
            logger.info("被动思考被触发，立即开始新一轮思考。")
        except TimeoutError:
            pass  # 正常超时，无需记录
        finally:
            self.immediate_thought_trigger.clear()

    async def start_thinking_loop(self) -> asyncio.Task:
        """启动核心逻辑的思考循环."""
        logger.info(f"=== {config.persona.bot_name} 的大脑准备开始持续思考 ===")
        self.thinking_loop_task = asyncio.create_task(self._core_thinking_loop())
        return self.thinking_loop_task

    async def stop(self) -> None:
        """停止核心逻辑的思考循环."""
        logger.info(f"--- {config.persona.bot_name} 的意识流动正在停止 ---")
        self.stop_event.set()
        if self.thinking_loop_task and not self.thinking_loop_task.done():
            self.thinking_loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.thinking_loop_task
            logger.info("主思考循环任务已被取消。")
