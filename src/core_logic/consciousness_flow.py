# src/core_logic/consciousness_flow.py
import asyncio
import contextlib
import threading
import time
import traceback
from typing import TYPE_CHECKING, Optional

from src.action.action_handler import ActionHandler
from src.common.custom_logging.logging_config import get_logger
from src.common.interruption_broker import InterruptionEventBroker
from src.common.utils import build_conversation_entity_uid, parse_focus_path
from src.config import config
from src.core_communication.core_ws_server import CoreWebsocketServer
from src.core_logic.decision_dispatcher import process_llm_decision
from src.core_logic.intrusive_thoughts import IntrusiveThoughtsGenerator
from src.core_logic.prompt_builder import PromptBuilderError, ThoughtPromptBuilder
from src.core_logic.sanitizer import LLMOutputSanitizer
from src.core_logic.state_manager import AIStateManager
from src.core_logic.thought_generator import ThoughtGenerator
from src.core_logic.thought_persistor import ThoughtPersistor
from src.database import ThoughtStorageService
from src.database.models import ThoughtChainDocument
from src.domain.models import Stimulus
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
        self._last_interrupt_context_stimulus: Stimulus | None = None
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
                logger.error(f"统一意识流主循环发生严重错误: {e}")
                # 使用 logger.exception 会自动记录堆栈信息，同时我们手动打印以确保在控制台可见
                logger.exception("核心思考循环中发生未处理的异常。")
                traceback.print_exc()  # 这会强制将完整的错误堆栈打印到控制台
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
                    self._listen_for_interruptions(session, race_start_timestamp)
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

        # sentry_task 返回的是 Stimulus 对象
        interrupting_stimulus = await sentry_task
        if not interrupting_stimulus:
            return

        logger.warning(f"[{session.conversation_id}] 中断哨兵获胜！思考-行动主任务被中断。")

        # 上下文存储的是 Stimulus 对象，而不是原始的 event_doc
        session.interruption_context = {
            "was_interrupted": True,
            "interrupting_stimulus": interrupting_stimulus,
        }

        # 从 Stimulus 对象获取时间戳和文本内容
        if interrupting_stimulus.timestamp:
            session.last_processed_timestamp = interrupting_stimulus.timestamp
        self._last_interrupt_context_stimulus = interrupting_stimulus

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
        self._last_interrupt_context_stimulus = None

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

        # 1. 实例化修正器，传入本轮思考所需的所有上下文
        sanitizer = LLMOutputSanitizer(
            user_map=prompt_components.user_map,
            uid_str_to_platform_id_map=prompt_components.uid_str_to_platform_id_map,
        )

        # 2. 执行修正
        sanitized_thought_json = sanitizer.sanitize(generated_thought_json)

        # 3. 记录修正前后的对比，便于调试
        if generated_thought_json != sanitized_thought_json:
            logger.warning("LLM输出被拦截且修正")
            logger.debug(f"修正前: {generated_thought_json}")
            logger.debug(f"修正后: {sanitized_thought_json}")

        # 委托 ThoughtPersistor 来处理打包和存储
        saved_key, new_thought_pearl = await self.thought_persistor.store_thought(
            thought_json=sanitized_thought_json,
            source_type="core_unified",
            source_id=focus_path_str,
        )

        if not saved_key or not new_thought_pearl:
            raise ThoughtGenerationError("未能将新的思考持久化到数据库。")

        return new_thought_pearl, saved_key

    async def _run_full_thought_cycle(self, session: Optional["ChatSession"]) -> float | None:
        """执行完整的思考循环，主要负责编排."""
        focus_path_str = (
            self.chat_session_manager.current_focus_path.get("target_path")
            if self.chat_session_manager and self.chat_session_manager.current_focus_path
            else "core"
        )
        try:
            (
                prompt_components,
                processed_stimuli,
            ) = await self.prompt_builder.build_prompts_components(
                level=parse_focus_path(focus_path_str)[0],
                focus_path=focus_path_str,
                session=session,
                handover_result=session.pending_handover_result if session else None,
            )
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

        # 传递 processed_stimuli
        await process_llm_decision(
            decision_json=new_thought_pearl.action_payload,
            focus_manager=self.chat_session_manager,
            action_handler=self.action_handler_instance,
            core_logic=self,
            source_thought_key=saved_key,
            source_action_id=new_thought_pearl.action_id,
            current_focus_path=focus_path_str,
            session=session,
            processed_events_this_turn=processed_stimuli,
        )

        if processed_stimuli:
            return max(s.timestamp for s in processed_stimuli)
        return session.last_processed_timestamp if session else None

    async def _listen_for_interruptions(
        self, session: "ChatSession", start_timestamp: float
    ) -> Stimulus | None:
        """纯粹的中断监听器（哨兵），现在通过订阅事件代理来工作."""
        subscription_queue = None
        try:
            subscription_queue = await self.interruption_broker.subscribe(session)
            context_stimulus = self._last_interrupt_context_stimulus
            bot_profile = await session.get_bot_profile()
            current_bot_id = str(bot_profile.get("user_id") or session.bot_id)

            while True:
                new_stimulus = await subscription_queue.get()
                if new_stimulus.timestamp <= start_timestamp:
                    continue

                interrupting_stimulus, new_context_stimulus = self._evaluate_interrupt(
                    new_stimulus, context_stimulus, current_bot_id, session
                )
                if interrupting_stimulus:
                    return interrupting_stimulus

                if new_context_stimulus:
                    context_stimulus = new_context_stimulus
        except asyncio.CancelledError:
            return None
        except Exception as e:
            logger.error(f"[{session.conversation_id}] 中断哨兵任务异常: {e}", exc_info=True)
            return None
        finally:
            if session:
                await self.interruption_broker.unsubscribe(session)

    def _evaluate_interrupt(
        self,
        new_stimulus: Stimulus,
        context_stimulus: Stimulus | None,
        current_bot_id: str,
        session: "ChatSession",
    ) -> tuple[Stimulus | None, Stimulus | None]:
        """对单个刺激物进行中断评估的辅助函数."""
        if new_stimulus.sender_id and new_stimulus.sender_id == current_bot_id:
            return None, None

        if not new_stimulus.text_content and not new_stimulus.image_urls:
            return None, new_stimulus

        if session.intelligent_interrupter.should_interrupt(
            new_stimulus=new_stimulus, context_stimulus=context_stimulus
        ):
            logger.info(
                f"[{session.conversation_id}] IIS决策：中断！元凶事件ID: {new_stimulus.event_id}"
            )
            return new_stimulus, new_stimulus
        return None, new_stimulus

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
