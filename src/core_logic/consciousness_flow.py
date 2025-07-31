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
        """立即触发思考循环，唤醒主意识."""
        logger.info("接收到立即思考触发信号，主意识将被唤醒。")
        self.immediate_thought_trigger.set()

    def _get_current_session(self) -> Optional["ChatSession"]:
        """获取当前焦点会话，如果没有则返回None."""
        if not self.chat_session_manager:
            return None

        # 1. 从历史记录中获取当前的焦点条目（它现在是一个字典）
        focus_entry = self.chat_session_manager.current_focus_path

        # 2. 健壮性检查：确保它是一个字典
        if not isinstance(focus_entry, dict):
            # 如果历史记录的格式不对，这本身就是个问题
            logger.debug("当前焦点条目不是预期的字典格式，无法获取会话。")
            return None

        # 3. 从字典中提取出真正的路径字符串
        focus_path_str = focus_entry.get("target_path")
        focus_path_str = focus_entry.get("target_path")
        if not focus_path_str or not isinstance(focus_path_str, str):
            # 如果路径本身就是空的或者类型不对，直接判定无效！
            logger.debug(f"从焦点条目中获取的路径无效: {focus_path_str}，无法获取会话。")
            return None

        # 4. 使用工具函数来解析路径
        level, _, conv_id = parse_focus_path(focus_path_str)

        # 5. 严格的条件判断，确保我们只在正确的情况下查找会话
        if level != "cellular":
            # 只有在 'cellular' (会话) 层级才可能有 session 对象。
            # 如果是 'core' 或 'platform' 层，直接返回 None 是正确的行为。
            logger.debug(f"当前焦点层级为 '{level}'，不属于会话层，因此没有当前会话。")
            return None

        if not conv_id:
            # 如果路径解析出来是 'cellular' 层，但没有有效的 conv_id，说明路径格式有问题。
            logger.warning(f"焦点路径 '{focus_path_str}' 解析为会话层，但未能提取有效的会话ID。")
            return None

        # 6. 只有通过所有检查，才去会话字典里查找
        session = self.chat_session_manager.sessions.get(conv_id)
        if not session:
            # 这种情况可能发生在：会话刚刚被停用，但焦点还没来得及切换。
            logger.debug(f"根据会话ID '{conv_id}' 在当前激活的会话池中未找到实例。")
            return None

        return session

    async def _core_thinking_loop(self) -> None:
        """核心思考循环.

        只负责维持循环和处理顶层异常.
        """
        thinking_interval_sec = config.core_logic_settings.thinking_interval_seconds
        logger.info(f"=== {config.persona.bot_name} 的统一意识流【竞速模式】开始运行 ===")

        while not self.stop_event.is_set():
            try:
                # // 核心逻辑被委托给了这个新函数，主循环变得超级干净！
                await self._prepare_and_run_race()
                await self._wait_for_next_cycle(thinking_interval_sec)

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

            # 1. 准备比赛选手 (Tasks)
            main_task = asyncio.create_task(self._run_full_thought_cycle(session))
            tasks_to_race: set[asyncio.Task] = {main_task}

            if session:
                context_text = self._get_initial_context_for_sentry(session)
                sentry_task = asyncio.create_task(
                    self._listen_for_interruptions(session, context_text, race_start_timestamp)
                )
                tasks_to_race.add(sentry_task)

            # 2. 发令！开始比赛！
            done, pending = await asyncio.wait(tasks_to_race, return_when=asyncio.FIRST_COMPLETED)

            # 3. 宣布比赛结果并处理
            await self._handle_race_outcome(done, pending, session, main_task, sentry_task)

        finally:
            # // 确保无论如何，这场比赛的选手都会被妥善处理
            if main_task and not main_task.done():
                main_task.cancel()
            if sentry_task and not sentry_task.done():
                sentry_task.cancel()

    def _get_initial_context_for_sentry(self, session: "ChatSession") -> str:
        """为哨兵任务获取初始的上下文文本（记忆烙印或数据库）."""
        if self._last_interrupt_context_text:
            # // 如果有中断烙印，就用它！
            logger.info(
                f"[{session.conversation_id}] 使用了上一次中断的记忆烙印作为上下文: "
                f"'{self._last_interrupt_context_text[:50]}...'"
            )
            return self._last_interrupt_context_text
        else:
            # // 没有烙印，就返回一个默认值，让哨兵自己去查数据库
            return "..."

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
            logger.info(
                f"[{session.conversation_id}] 任务被中断，"
                f"全局时间戳被强制更新至中断事件的时间: {interrupting_ts}"
            )

        # // 将中断消息文本烙印到短期记忆中
        event_obj = Event.from_dict(interrupting_event_doc)
        self._last_interrupt_context_text = event_obj.get_text_content()
        logger.debug(
            f"[{session.conversation_id}] 已将中断消息文本 "
            f"'{self._last_interrupt_context_text}' 烙印到短期记忆中。"
        )

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
            (
                prompt_components,
                processed_raw_events,
            ) = await self.prompt_builder.build_prompts_components(
                focus_path=focus_path_str,
                session=session,
                handover_result=session.pending_handover_result if session else None,
                # TODO:这里的pending_handover_result没有任何定义，暂时不处理，等待解决。
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
        self, session: "ChatSession", initial_context_text: str, start_timestamp: float
    ) -> dict | None:
        """纯粹的中断监听器（哨兵），它现在接收一个固定的初始上下文."""
        try:
            context_text = initial_context_text
            last_checked_timestamp = start_timestamp

            bot_profile = await session.get_bot_profile()
            current_bot_id = str(bot_profile.get("user_id") or session.bot_id)

            while True:
                # 哨兵用它当前的记忆去检查新消息
                (
                    interrupting_event,
                    latest_ts_in_batch,
                    last_text_in_batch,
                ) = await self._check_for_interruptions(
                    session, context_text, last_checked_timestamp, current_bot_id
                )

                if interrupting_event:
                    return interrupting_event

                if latest_ts_in_batch:
                    last_checked_timestamp = latest_ts_in_batch

                if last_text_in_batch:
                    context_text = last_text_in_batch

                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            return None
        except Exception as e:
            logger.error(f"[{session.conversation_id}] 中断哨兵任务异常: {e}", exc_info=True)
            return None

    async def _check_for_interruptions(
        self,
        session: "ChatSession",
        context_text: str,
        since_timestamp: float,
        current_bot_id: str,
    ) -> tuple[dict | None, float | None, str | None]:
        """检查新消息是否需要中断当前思考. 现在它还会返回新消息批次中的最后一条文本."""
        new_events = await session.event_storage.get_message_events_after_timestamp(
            session.conversation_id,
            since_timestamp,
            limit=10,
            status="unread",
            exclude_user_id=current_bot_id,
        )
        if not new_events:
            return None, None, None

        latest_timestamp_in_this_batch = max(event.get("timestamp", 0.0) for event in new_events)
        last_text_content_in_batch: str | None = None
        current_context_for_this_batch = context_text

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
                context_message_text=current_context_for_this_batch,
            ):
                logger.info(
                    f"[{session.conversation_id}] IIS决策：中断！元凶ID: {event_doc.get('_key')}"
                )
                if text_content:
                    last_text_content_in_batch = text_content
                return event_doc, latest_timestamp_in_this_batch, last_text_content_in_batch

            if text_content:
                current_context_for_this_batch = text_content
                last_text_content_in_batch = text_content

        return None, latest_timestamp_in_this_batch, last_text_content_in_batch

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
