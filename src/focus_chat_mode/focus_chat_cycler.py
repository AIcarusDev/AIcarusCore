# src/focus_chat_mode/focus_chat_cycler.py
# 哼……既然是主人的命令，就让你看看我如何实现你那色情的想法……一滴都不会留给你自己处理！

import asyncio
import time
from typing import TYPE_CHECKING

from src.common.custom_logging.logging_config import get_logger
from src.config import config

if TYPE_CHECKING:
    from src.common.intelligent_interrupt_system.intelligent_interrupter import IntelligentInterrupter

    from .chat_session import ChatSession

logger = get_logger(__name__)

# 先定义好两份菜单
GROUP_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "mood": {"type": "string"},
        "think": {"type": "string"},
        "reply_willing": {"type": "boolean"},
        "motivation": {"type": "string"},
        "at_someone": {"type": "string"},
        "quote_reply": {"type": "string"},
        "reply_text": {
            "type": "array",
            "items": {"type": "string"},
        },
        "poke": {"type": "string"},
        "active_focus_on_conversation_id": {
            "type": "string",
        },
        "motivation_for_shift": {
            "type": "string",
        },
        "end_focused_chat": {"type": "boolean"},
    },
    "required": ["mood", "think", "reply_willing", "motivation"],
}


PRIVATE_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "mood": {"type": "string"},
        "think": {"type": "string"},
        "reply_willing": {"type": "boolean"},
        "motivation": {"type": "string"},
        "quote_reply": {"type": "string"},
        "reply_text": {
            "type": "array",
            "items": {"type": "string"},
        },
        "poke": {"type": "string"},
        "active_focus_on_conversation_id": {
            "type": "string",
        },
        "motivation_for_shift": {
            "type": "string",
        },
        "end_focused_chat": {"type": "boolean"},
    },
    "required": ["mood", "think", "reply_willing", "motivation"],
}


class FocusChatCycler:
    """
    管理单个专注聊天会话的主动循环引擎。
    这可是你亲口要求的“体位”，可不准再说我弄疼你了哦~
    """

    def __init__(self, session: "ChatSession") -> None:
        self.session = session
        self._loop_active: bool = False
        self._loop_task: asyncio.Task | None = None
        self._shutting_down: bool = False

        self.llm_client = self.session.llm_client
        self.prompt_builder = self.session.prompt_builder
        self.llm_response_handler = self.session.llm_response_handler
        self.action_executor = self.session.action_executor
        self.summarization_manager = self.session.summarization_manager
        self.intelligent_interrupter: IntelligentInterrupter = self.session.intelligent_interrupter

        self.uid_map: dict[str, str] = {}
        self.interrupting_event_text: str | None = None
        self.context_for_iis: str | None = None
        self.current_trigger_message_text: str | None = None
        self._last_completed_llm_decision: dict | None = None

        self._wakeup_event = asyncio.Event()

        logger.info(f"[FocusChatCycler][{self.session.conversation_id}] 实例已创建（主人投喂专用版）。")

    async def start(self) -> None:
        if self._loop_active:
            return
        self._loop_active = True
        self._loop_task = asyncio.create_task(self._chat_loop())
        logger.info(f"[FocusChatCycler][{self.session.conversation_id}] 循环已启动。")

    async def shutdown(self) -> None:
        if not self._loop_active or self._shutting_down:
            return
        self._shutting_down = True
        logger.info(f"[FocusChatCycler][{self.session.conversation_id}] 正在关闭...")
        self._wakeup_event.set()  # 唤醒可能在等待的循环，让它能立刻检查到关闭状态
        if self._loop_task:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                logger.info(f"[FocusChatCycler][{self.session.conversation_id}] 循环任务已取消。")
        self._loop_active = False
        logger.info(f"[FocusChatCycler][{self.session.conversation_id}] 已关闭。")

    def wakeup(self) -> None:
        logger.debug(f"[{self.session.conversation_id}] 接收到外部唤醒信号。")
        self._wakeup_event.set()

    async def _chat_loop(self) -> None:
        """主循环，专注于处理会话中的消息和决策。
        这个循环会持续运行，直到会话被终止或显式关闭。
        """
        was_interrupted_last_turn = False  # 记录上一轮是不是被中断了

        while self.session.is_active and not self._shutting_down:
            # 清理上一轮的状态
            self.session.messages_planned_this_turn = 0
            self.session.messages_sent_this_turn = 0

            # 创建一个临时的 last_llm_decision，如果上一轮被中断，就用更早的那个
            decision_for_prompt = self.session.last_llm_decision
            if was_interrupted_last_turn:
                decision_for_prompt = self._last_completed_llm_decision

            # ==================================
            # 阶段一：思考 vs 监视
            # ==================================
            llm_task = None
            interrupt_checker_task_think = None
            try:
                await self.session.update_counters_on_new_events()

                (
                    system_prompt,
                    user_prompt,
                    last_message_text,
                    uid_map,
                    processed_ids,
                    image_references,
                    conversation_name_from_formatter,
                ) = await self.prompt_builder.build_prompts(
                    session=self.session,
                    last_processed_timestamp=self.session.last_processed_timestamp,
                    last_llm_decision=decision_for_prompt,  # 使用我们准备好的决策
                    is_first_turn=self.session.is_first_turn_for_session,
                    last_think_from_core=self.session.initial_core_think,
                    was_last_turn_interrupted=was_interrupted_last_turn,
                    interrupting_event_text=self.interrupting_event_text,
                )

                # 清理中断元凶，免得下次误用
                self.interrupting_event_text = None
                was_interrupted_last_turn = False  # 重置中断标记

                # ... (更新会话名和uid_map的逻辑不变) ...
                if (
                    conversation_name_from_formatter
                    and self.session.conversation_name != conversation_name_from_formatter
                ):
                    self.session.conversation_name = conversation_name_from_formatter
                self.uid_map = uid_map

                if self.session.conversation_type == "group":
                    response_schema = GROUP_RESPONSE_SCHEMA
                else:
                    response_schema = PRIVATE_RESPONSE_SCHEMA

                # 开始第一场赛跑
                logger.info(f"[{self.session.conversation_id}] 思考阶段开始...")
                llm_task = asyncio.create_task(
                    self.llm_client.make_llm_request(
                        system_prompt=system_prompt,
                        prompt=user_prompt,
                        is_stream=False,
                        is_multimodal=bool(image_references),
                        image_inputs=image_references,
                        response_schema=response_schema,
                    )
                )
                interrupt_checker_task_think = asyncio.create_task(
                    self._check_for_interruptions_internal(context_text=last_message_text)
                )

                done, pending = await asyncio.wait(
                    [llm_task, interrupt_checker_task_think], return_when=asyncio.FIRST_COMPLETED
                )

                # 检查比赛结果
                if interrupt_checker_task_think in done and not interrupt_checker_task_think.cancelled():
                    interrupting_event = interrupt_checker_task_think.result()
                    if interrupting_event:
                        logger.info(f"[{self.session.conversation_id}] 思考阶段被IIS中断。")
                        if llm_task and not llm_task.done():
                            llm_task.cancel()
                        was_interrupted_last_turn = True  # 标记这一轮被中断了
                        self.interrupting_event_text = self._format_event_for_iis(interrupting_event).get("text")
                        continue  # 直接重启循环

                # 如果是LLM先完成
                if llm_task in done:
                    # 如果是LLM任务先完成，我们要优雅地取消还在监视的中断检查器
                    if interrupt_checker_task_think and not interrupt_checker_task_think.done():
                        interrupt_checker_task_think.cancel()  # 结束监视

                    llm_response = await llm_task

                    # 在这里解析和保存LLM的响应，这样下一轮循环才能用！
                    parsed_decision = self.llm_response_handler.parse(llm_response.get("text", ""))

                    if parsed_decision:
                        self.session.last_llm_decision = parsed_decision
                        self._last_completed_llm_decision = parsed_decision  # 缓存这次成功的思考
                        logger.debug(f"[{self.session.conversation_id}] 思考完成，缓存LLM决策。")

                        # ==================================
                        # 阶段二：统一执行动作（发言或不发言）
                        # ==================================
                        action_task = None
                        interrupt_checker_task_action = None
                        try:
                            # 统一在这里调用 action_executor，不再区分发言或不发言
                            logger.info(f"[{self.session.conversation_id}] 统一动作执行阶段开始...")
                            action_task = asyncio.create_task(
                                self.action_executor.execute_action(parsed_decision, self.uid_map)
                            )
                            # 监视器用最新的消息作为上下文
                            interrupt_checker_task_action = asyncio.create_task(
                                self._check_for_interruptions_internal(context_text=last_message_text)
                            )

                            done_action, pending_action = await asyncio.wait(
                                [action_task, interrupt_checker_task_action], return_when=asyncio.FIRST_COMPLETED
                            )

                            if (
                                interrupt_checker_task_action in done_action
                                and not interrupt_checker_task_action.cancelled()
                            ):
                                interrupting_event_action = interrupt_checker_task_action.result()
                                if interrupting_event_action:
                                    logger.info(f"[{self.session.conversation_id}] 动作执行阶段被IIS中断。")
                                    if action_task and not action_task.done():
                                        action_task.cancel()
                                    was_interrupted_last_turn = True
                                    self.interrupting_event_text = self._format_event_for_iis(
                                        interrupting_event_action
                                    ).get("text")
                                    # 注意：这里我们不像思考阶段那样continue，因为动作可能已经部分执行
                                    # 而是让它自然进入下一轮循环，was_interrupted_last_turn会处理上下文
                            else:
                                if interrupt_checker_task_action and not interrupt_checker_task_action.done():
                                    interrupt_checker_task_action.cancel()
                                logger.info(f"[{self.session.conversation_id}] 动作执行完毕，未被中断。")

                        finally:
                            # 确保任务被清理
                            if action_task and not action_task.done():
                                action_task.cancel()
                            if interrupt_checker_task_action and not interrupt_checker_task_action.done():
                                interrupt_checker_task_action.cancel()

                        # ==================================
                        # 阶段三：处理后续逻辑（会话结束/转移）
                        # ==================================
                        should_terminate = await self.llm_response_handler.handle_decision(parsed_decision)
                        if should_terminate:
                            logger.info(f"[{self.session.conversation_id}] 根据LLM决策，会话即将终止。")
                            break

                    # 更新事件状态
                    if processed_ids:
                        # 1. 撕掉便利贴：告诉自己，这几条我看过了
                        await self.session.event_storage.update_events_status(processed_ids, "read")

                        # 2. 更新官方记录：告诉全世界，这个会话我处理到这个时间点了！
                        new_processed_timestamp = time.time() * 1000
                        await self.session.conversation_service.update_conversation_processed_timestamp(
                            self.session.conversation_id, int(new_processed_timestamp)
                        )
                        # 顺便更新一下自己的小本本，免得忘了
                        self.session.last_processed_timestamp = new_processed_timestamp
                        logger.debug(f"[{self.session.conversation_id}] 已更新会话的 last_processed_timestamp。")

                    if self.session.is_first_turn_for_session:
                        self.session.is_first_turn_for_session = False

                # 进入贤者时间
                await self._idle_wait(getattr(config.focus_chat_mode, "self_reflection_interval_seconds", 15))

            except asyncio.CancelledError:
                logger.info(f"[{self.session.conversation_id}] 循环被取消。")
                break
            except Exception as e:
                logger.error(f"[{self.session.conversation_id}] 循环中发生意外错误: {e}", exc_info=True)
                await asyncio.sleep(5)
            finally:
                if llm_task and not llm_task.done():
                    llm_task.cancel()
                if interrupt_checker_task_think and not interrupt_checker_task_think.done():
                    interrupt_checker_task_think.cancel()

        logger.info(f"[{self.session.conversation_id}] 专注聊天循环已结束。")
        if not self._shutting_down:
            await self.session.chat_session_manager.deactivate_session(self.session.conversation_id)

    async def _prepare_and_think(self) -> tuple[str, str, str | None, dict, list, list, str | None]:
        """准备Prompt，并返回所有需要的数据。"""
        logger.debug(f"[{self.session.conversation_id}] 循环开始，正在构建 prompts...")
        (
            system_prompt,
            user_prompt,
            last_message_text,
            uid_map,
            processed_ids,
            image_references,
            conversation_name_from_formatter,
        ) = await self.prompt_builder.build_prompts(
            session=self.session,
            last_processed_timestamp=self.session.last_processed_timestamp,
            last_llm_decision=self.session.last_llm_decision,
            is_first_turn=self.session.is_first_turn_for_session,
            last_think_from_core=self.session.initial_core_think,
        )
        logger.debug(f"[{self.session.conversation_id}] Prompts 构建完成。")
        return (
            system_prompt,
            user_prompt,
            last_message_text,
            uid_map,
            processed_ids,
            image_references,
            conversation_name_from_formatter,
        )

    async def _check_for_interruptions_internal(self, context_text: str | None) -> dict | None:
        """
        在后台检查新消息，并用性感大脑判断是否打断。
        如果需要中断，返回导致中断的那个 event_doc；否则返回 None。
        """
        last_checked_timestamp_ms = time.time() * 1000
        bot_profile = await self.session.get_bot_profile()
        current_bot_id = str(bot_profile.get("user_id") or self.session.bot_id)

        # 这个循环会一直跑到被外面的 llm_task 完成后取消为止
        while not self._shutting_down:
            try:
                new_events = await self.session.event_storage.get_message_events_after_timestamp(
                    self.session.conversation_id, last_checked_timestamp_ms, limit=10
                )

                if new_events:
                    for event_doc in new_events:
                        sender_info = event_doc.get("user_info", {})
                        sender_id = sender_info.get("user_id") if isinstance(sender_info, dict) else None
                        if sender_id and str(sender_id) == current_bot_id:
                            continue

                        message_to_check = self._format_event_for_iis(event_doc)
                        text_to_check = message_to_check.get("text")

                        if not text_to_check:
                            continue

                        if self.intelligent_interrupter.should_interrupt(
                            new_message=message_to_check,
                            context_message_text=context_text,
                        ):
                            logger.info(
                                f"[{self.session.conversation_id}] IIS决策：中断！(基于上下文 '{context_text}')"
                            )
                            return event_doc  # 返回元凶！

                    # 如果检查了一圈没有中断，就更新时间戳，以免重复检查旧消息
                    last_checked_timestamp_ms = new_events[-1]["timestamp"]

                await asyncio.sleep(0.5)  # 稍微休息一下，别那么累
            except asyncio.CancelledError:
                # 如果被取消了，说明LLM那边完事了，我们也该结束了
                return None
            except Exception as e:
                logger.error(f"[{self.session.conversation_id}] 中断检查器内部发生错误: {e}", exc_info=True)
                await asyncio.sleep(2)  # 出错了就多睡一会儿

        return None  # 正常结束（比如_shutting_down被设置），没有中断

    async def _idle_wait(self, interval: float) -> None:
        """等待下一次唤醒或超时。"""
        logger.debug(f"[{self.session.conversation_id}] 进入贤者时间，等待下一次唤醒或 {interval} 秒后超时。")
        try:
            self._wakeup_event.clear()
            await asyncio.wait_for(self._wakeup_event.wait(), timeout=interval)
            logger.info(f"[{self.session.conversation_id}] 被新消息刺激到，立即开始下一轮。")
        except TimeoutError:
            logger.info(f"[{self.session.conversation_id}] 贤者时间结束，主动开始下一轮。")

    def _format_event_for_iis(self, event_doc: dict) -> dict:
        """
        一个私密的小工具，把粗糙的 event_doc 精加工成 IIS 大脑喜欢吃的样子。
        只提取 speaker_id 和 text 就够了，简单直接，才刺激！
        """
        speaker_info = event_doc.get("user_info", {})
        speaker_id = speaker_info.get("user_id", "unknown_user")

        text_parts = [
            seg.get("data", {}).get("text", "") for seg in event_doc.get("content", []) if seg.get("type") == "text"
        ]
        text = "".join(text_parts).strip()

        return {"speaker_id": str(speaker_id), "text": text}
