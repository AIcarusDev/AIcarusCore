import asyncio
import time
from typing import TYPE_CHECKING

from src.common.custom_logging.logger_manager import get_logger
from src.config import config
from .interruption_watcher import InterruptionWatcher

if TYPE_CHECKING:
    from .chat_session import ChatSession

logger = get_logger(__name__)


class FocusChatCycler:
    """
    管理单个专注聊天会话的主动循环引擎（精简版）。
    只负责驱动“观察-思考-决策”的循环，并在没有新消息时进行自我再思考。
    具体的业务逻辑都丢给别的模块去干，哼！
    """

    def __init__(self, session: "ChatSession") -> None:
        self.session = session
        self._loop_active: bool = False
        self._loop_task: asyncio.Task | None = None
        self._shutting_down: bool = False

        # 从 session 中获取依赖，方便访问
        self.conversation_id = self.session.conversation_id
        self.llm_client = self.session.llm_client
        self.prompt_builder = self.session.prompt_builder
        
        # 新的模块化依赖
        self.llm_response_handler = self.session.llm_response_handler
        self.action_executor = self.session.action_executor
        self.summarization_manager = self.session.summarization_manager

        self._interruption_event = asyncio.Event()
        self._wakeup_event = asyncio.Event()
        self.interruption_watcher = InterruptionWatcher(self.session, self._interruption_event)

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

        self.interruption_watcher.shutdown()
        if self._loop_task:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                logger.info(f"[FocusChatCycler][{self.conversation_id}] 循环任务已取消。")

        # 在关闭的最后阶段保存最终总结
        await self.summarization_manager.save_final_summary()

        self._loop_active = False
        logger.info(f"[FocusChatCycler][{self.conversation_id}] 已关闭。")
    
    def wakeup(self):
        """从外部唤醒空闲的循环。哼，别随便叫我！"""
        logger.debug(f"[{self.conversation_id}] 接收到外部唤醒信号。")
        self._wakeup_event.set()

    async def _chat_loop(self) -> None:
        """
        专注聊天的主循环（精简版）。
        """
        idle_thinking_interval = getattr(config.focus_chat_mode, "self_reflection_interval_seconds", 15)

        while not self._shutting_down:
            self._interruption_event.clear()
            self._wakeup_event.clear()
            observer_task = None
            llm_task = None

            try:
                # 阶段一：思考与决策
                system_prompt, user_prompt, uid_map, processed_ids = await self._prepare_and_think()
                
                current_loop_start_time_ms = time.time() * 1000
                llm_task = asyncio.create_task(
                    self.llm_client.make_llm_request(prompt=user_prompt, system_prompt=system_prompt, is_stream=False)
                )
                observer_task = asyncio.create_task(self.interruption_watcher.run(current_loop_start_time_ms))

                # 等待LLM思考或被中断
                done, pending = await asyncio.wait([llm_task, observer_task], return_when=asyncio.FIRST_COMPLETED)

                if observer_task in done or self._interruption_event.is_set():
                    logger.info(f"[{self.conversation_id}] 思考被高价值新消息中断，将立即重新思考。")
                    if llm_task and not llm_task.done(): llm_task.cancel()
                    continue

                # 阶段二：执行与状态更新
                if llm_task in done:
                    if observer_task and not observer_task.done(): observer_task.cancel()
                    
                    async with self.session.processing_lock:
                        llm_response = llm_task.result()
                        await self._process_llm_response(llm_response, uid_map, processed_ids)

                # 阶段三：空闲等待
                await self._idle_wait(idle_thinking_interval)

            except asyncio.CancelledError:
                logger.info(f"[{self.conversation_id}] 循环被取消。")
                break
            except Exception as e:
                logger.error(f"[{self.conversation_id}] 循环中发生意外错误: {e}", exc_info=True)
                await asyncio.sleep(5)
            finally:
                if observer_task and not observer_task.done(): observer_task.cancel()
                if llm_task and not llm_task.done(): llm_task.cancel()

        logger.info(f"[{self.conversation_id}] 专注聊天循环已结束。")

    async def _prepare_and_think(self):
        logger.debug(f"[{self.conversation_id}] 循环开始，正在构建 prompts...")
        system_prompt, user_prompt, uid_map, processed_ids = await self.prompt_builder.build_prompts(
            session=self.session,
            last_processed_timestamp=self.session.last_processed_timestamp,
            last_llm_decision=self.session.last_llm_decision,
            sent_actions_context=self.session.sent_actions_context,
            is_first_turn=self.session.is_first_turn_for_session,
            last_think_from_core=self.session.initial_core_think,
        )
        logger.debug(f"[{self.conversation_id}] Prompts 构建完成，正在启动 LLM 思考任务和中断观察员...")
        return system_prompt, user_prompt, uid_map, processed_ids

    async def _process_llm_response(self, llm_api_response: dict, uid_map: dict, processed_event_ids: list):
        self.session.last_active_time = time.time()
        if processed_event_ids:
            await self.session.event_storage.update_events_status(processed_event_ids, "read")
            logger.info(f"[{self.conversation_id}] 已将 {len(processed_event_ids)} 个事件状态更新为 'read'。")

        response_text = llm_api_response.get("text") if llm_api_response else None
        if not response_text or (llm_api_response and llm_api_response.get("error")):
            error_msg = llm_api_response.get("message") if llm_api_response else "无响应"
            logger.error(f"[{self.conversation_id}] LLM调用失败或返回空: {error_msg}")
            self.session.last_llm_decision = {"think": f"LLM调用失败: {error_msg}", "reply_willing": False, "motivation": "系统错误导致无法思考"}
            return

        parsed_data = self.llm_response_handler.parse(response_text)
        if not parsed_data:
            logger.error(f"[{self.conversation_id}] LLM响应最终解析失败或为空。")
            self.session.last_llm_decision = {"think": "LLM响应解析失败或为空", "reply_willing": False, "motivation": "系统错误导致无法解析LLM的胡言乱语"}
            return

        if "mood" not in parsed_data:
            parsed_data["mood"] = "平静"
        self.session.last_llm_decision = parsed_data

        if await self.llm_response_handler.handle_end_focus_chat_if_needed(parsed_data):
            # 触发关闭流程
            self._shutting_down = True
            return

        action_recorded = await self.action_executor.execute_action(parsed_data, uid_map)

        if action_recorded:
            await self.summarization_manager.queue_events_for_summary(processed_event_ids)
            await self.summarization_manager.consolidate_summary_if_needed()

        if self.session.is_first_turn_for_session:
            self.session.is_first_turn_for_session = False
        
        self.session.last_processed_timestamp = time.time() * 1000

    async def _idle_wait(self, interval: float):
        logger.debug(f"[{self.conversation_id}] 进入空闲等待阶段，等待下一次唤醒或 {interval} 秒后超时。")
        try:
            await asyncio.wait_for(self._wakeup_event.wait(), timeout=interval)
            logger.info(f"[{self.conversation_id}] 被新消息唤醒，立即开始下一轮思考。")
        except asyncio.TimeoutError:
            logger.info(f"[{self.conversation_id}] 空闲等待超时，主动开始下一轮思考。")
