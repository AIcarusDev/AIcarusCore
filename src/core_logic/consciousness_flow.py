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
from src.common.utils import parse_focus_path
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

if TYPE_CHECKING:
    from src.focus_chat_mode.chat_session import ChatSession
    from src.focus_chat_mode.chat_session_manager import ChatSessionManager

logger = get_logger(__name__)


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

        if not isinstance(focus_entry, dict):
            # 兼容旧格式或'core'字符串，但确保提取出路径字符串
            focus_path_str = str(focus_entry) if focus_entry is not None else None
        else:
            focus_path_str = focus_entry.get("target_path")

        if not focus_path_str or not isinstance(focus_path_str, str):
            return None

        level, platform_id, conv_id_part = parse_focus_path(focus_path_str)

        if level != "cellular" or not platform_id or not conv_id_part:
            return None

        try:
            # 1. 将路径的会话部分 (e.g., 'group.123456') 分割成类型和ID
            # 确保 conv_id_part 确实包含 "."，否则 split 会抛出 ValueError
            if "." not in conv_id_part:
                return None

            conv_type, actual_id = conv_id_part.split(".", 1)

            # 2. 根据平台ID、类型和真实ID，重新组装出完整的实体UID
            #    这与 ChatSessionManager.sessions 字典的 key 格式完全匹配
            session_key = f"{platform_id}_{conv_type}_{actual_id}"

            # 3. 使用这个正确的 key 进行查找
            session = self.chat_session_manager.sessions.get(session_key)

            if not session:
                return None
            return session
        except (ValueError, IndexError):
            return None

    async def _core_thinking_loop(self) -> None:
        """核心思考循环.

        只负责维持循环和处理顶层异常.
        """
        # 根据配置决定使用哪种思考模式
        if config.core_logic_settings.enable_continuous_thinking:
            active_interval = config.core_logic_settings.continuous_thinking_interval_seconds
            mode_desc = f"连续思考模式 (间隔: {active_interval}s)"
        else:
            active_interval = config.core_logic_settings.thinking_interval_seconds
            mode_desc = f"标准间隔模式 (间隔: {active_interval}s)"

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
                await asyncio.sleep(10)  # // 发生严重错误时，休息一下，避免疯狂刷日志

        logger.info(f"--- {config.persona.bot_name} 的统一意识流已停止 ---")

    async def _prepare_and_run_race(self) -> None:
        """准备并执行“主任务”与“哨兵任务”的竞速."""
        main_task: asyncio.Task | None = None
        sentry_task: asyncio.Task | None = None

        try:
            race_start_timestamp = time.time() * 1000.0
            session = self._get_current_session()

            # 1. 准备竞速任务
            main_task = asyncio.create_task(self._run_full_thought_cycle(session))
            tasks_to_race: set[asyncio.Task] = {main_task}

            if session:
                sentry_task = asyncio.create_task(
                    self._listen_for_interruptions(
                        session, self._last_interrupt_context_text, race_start_timestamp
                    )
                )
                tasks_to_race.add(sentry_task)

            # 2. 开始竞速，等待第一个完成的任务
            done, pending = await asyncio.wait(tasks_to_race, return_when=asyncio.FIRST_COMPLETED)

            # 3. 处理竞速结果
            await self._handle_race_outcome(done, pending, session, main_task, sentry_task)

        finally:
            # 4. 清理任务
            if main_task and not main_task.done():
                main_task.cancel()
            if sentry_task and not sentry_task.done():
                sentry_task.cancel()

    async def _handle_race_outcome(
        self,
        done: set[asyncio.Task],
        pending: set[asyncio.Task],
        session: Optional["ChatSession"],
        main_task: asyncio.Task,
        sentry_task: asyncio.Task | None,
    ) -> None:
        """处理竞速比赛的结果."""
        # 1. 让还在跑的选手停下来
        for task in pending:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        # 2. 检查是哪个选手赢了
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

        interrupting_event_doc = (
            await sentry_task
        )  # The result of sentry_task is the interrupting event_doc
        if not interrupting_event_doc:
            logger.debug(f"[{session.conversation_id}] 哨兵任务完成，但未返回中断事件。")
            return

        logger.warning(f"[{session.conversation_id}] 中断哨兵获胜！思考-行动主任务被中断。")
        session.interruption_context = {
            "was_interrupted": True,
            "interrupting_event_doc": interrupting_event_doc,
        }

        if interrupting_ts := interrupting_event_doc.get("timestamp"):
            session.last_processed_timestamp = interrupting_ts
            logger.info(
                f"[{session.conversation_id}] 任务被中断，"
                f"全局时间戳被强制更新至中断事件的时间: {interrupting_ts}"
            )

        event_obj = Event.from_dict(interrupting_event_doc)
        self._last_interrupt_context_text = event_obj.get_text_content()
        logger.debug(
            f"[{session.conversation_id}] 已将中断消息文本 "
            f"'{self._last_interrupt_context_text[:50]}...' 烙印到短期记忆中。"
        )

        # 中断发生后，立即触发下一轮思考来处理中断事件。
        self.trigger_immediate_thought_cycle()
        logger.info(f"[{session.conversation_id}] 中断发生，已设置立即思考信号以快速响应。")

    async def _process_main_task_victory(
        self, main_task: asyncio.Task, session: Optional["ChatSession"]
    ) -> None:
        """专门处理“主任务”胜利的场景（即正常完成思考）."""
        last_processed_ts_from_task = await main_task

        if (session and last_processed_ts_from_task) and (
            last_processed_ts_from_task > session.last_processed_timestamp
        ):
            session.last_processed_timestamp = last_processed_ts_from_task
            logger.info(
                f"[{session.conversation_id}] 主任务正常完成，"
                f"全局时间戳已更新至: {last_processed_ts_from_task}"
            )

        if session:
            session.interruption_context = None

        # // 正常完成后，把记忆烙印擦掉
        self._last_interrupt_context_text = None

    async def _run_full_thought_cycle(self, session: Optional["ChatSession"]) -> float | None:
        """执行完整的思考循环，包括生成思考、处理中断和执行动作."""
        focus_entry = (
            self.chat_session_manager.current_focus_path if self.chat_session_manager else None
        )

        # 从字典条目中提取出真正的路径字符串
        focus_path_str: str | None = None
        if isinstance(focus_entry, dict):
            focus_path_str = focus_entry.get("target_path")
        elif isinstance(focus_entry, str):  # 兼容旧格式或可能的'core'字符串
            focus_path_str = focus_entry
        # 如果 focus_entry 是 None，则 focus_path_str 保持为 None

        try:
            # 在调用前，使用工具函数从 focus_path_str 解析出 level
            level, _, _ = parse_focus_path(focus_path_str)
            (
                prompt_components,
                processed_raw_events,
            ) = await self.prompt_builder.build_prompts_components(
                level=level,
                focus_path=focus_path_str,
                session=session,
                handover_result=session.pending_handover_result if session else None,
            )
        except PromptBuilderError as e:
            # 如果构建Prompt的过程中出了问题（比如 session manager 还没好）
            # 我们就在这里抓住它，打个日志，然后安静地结束这一轮思考
            logger.error(f"构建Prompt失败，中止本轮思考循环: {e}")
            return None  # 返回 None，表示本轮没有产出

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
            focus_path=focus_path_str,
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
            source_id=focus_path_str,
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
            current_focus_path=focus_path_str,
            session=session,
            processed_events_this_turn=processed_raw_events,
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
        self, session: "ChatSession", initial_context_text: str | None, start_timestamp: float
    ) -> dict | None:
        """纯粹的中断监听器（哨兵），现在通过订阅事件代理来工作."""
        subscription_queue = None
        try:
            # 1. 订阅！获取专属的消息队列
            subscription_queue = await self.interruption_broker.subscribe(session)

            context_text = initial_context_text
            bot_profile = await session.get_bot_profile()
            current_bot_id = str(bot_profile.get("user_id") or session.bot_id)

            while True:
                # 2. 等待！不再轮询，而是优雅地等待新消息被投喂
                new_event_doc = await subscription_queue.get()

                # 检查一下事件时间戳，确保不是旧消息
                if new_event_doc.get("timestamp", 0) <= start_timestamp:
                    continue

                # 3. 评估！收到新消息，交给IIS判断
                interrupting_event, new_context = self._evaluate_interrupt(
                    new_event_doc, context_text, current_bot_id, session
                )

                if interrupting_event:
                    return interrupting_event  # 发现中断，立刻返回！

                if new_context:
                    context_text = new_context  # 更新上下文，为下一次判断做准备

        except asyncio.CancelledError:
            # 主任务完成了，哨兵被取消，这是正常流程
            return None
        except Exception as e:
            logger.error(f"[{session.conversation_id}] 中断哨兵任务异常: {e}", exc_info=True)
            return None
        finally:
            # 4. 取消订阅！无论如何，都要清理资源
            if session:
                await self.interruption_broker.unsubscribe(session)

    def _evaluate_interrupt(
        self, event_doc: dict, context_text: str, current_bot_id: str, session: "ChatSession"
    ) -> tuple[dict | None, str | None]:
        """对单个事件进行中断评估的辅助函数."""
        user_info = event_doc.get("user_info") or {}
        sender_id = user_info.get("user_id")

        if sender_id and str(sender_id) == current_bot_id:
            return None, None  # 不评估自己的消息

        content_list = event_doc.get("content", []) or []
        text_content = extract_text_from_content(
            [
                Seg(type=c.get("type"), data=c.get("data", {}))
                for c in content_list
                if isinstance(c, dict)
            ]
        )

        message_to_check = {"speaker_id": str(sender_id), "text": text_content}
        if not message_to_check.get("text"):
            return None, None  # 非文本消息不触发中断

        if session.intelligent_interrupter.should_interrupt(
            new_message=message_to_check,
            context_message_text=context_text,
        ):
            logger.info(
                f"[{session.conversation_id}] IIS决策：中断！元凶ID: {event_doc.get('_key')}"
            )
            return event_doc, text_content

        return None, text_content

    async def _wait_for_next_cycle(self, interval: float) -> None:
        """等待下一个思考周期，可以被 immediate_thought_trigger 立即中断."""
        try:
            # 使用配置中传入的 interval 作为超时时间
            await asyncio.wait_for(self.immediate_thought_trigger.wait(), timeout=interval)
        except TimeoutError:
            # 这是正常情况，意味着休眠时间到了
            log_msg = (
                f"连续思考间隔到达 ({interval}s)，开始新一轮思考。"
                if config.core_logic_settings.enable_continuous_thinking
                else f"标准思考间隔到达 ({interval}s)，开始新一轮思考。"
            )
            logger.info(log_msg)
        else:
            # 这意味着是被 trigger_immediate_thought_cycle 唤醒的
            logger.info("被动思考被触发，立即开始新一轮思考。")
        finally:
            # 无论如何，清除事件，为下一次触发做准备
            if self.immediate_thought_trigger.is_set():
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
