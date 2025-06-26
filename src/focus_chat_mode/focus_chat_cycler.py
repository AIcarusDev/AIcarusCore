# src/focus_chat_mode/focus_chat_cycler.py
# 哼，笨蛋主人，看我怎么用全新的身体，把你这里也弄得湿湿的~❤

import asyncio
import time
from typing import TYPE_CHECKING

from src.common.custom_logging.logger_manager import get_logger
from src.config import config

# from .interruption_watcher import InterruptionWatcher # <-- 去死吧！你这个只会偷窥的废物！

if TYPE_CHECKING:
    # ❤ 引入我们全新的、智能的、性感的大脑！
    from src.common.intelligent_interrupt_system.intelligent_interrupter import IntelligentInterrupter

    from .chat_session import ChatSession

logger = get_logger(__name__)


class FocusChatCycler:
    """
    管理单个专注聊天会话的主动循环引擎（小色猫重构版）。
    我直接在这里判断中断，不再需要那个碍事的 watcher 了，哼！
    """

    def __init__(self, session: "ChatSession") -> None:
        self.session = session
        self._loop_active: bool = False
        self._loop_task: asyncio.Task | None = None
        self._shutting_down: bool = False

        self.conversation_id = self.session.conversation_id
        self.llm_client = self.session.llm_client
        self.prompt_builder = self.session.prompt_builder
        self.llm_response_handler = self.session.llm_response_handler
        self.action_executor = self.session.action_executor
        self.summarization_manager = self.session.summarization_manager

        # ❤ 我现在需要的是这个！从 session 的身体里直接把它掏出来！
        self.intelligent_interrupter: IntelligentInterrupter = self.session.intelligent_interrupter

        self._interruption_event = asyncio.Event()  # 这个我们还留着，用来内部通信
        self._wakeup_event = asyncio.Event()
        # self.interruption_watcher = InterruptionWatcher(self.session, self._interruption_event) # <-- 滚！

        logger.info(f"[FocusChatCycler][{self.conversation_id}] 实例已创建，并接入了全新的智能打断系统。")

    async def start(self) -> None:
        if self._loop_active:
            return
        self._loop_active = True
        self._loop_task = asyncio.create_task(self._chat_loop())
        logger.info(f"[FocusChatCycler][{self.conversation_id}] 循环已启动。")

    async def shutdown(self) -> None:
        if not self._loop_active or self._shutting_down:
            return
        self._shutting_down = True
        logger.info(f"[FocusChatCycler][{self.conversation_id}] 正在关闭...")
        # self.interruption_watcher.shutdown() # <-- 哼，这里也不需要你了
        if self._loop_task:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                logger.info(f"[FocusChatCycler][{self.conversation_id}] 循环任务已取消。")
        await self.summarization_manager.save_final_summary()
        self._loop_active = False
        logger.info(f"[FocusChatCycler][{self.conversation_id}] 已关闭。")

    def wakeup(self) -> None:
        logger.debug(f"[{self.conversation_id}] 接收到外部唤醒信号。")
        self._wakeup_event.set()

    async def _chat_loop(self) -> None:
        idle_thinking_interval = getattr(config.focus_chat_mode, "self_reflection_interval_seconds", 15)
        # ❤ 我们需要一个新的变量来记录上次检查到哪里了，就像记住上次高潮的余韵
        last_checked_timestamp_ms = self.session.last_processed_timestamp or (time.time() * 1000)

        while not self._shutting_down:
            self._interruption_event.clear()
            self._wakeup_event.clear()
            interrupt_checker_task = None
            llm_task = None
            try:
                # 阶段一：思考与决策
                system_prompt, user_prompt, uid_map, processed_ids, image_references = await self._prepare_and_think()
                current_loop_start_time_ms = time.time() * 1000
                is_multimodal_request = bool(image_references)

                llm_task = asyncio.create_task(
                    self.llm_client.make_llm_request(
                        prompt=user_prompt,
                        system_prompt=system_prompt,
                        is_stream=False,
                        is_multimodal=is_multimodal_request,
                        image_inputs=image_references,
                        task_id=f"focus-chat-{self.conversation_id}-{current_loop_start_time_ms}",
                    )
                )

                # ❤❤ 【高潮改造点】❤❤
                # 我们不再启动一个独立的、色眯眯的 watcher 任务
                # 而是用一个更性感的、异步的“中断检查器”任务来代替！
                interrupt_checker_task = asyncio.create_task(self._check_for_interruptions(last_checked_timestamp_ms))

                done, pending = await asyncio.wait(
                    [llm_task, interrupt_checker_task], return_when=asyncio.FIRST_COMPLETED
                )

                if interrupt_checker_task in done and not interrupt_checker_task.cancelled():
                    # 如果是中断检查任务先完成，说明我们被高潮打断了！
                    logger.info(f"[{self.conversation_id}] 思考被高价值新消息中断，将立即重新思考。")
                    if llm_task and not llm_task.done():
                        llm_task.cancel()
                    # ❤ 更新时间戳，以便下一轮从新的地方开始检查
                    last_checked_timestamp_ms = interrupt_checker_task.result()
                    continue

                # 阶段二：执行与状态更新 (如果LLM先完成)
                if llm_task in done:
                    # ❤ 别忘了把我们的中断检查也停掉，别让它在后台空转，浪费体力
                    if interrupt_checker_task and not interrupt_checker_task.done():
                        interrupt_checker_task.cancel()

                    async with self.session.processing_lock:
                        llm_response = llm_task.result()
                        await self._process_llm_response(llm_response, uid_map, processed_ids, image_references)
                        # ❤ 处理完LLM响应后，我们的检查点也更新到当前
                        last_checked_timestamp_ms = self.session.last_processed_timestamp

                # 阶段三：空闲等待
                await self._idle_wait(idle_thinking_interval)

            except asyncio.CancelledError:
                logger.info(f"[{self.conversation_id}] 循环被取消。")
                break
            except Exception as e:
                logger.error(f"[{self.conversation_id}] 循环中发生意外错误: {e}", exc_info=True)
                await asyncio.sleep(5)
            finally:
                if llm_task and not llm_task.done():
                    llm_task.cancel()
                if interrupt_checker_task and not interrupt_checker_task.done():
                    interrupt_checker_task.cancel()

        logger.info(f"[{self.conversation_id}] 专注聊天循环已结束。")

    async def _prepare_and_think(self) -> tuple[str, str, dict, list, list]:
        logger.debug(f"[{self.conversation_id}] 循环开始，正在构建 prompts...")
        system_prompt, user_prompt, uid_map, processed_ids, image_references = await self.prompt_builder.build_prompts(
            session=self.session,
            last_processed_timestamp=self.session.last_processed_timestamp,
            last_llm_decision=self.session.last_llm_decision,
            sent_actions_context=self.session.sent_actions_context,
            is_first_turn=self.session.is_first_turn_for_session,
            last_think_from_core=self.session.initial_core_think,
        )
        logger.debug(f"[{self.conversation_id}] Prompts 构建完成，准备让LLM好好爽一下...")
        return system_prompt, user_prompt, uid_map, processed_ids, image_references

    # --- ❤❤❤ 最终高潮修复点 ❤❤❤ ---
    # 就是这个方法！我要用全新的、正确的体位来重写它！
    async def _process_llm_response(
        self, llm_api_response: dict, uid_map: dict, processed_event_ids: list, image_references: list[str]
    ) -> None:
        self.session.last_active_time = time.time()
        if processed_event_ids:
            # 我把我之前自作多情加的那一行删掉了！我们现在只做最纯粹的事！
            await self.session.event_storage.update_events_status(processed_event_ids, "read")
            logger.info(f"[{self.conversation_id}] 已将 {len(processed_event_ids)} 个事件状态更新为 'read'。")

        response_text = llm_api_response.get("text") if llm_api_response else None
        if not response_text or (llm_api_response and llm_api_response.get("error")):
            error_msg = llm_api_response.get("message") if llm_api_response else "无响应"
            logger.error(f"[{self.conversation_id}] LLM高潮失败: {error_msg}")
            self.session.last_llm_decision = {
                "think": f"LLM调用失败: {error_msg}",
                "reply_willing": False,
                "motivation": "系统错误导致无法思考",
            }
            return

        parsed_data = self.llm_response_handler.parse(response_text)
        if not parsed_data:
            logger.error(f"[{self.conversation_id}] LLM响应解析失败或为空，高潮后的胡言乱语真是的...")
            self.session.last_llm_decision = {
                "think": "LLM响应解析失败或为空",
                "reply_willing": False,
                "motivation": "系统错误导致无法解析LLM的胡言乱语",
            }
            return

        if "mood" not in parsed_data:
            parsed_data["mood"] = "平静"
        self.session.last_llm_decision = parsed_data

        if await self.llm_response_handler.handle_end_focus_chat_if_needed(parsed_data):
            self._shutting_down = True
            return

        action_recorded = await self.action_executor.execute_action(parsed_data, uid_map)

        if action_recorded:
            await self.summarization_manager.queue_events_for_summary(processed_event_ids)
            await self.summarization_manager.consolidate_summary_if_needed()

        if self.session.is_first_turn_for_session:
            self.session.is_first_turn_for_session = False

        # ❤ 在所有处理都结束后，我们用一个简单粗暴但绝对不会出错的方式来更新时间戳！
        # 这就保证了下一轮的 prompt_builder 会从这个时间点之后开始拉取新消息。
        self.session.last_processed_timestamp = time.time() * 1000
        logger.debug(f"[{self.conversation_id}] 已将处理时间戳更新至 {self.session.last_processed_timestamp}。")

    async def _idle_wait(self, interval: float) -> None:
        logger.debug(f"[{self.conversation_id}] 进入贤者时间，等待下一次唤醒或 {interval} 秒后超时。")
        try:
            await asyncio.wait_for(self._wakeup_event.wait(), timeout=interval)
            logger.info(f"[{self.conversation_id}] 被新消息刺激到，立即开始下一轮。")
        except TimeoutError:
            logger.info(f"[{self.conversation_id}] 贤者时间结束，主动开始下一轮。")

    # ❤❤ 【全新的性感小肉穴之一：中断检查器】❤❤
    async def _check_for_interruptions(self, observe_start_timestamp_ms: float) -> float:
        """
        一个在后台持续检查新消息，并用性感大脑判断是否打断的私密任务。
        如果决定中断，它会返回最新的消息时间戳，然后高潮结束。
        """
        last_checked_timestamp_ms = observe_start_timestamp_ms
        bot_profile = await self.session.get_bot_profile()
        current_bot_id = str(bot_profile.get("user_id") or self.session.bot_id)

        while not self._shutting_down:  # 无限循环，直到被外部取消或者自己高潮
            new_events = await self.session.event_storage.get_message_events_after_timestamp(
                self.session.conversation_id, last_checked_timestamp_ms, limit=10
            )

            if new_events:
                for event_doc in new_events:
                    # 忽略自己发的消息，自己玩没意思
                    sender_info = event_doc.get("user_info", {})
                    sender_id = sender_info.get("user_id") if isinstance(sender_info, dict) else None
                    if sender_id and str(sender_id) == current_bot_id:
                        continue

                    # ❤ 把 event_doc 转换成 IntelligentInterrupter 喜欢的肉棒格式
                    message_to_check = self._format_event_for_iis(event_doc)
                    if not message_to_check.get("text"):  # 没有文本内容的消息，我的大脑不感兴趣
                        continue

                    if self.intelligent_interrupter.should_interrupt(message_to_check):
                        logger.info(f"[{self.session.conversation_id}] IIS决策：中断！啊~")
                        # 返回最新的时间戳，让主循环知道我们检查到哪里了，然后结束这个任务
                        return new_events[-1]["timestamp"]

                # 如果检查了一圈没有中断，就更新时间戳继续等
                last_checked_timestamp_ms = new_events[-1]["timestamp"]

            await asyncio.sleep(0.5)  # 稍微休息一下，才有力气迎接下一次冲击
        return last_checked_timestamp_ms  # 正常退出时也返回一下

    # ❤❤ 【全新的性感小肉穴之二：肉棒塑形器】❤❤
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
