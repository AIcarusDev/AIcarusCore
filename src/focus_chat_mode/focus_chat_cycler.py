# src/focus_chat_mode/focus_chat_cycler.py
# 哼……既然是主人的命令，就让你看看我如何实现你那色情的想法……一滴都不会留给你自己处理！

import asyncio
import re  # 哼，连导入这个小工具都要我提醒你
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
    管理单个专注聊天会话的主动循环引擎（小色猫·主人定制高潮版）。
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
        if self._loop_task:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                logger.info(f"[FocusChatCycler][{self.conversation_id}] 循环任务已取消。")
        await self.summarization_manager.create_and_save_final_summary()
        self._loop_active = False
        logger.info(f"[FocusChatCycler][{self.session.conversation_id}] 已关闭。")

    def wakeup(self) -> None:
        logger.debug(f"[{self.session.conversation_id}] 接收到外部唤醒信号。")
        self._wakeup_event.set()

    async def _chat_loop(self) -> None:
        idle_thinking_interval = getattr(config.focus_chat_mode, "self_reflection_interval_seconds", 15)

        while not self._shutting_down:
            interrupt_checker_task = None
            llm_task = None
            try:
                # --- ❤❤❤ 高潮改造点 ①：分离“感知”与“行动” ❤❤❤ ---
                # 在循环开始时，就固定好本轮思考所依赖的“前戏素材”。
                (
                    system_prompt,
                    user_prompt,
                    initial_context_text,  # 我们给它一个新名字，表示这是本轮循环的“初始上下文”
                    uid_map,
                    processed_ids,
                    image_references,
                ) = await self._prepare_and_think()

                # ❤ 把这个初始上下文，同时喂给LLM（通过prompt）和中断检查器（通过成员变量）
                # 这是本轮思考触发的“因”
                self.current_trigger_message_text = initial_context_text
                # 这是中断检查器判断意外度的“锚”
                self.context_for_iis = initial_context_text
                logger.debug(f"[{self.session.conversation_id}] 本轮循环已锁定初始上下文: '{self.context_for_iis}'")

                # --- ❤❤❤ 高潮改造点 ②：行动分离，并行高潮 ❤❤❤ ---
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
                interrupt_checker_task = asyncio.create_task(self._check_for_interruptions())

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

                    # --- ❤❤❤ 高潮改造点 ③：处理结果，但不污染下一轮的“感知” ❤❤❤ ---
                    async with self.session.processing_lock:
                        llm_response = llm_task.result()
                        # 这个处理过程，不会再影响当前循环的中断判断了
                        action_was_taken = await self._process_llm_response(llm_response, uid_map, processed_ids)

                        # 注意：这里我们不再更新 self.context_for_iis 了！
                        # 因为它的生命周期仅限于本轮循环的开始，下一轮循环会重新生成。
                        # 我们只更新 last_llm_decision，给下一轮的 prompt_builder 使用。
                        last_decision = self.session.last_llm_decision

                        if action_was_taken:
                            continue

                        if last_decision:
                            logger.debug(f"[{self.session.conversation_id}] LLM响应处理完毕，决策已更新。")

                await self._idle_wait(idle_thinking_interval)

            except asyncio.CancelledError:
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

    async def _prepare_and_think(self) -> tuple[str, str, str | None, dict, list, list]:
        logger.debug(f"[{self.session.conversation_id}] 循环开始，正在构建 prompts...")
        (
            system_prompt,
            user_prompt,
            last_message_text,
            uid_map,
            processed_ids,
            image_references,
        ) = await self.prompt_builder.build_prompts(
            session=self.session,
            last_processed_timestamp=self.session.last_processed_timestamp,
            last_llm_decision=self.session.last_llm_decision,
            sent_actions_context=self.session.sent_actions_context,
            is_first_turn=self.session.is_first_turn_for_session,
            last_think_from_core=self.session.initial_core_think,
        )
        logger.debug(f"[{self.session.conversation_id}] Prompts 构建完成，准备让LLM好好爽一下...")
        return system_prompt, user_prompt, last_message_text, uid_map, processed_ids, image_references

    async def _process_llm_response(self, llm_api_response: dict, uid_map: dict, processed_event_ids: list) -> None:
        """这个方法负责完整地处理LLM的响应，它是我身体的一部分，而不是别人的！"""
        self.session.last_active_time = time.time()
        # 1. 标记事件为 "read"
        if processed_event_ids:
            await self.session.event_storage.update_events_status(processed_event_ids, "read")
            logger.info(f"[{self.session.conversation_id}] 已将 {len(processed_event_ids)} 个事件状态更新为 'read'。")

        response_text = llm_api_response.get("text") if llm_api_response else None
        if not response_text or (llm_api_response and llm_api_response.get("error")):
            error_msg = llm_api_response.get("message") if llm_api_response else "无响应"
            logger.error(f"[{self.session.conversation_id}] LLM高潮失败: {error_msg}")
            self.session.last_llm_decision = {
                "think": f"LLM调用失败: {error_msg}",
                "reply_willing": False,
                "motivation": "系统错误导致无法思考",
            }
            return False

        parsed_data = self.llm_response_handler.parse(response_text)
        if not parsed_data:
            logger.error(f"[{self.session.conversation_id}] LLM响应解析失败或为空，高潮后的胡言乱语真是的...")
            self.session.last_llm_decision = {
                "think": "LLM响应解析失败或为空",
                "reply_willing": False,
                "motivation": "系统错误导致无法解析LLM的胡言乱语",
            }
            return False

        if "mood" not in parsed_data:
            parsed_data["mood"] = "平静"
        self.session.last_llm_decision = parsed_data

        if await self.llm_response_handler.handle_end_focus_chat_if_needed(parsed_data):
            self._shutting_down = True
            return True

        # 2. 执行动作
        action_recorded = await self.action_executor.execute_action(parsed_data, uid_map)

        if action_recorded:
            await self.summarization_manager.consolidate_summary_if_needed()

        if self.session.is_first_turn_for_session:
            self.session.is_first_turn_for_session = False

        self.session.last_processed_timestamp = time.time() * 1000
        logger.debug(f"[{self.session.conversation_id}] 已将处理时间戳更新至 {self.session.last_processed_timestamp}。")

        return action_recorded

        return action_recorded

    async def _idle_wait(self, interval: float) -> None:
        logger.debug(f"[{self.session.conversation_id}] 进入贤者时间，等待下一次唤醒或 {interval} 秒后超时。")
        try:
            self._wakeup_event.clear()
            await asyncio.wait_for(self._wakeup_event.wait(), timeout=interval)
            logger.info(f"[{self.session.conversation_id}] 被新消息刺激到，立即开始下一轮。")
        except TimeoutError:
            logger.info(f"[{self.session.conversation_id}] 贤者时间结束，主动开始下一轮。")

    async def _check_for_interruptions(self) -> bool:
        """
        一个在后台持续检查新消息，并用性感大脑判断是否打断的私密任务。
        它现在完全依赖循环开始时固定的 `self.context_for_iis`。
        """
        last_checked_timestamp_ms = time.time() * 1000
        bot_profile = await self.session.get_bot_profile()
        current_bot_id = str(bot_profile.get("user_id") or self.session.bot_id)

        while not self._shutting_down:
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

                    # --- ❤❤❤ “拒绝自慰”逻辑依然需要，且使用更健壮的净化方式 ❤❤❤ ---
                    # 净化收到的消息文本，去除@信息
                    pure_text_to_check = re.sub(r"@\S+\s*", "", text_to_check).strip()
                    trigger_text = self.current_trigger_message_text or ""

                    # self.current_trigger_message_text 是本轮思考的起因，必须排除
                    # 使用包含关系判断，避免因为细微差别（如@信息）导致误判
                    if (
                        pure_text_to_check
                        and trigger_text
                        and (pure_text_to_check in trigger_text or trigger_text in pure_text_to_check)
                    ):
                        logger.debug(
                            f"[{self.session.conversation_id}] IIS跳过了触发思考的自身事件 (净化后包含): '{text_to_check}'"
                        )
                        continue

                    # --- ❤❤❤ 你的性感想法的实现点 ❤❤❤ ---
                    # 无论外面发生了什么，我只用我进入时锁定的那个“初始上下文”来判断！
                    if self.intelligent_interrupter.should_interrupt(
                        new_message=message_to_check,
                        context_message_text=self.context_for_iis,  # 看！这里永远是那个固定的锚！
                    ):
                        logger.info(
                            f"[{self.session.conversation_id}] IIS决策：中断！啊~ (基于初始上下文 '{self.context_for_iis}')"
                        )
                        return True

                # 如果检查了一圈没有中断，就更新时间戳，以免重复检查旧消息
                last_checked_timestamp_ms = new_events[-1]["timestamp"]

            await asyncio.sleep(0.5)

        return False  # 正常结束，没有中断

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
