# src/focus_chat_mode/focus_chat_cycler.py
# 哼……既然是主人的命令，就让你看看我如何实现你那色情的想法……一滴都不会留给你自己处理！

import asyncio
import re
import time
from typing import TYPE_CHECKING

from src.common.custom_logging.logging_config import get_logger
from src.config import config

if TYPE_CHECKING:
    from src.common.intelligent_interrupt_system.intelligent_interrupter import IntelligentInterrupter

    from .chat_session import ChatSession

logger = get_logger(__name__)


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

        # 哼，我在这里给自己加了个小口袋，专门装 uid_map！
        self.uid_map: dict[str, str] = {}
        # 这两个是给中断检查器用的，我把它们也放在这里
        self.context_for_iis: str | None = None
        self.current_trigger_message_text: str | None = None

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
        idle_thinking_interval = getattr(config.focus_chat_mode, "self_reflection_interval_seconds", 15)

        while self.session.is_active and not self._shutting_down:
            interrupt_checker_task = None
            llm_task = None
            try:
                # 喂！看这里！思考之前，先看看有没有人理我！
                await self.session.update_counters_on_new_events()
                (
                    system_prompt,
                    user_prompt,
                    initial_context_text,
                    uid_map,
                    processed_ids,
                    image_references,
                    conversation_name_from_formatter,
                ) = await self._prepare_and_think()

                if (
                    conversation_name_from_formatter
                    and self.session.conversation_name != conversation_name_from_formatter
                ):
                    self.session.conversation_name = conversation_name_from_formatter
                    logger.info(
                        f"[{self.session.conversation_id}] 会话名称已由Cycler更新为: '{self.session.conversation_name}'"
                    )

                self.uid_map = uid_map
                # ❤ 把这个初始上下文，同时喂给LLM（通过prompt）和中断检查器（通过成员变量）
                # 这是本轮思考触发的“因”
                self.current_trigger_message_text = initial_context_text
                # 这是中断检查器判断意外度的“锚”
                self.context_for_iis = initial_context_text
                logger.debug(f"[{self.session.conversation_id}] 本轮循环已锁定初始上下文: '{self.context_for_iis}'")

                # 2.行动分离，并行高潮
                # LLM开始它漫长的思考高潮
                llm_task = asyncio.create_task(
                    self.llm_client.make_llm_request(
                        system_prompt=system_prompt,
                        prompt=user_prompt,
                        is_stream=False,
                        is_multimodal=bool(image_references),
                        image_inputs=image_references,
                    )
                )
                # 中断检查器，用我们刚刚锁定的上下文，开始它独立的监视高潮
                interrupt_checker_task = asyncio.create_task(self._check_for_interruptions_internal())

                done, pending = await asyncio.wait(
                    [llm_task, interrupt_checker_task], return_when=asyncio.FIRST_COMPLETED
                )

                if interrupt_checker_task in done and not interrupt_checker_task.cancelled():
                    interrupted = interrupt_checker_task.result()
                    if interrupted:
                        logger.info(f"[{self.session.conversation_id}] 主循环被IIS中断，取消LLM任务。")
                        if llm_task and not llm_task.done():
                            llm_task.cancel()
                        continue  # 直接进入下一轮循环，获取全新的“初始上下文”

                if llm_task in done:
                    # 如果是LLM任务先完成，我们要优雅地取消还在监视的中断检查器
                    if interrupt_checker_task and not interrupt_checker_task.done():
                        interrupt_checker_task.cancel()

                    async with self.session.processing_lock:
                        llm_response = await llm_task

                        # 在这里解析和保存LLM的响应，这样下一轮循环才能用！
                        parsed_decision = self.llm_response_handler.parse(llm_response.get("text", ""))
                        if parsed_decision:
                            self.session.last_llm_decision = parsed_decision
                            logger.debug(
                                f"[{self.session.conversation_id}] 已将LLM决策存入 session.last_llm_decision。"
                            )
                        else:
                            # 如果解析失败，就把上一轮的记忆清空，免得用错
                            self.session.last_llm_decision = None
                            logger.warning(
                                f"[{self.session.conversation_id}] LLM响应解析失败，last_llm_decision 已清空。"
                            )

                        # 把解析好的结果传给 handle_decision，而不是原始的 llm_response
                        should_terminate = await self.llm_response_handler.handle_decision(parsed_decision or {})

                        if should_terminate:
                            logger.info(f"[{self.session.conversation_id}] 根据LLM决策或转移指令，本会话即将终止。")
                            break

                        self.session.last_active_time = time.time()
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
                            logger.debug(f"[{self.session.conversation_id}] 已将会话的 last_processed_timestamp 更新到数据库。")

                        if self.session.is_first_turn_for_session:
                            self.session.is_first_turn_for_session = False

                await self._idle_wait(idle_thinking_interval)

            except asyncio.CancelledError:
                logger.info(f"[{self.session.conversation_id}] 循环被取消。")
                break
            except Exception as e:
                logger.error(f"[{self.session.conversation_id}] 循环中发生意外错误: {e}", exc_info=True)
                await asyncio.sleep(5)
            finally:
                if llm_task and not llm_task.done():
                    llm_task.cancel()
                if interrupt_checker_task and not interrupt_checker_task.done():
                    interrupt_checker_task.cancel()

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

    async def _idle_wait(self, interval: float) -> None:
        """等待下一次唤醒或超时。"""
        logger.debug(f"[{self.session.conversation_id}] 进入贤者时间，等待下一次唤醒或 {interval} 秒后超时。")
        try:
            self._wakeup_event.clear()
            await asyncio.wait_for(self._wakeup_event.wait(), timeout=interval)
            logger.info(f"[{self.session.conversation_id}] 被新消息刺激到，立即开始下一轮。")
        except TimeoutError:
            logger.info(f"[{self.session.conversation_id}] 贤者时间结束，主动开始下一轮。")

    async def _check_for_interruptions_internal(self) -> bool:
        """
        一个在后台持续检查新消息，并用性感大脑判断是否打断的私密任务。
        它现在完全依赖循环开始时固定的 `self.context_for_iis`。
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

                        pure_text_to_check = re.sub(r"@\S+\s*", "", text_to_check).strip()
                        trigger_text = self.current_trigger_message_text or ""

                        if (
                            pure_text_to_check
                            and trigger_text
                            and (pure_text_to_check in trigger_text or trigger_text in pure_text_to_check)
                        ):
                            logger.debug(
                                f"[{self.session.conversation_id}] IIS跳过了触发思考的自身事件 (净化后包含): '{text_to_check}'"
                            )
                            continue

                        # 这就是核心判断！
                        if self.intelligent_interrupter.should_interrupt(
                            new_message=message_to_check,
                            context_message_text=self.context_for_iis,
                        ):
                            logger.info(
                                f"[{self.session.conversation_id}] IIS决策：中断！啊~ (基于初始上下文 '{self.context_for_iis}')"
                            )
                            return True  # 发现需要中断，立刻返回True

                    # 如果检查了一圈没有中断，就更新时间戳，以免重复检查旧消息
                    last_checked_timestamp_ms = new_events[-1]["timestamp"]

                await asyncio.sleep(0.5)  # 稍微休息一下，别那么累
            except asyncio.CancelledError:
                # 如果被取消了，说明LLM那边完事了，我们也该结束了
                return False
            except Exception as e:
                logger.error(f"[{self.session.conversation_id}] 中断检查器内部发生错误: {e}", exc_info=True)
                await asyncio.sleep(2)  # 出错了就多睡一会儿

        return False  # 正常结束（比如_shutting_down被设置），没有中断

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
