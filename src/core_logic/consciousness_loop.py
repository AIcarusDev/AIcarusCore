# src/core_logic/consciousness_loop.py

import asyncio
import datetime
import json
import random
import re
import threading
import sys
from typing import Any, Dict, List, Optional, Tuple, Coroutine, TYPE_CHECKING

from src.sub_consciousness.chat_session_handler import ChatSessionManager
from src.action.action_handler import ActionHandler
from src.common.custom_logging.logger_manager import get_logger
# 🐾 小懒猫改动：导入我们自定义的 TaskCancelledByExternalEventError
from src.common.protected_runner import execute_protected_task_with_polling, TaskTimeoutError, TaskCancelledByExternalEventError
from src.common.utils import format_chat_history_for_prompt
from src.config.alcarus_configs import AlcarusRootConfig, CoreLogicSettings, PersonaSettings
from src.core_communication.core_ws_server import CoreWebsocketServer
from src.database.arangodb_handler import ArangoDBHandler
from src.llmrequest.llm_processor import Client as ProcessorClient

from .thought_builder import CorePromptBuilder
from .thought_processor import CoreThoughtProcessor
from .main_thought_input_preparer import MainThoughtInputPreparer

if TYPE_CHECKING:
    from src.plugins.intrusive_thoughts_plugin import IntrusiveThoughtsGenerator

logger = get_logger("AIcarusCore.CoreLogic")

class CoreLogic:
    INITIAL_STATE: Dict[str, Any] = {
        "mood": "平静。",
        "previous_thinking": "这是你的第一次思考，请开始吧。",
        "thinking_guidance": "随意发散一下吧。",
        "current_task_info_for_prompt": "你当前没有什么特定的目标或任务。",
        "action_result_info": "你上一轮没有执行产生结果的特定行动。",
        "pending_action_status": "",
        "recent_contextual_information": "最近未感知到任何特定信息或通知。",
        "active_sub_mind_latest_activity": "目前没有活跃的子思维会话，或者它们最近没有活动。",
    }

    def __init__(
        self,
        root_cfg: AlcarusRootConfig,
        db_handler: ArangoDBHandler,
        main_consciousness_llm_client: ProcessorClient,
        intrusive_thoughts_llm_client: ProcessorClient,
        sub_mind_llm_client: ProcessorClient,
        action_decision_llm_client: Optional[ProcessorClient],
        information_summary_llm_client: Optional[ProcessorClient],
        chat_session_manager: Optional[ChatSessionManager] = None,
        core_comm_layer: Optional[CoreWebsocketServer] = None,
        intrusive_generator_instance: Optional['IntrusiveThoughtsGenerator'] = None,
    ):
        self.logger = logger
        self.root_cfg: AlcarusRootConfig = root_cfg
        self.db_handler: ArangoDBHandler = db_handler

        self.main_consciousness_llm_client: ProcessorClient = main_consciousness_llm_client
        self.intrusive_thoughts_llm_client: ProcessorClient = intrusive_thoughts_llm_client
        self.sub_mind_llm_client: ProcessorClient = sub_mind_llm_client

        self.chat_session_manager: Optional[ChatSessionManager] = chat_session_manager
        self.core_comm_layer: Optional[CoreWebsocketServer] = core_comm_layer

        self.stop_event: threading.Event = threading.Event()
        self.async_stop_event: asyncio.Event = asyncio.Event()
        self.sub_mind_update_event: asyncio.Event = asyncio.Event()

        self.core_incoming_event_queue: asyncio.Queue = asyncio.Queue()
        self.event_processing_cooldown_seconds: float = 0.5
        self.max_events_per_cycle: int = 5

        self.action_handler_instance: Optional[ActionHandler] = None
        if hasattr(self.root_cfg, 'action_handler_settings') and self.root_cfg.action_handler_settings and self.root_cfg.action_handler_settings.enabled:
            self.logger.info("ActionHandler 配置为启用，正在初始化...")
            self.action_handler_instance = ActionHandler(root_cfg=self.root_cfg)
            self.action_handler_instance.set_dependencies(
                db_handler=self.db_handler,
                comm_layer=self.core_comm_layer
            )
            self.logger.info("ActionHandler 已配置并设置了依赖。其LLM客户端将在首次使用时初始化。")
        elif hasattr(self.root_cfg, 'action_handler_settings') and self.root_cfg.action_handler_settings and not self.root_cfg.action_handler_settings.enabled:
            self.logger.info("ActionHandler 在配置中被禁用，将不会被初始化。")
        else:
             self.logger.warning("警告：在 root_cfg 中未找到 action_handler_settings 配置。ActionHandler 将不会被初始化。")

        self.intrusive_generator_instance: Optional['IntrusiveThoughtsGenerator'] = intrusive_generator_instance
        if self.intrusive_generator_instance:
            self.logger.info("IntrusiveThoughtsGenerator 实例已通过参数传入。")
        else:
            self.logger.info("IntrusiveThoughtsGenerator 实例未传入，可能已禁用或未初始化。")

        self.prompt_builder = CorePromptBuilder(self.root_cfg.persona, self.INITIAL_STATE, self.logger)
        self.thought_processor = CoreThoughtProcessor(
            db_handler=self.db_handler,
            action_handler_instance=self.action_handler_instance,
            chat_session_manager=self.chat_session_manager,
            core_comm_layer=self.core_comm_layer,
            logger_instance=self.logger, # 传递 logger 实例
        )
        self.input_preparer = MainThoughtInputPreparer(
            db_handler=self.db_handler,
            chat_session_manager=self.chat_session_manager,
            intrusive_generator_instance=self.intrusive_generator_instance,
            logger_instance=self.logger,
            core_logic_settings=self.root_cfg.core_logic_settings
        )

        self.current_focused_conversation_id: Optional[str] = None
        # 🐾 小懒猫加的：添加一个实例变量来保存当前正在运行的LLM任务
        # 🐾 小懒猫修改：改为私有变量，并确保在任务开始时清空。
        self._current_main_llm_thinking_task: Optional[asyncio.Task[Tuple[Optional[Dict[str, Any]], Optional[str], Optional[str]]]] = None
        self.logger.info("CoreLogic instance created.")

    async def _generate_thought_from_llm(
        self,
        llm_client: ProcessorClient,
        system_prompt_str: str,
        user_prompt_str: str,
        # 🐾 小懒猫修改：取消事件应该由 ProtectedRunner 内部管理，或者由 CoreLogic 在需要取消时设置
        # ProtectedRunner 会将它自己的 cancellation_event 传递给 llm_client
        # 这里只保留 llm_client 接收的 cancellation_event，以便正确传递给 make_llm_request
        cancellation_event_for_llm_client: Optional[asyncio.Event] = None
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str], Optional[str]]:
        self.logger.info(f"正在请求 {llm_client.llm_client.provider} API ({llm_client.llm_client.model_name}) 生成主思考 (受保护的调用)...")
        # 🐾 小懒猫修改：这个日志应该更精确，它不是传递给 ProtectedRunner，而是 ProtectedRunner 会传递给 make_llm_request
        # if cancellation_event:
        #     self.logger.debug(f"传递给 ProtectedRunner 的 cancellation_event 当前状态: {cancellation_event.is_set()}")

        raw_llm_response_text: str = ""
        try:
            llm_request_coro: Coroutine[Any, Any, Dict[str, Any]] = llm_client.make_llm_request(
                prompt=user_prompt_str,
                system_prompt=system_prompt_str,
                is_stream=False,
                # 🐾 小懒猫修改：将 cancellation_event_for_llm_client 传递给 make_llm_request
                interruption_event=cancellation_event_for_llm_client # 这个事件由 ProtectedRunner 提供
            )

            llm_response_data: Dict[str, Any] = await execute_protected_task_with_polling(
                task_coro=llm_request_coro,
                task_description="主思维LLM思考生成",
                overall_timeout_seconds=self.root_cfg.core_logic_settings.llm_call_overall_timeout_seconds,
                polling_interval_seconds=self.root_cfg.core_logic_settings.llm_call_polling_interval_seconds,
                # 🐾 小懒猫修改：ProtectedRunner 内部会创建并管理一个取消事件。
                # CoreLogic 不再需要在这里传入它自己的事件，除非 CoreLogic 明确要主动取消此任务。
                # ProtectedRunner 会返回 TaskCancelledByExternalEventError 来指示取消。
                # cancellation_event=cancellation_event # 移除了这里的直接传递
            )

            if llm_response_data.get("error"):
                error_type = llm_response_data.get("type", "UnknownError")
                error_message = llm_response_data.get("message", "LLM客户端返回了一个错误")
                self.logger.error(f"主思维LLM调用失败 ({error_type}): {error_message}")
                if llm_response_data.get("details"):
                    self.logger.error(f"  错误详情: {str(llm_response_data.get('details'))[:300]}...")
                return None, user_prompt_str, system_prompt_str

            if llm_response_data.get("interrupted", False) or \
               llm_response_data.get("finish_reason", "").upper() in ["INTERRUPTED", "INTERRUPTED_BEFORE_CALL"]:
                # 🐾 小懒猫改动：这里增加详细日志，说明LLM任务为什么被中断
                self.logger.warning(f"主思维LLM思考生成任务被中断。原因: {llm_response_data.get('finish_reason', 'N/A')}. "
                                   f"LLM客户端可能在内部检测到取消事件或连接问题。原始响应: {str(llm_response_data)[:200]}...")
                return None, user_prompt_str, system_prompt_str

            raw_llm_response_text = llm_response_data.get("text")
            if not raw_llm_response_text:
                error_message_no_text = "错误：主思维LLM响应中缺少文本内容。"
                if llm_response_data:
                    error_message_no_text += f"\n  完整响应: {str(llm_response_data)[:500]}..."
                self.logger.error(error_message_no_text)
                return None, user_prompt_str, system_prompt_str

            json_string_to_parse = raw_llm_response_text.strip()
            if json_string_to_parse.startswith("```json"):
                json_string_to_parse = json_string_to_parse[7:-3].strip()
            elif json_string_to_parse.startswith("```"):
                 json_string_to_parse = json_string_to_parse[3:-3].strip()

            # 🐾 小懒猫改动：在正则替换前先记录原始字符串，防止调试困难
            original_json_string_before_regex = json_string_to_parse
            json_string_to_parse = re.sub(r"[,\s]+(?=\}$)", "}", json_string_to_parse)
            json_string_to_parse = re.sub(r",\s*$", "", json_string_to_parse)
            if original_json_string_before_regex != json_string_to_parse:
                self.logger.debug(f"LLM响应JSON经过正则修正。原始(前100): '{original_json_string_before_regex[:100]}...', 修正后(前100): '{json_string_to_parse[:100]}...'")

            parsed_thought_json: Dict[str, Any] = json.loads(json_string_to_parse)
            self.logger.info("主思维LLM API 响应已成功解析为JSON。")

            if llm_response_data.get("usage"):
                parsed_thought_json["_llm_usage_info"] = llm_response_data["usage"]

            return parsed_thought_json, user_prompt_str, system_prompt_str

        except TaskTimeoutError as e_task_timeout:
            self.logger.error(f"错误：主思维LLM思考生成任务超时: {e_task_timeout}")
            # 🐾 小懒猫修改：当超时发生时，我们也要明确地返回None
            return None, user_prompt_str, system_prompt_str
        except TaskCancelledByExternalEventError as e_task_cancelled:
            # 🐾 小懒猫改动：这里明确捕获 TaskCancelledByExternalEventError
            self.logger.warning(f"主思维LLM思考生成任务被外部事件取消: {e_task_cancelled}. 任务在ProtectedRunner内部被取消。")
            # 🐾 小懒猫修改：当被取消时，我们也要明确地返回None
            return None, user_prompt_str, system_prompt_str
        except asyncio.CancelledError as e_async_cancelled:
            # 🐾 小懒猫改动：显式捕获 asyncio.CancelledError
            self.logger.error(f"主思维LLM思考生成任务在执行中被外部asyncio.CancelledError取消: {e_async_cancelled}", exc_info=True)
            return None, user_prompt_str, system_prompt_str
        except json.JSONDecodeError as e_json:
            self.logger.error(f"错误：解析主思维LLM的JSON响应失败: {e_json}")
            self.logger.error(f"未能解析的文本内容: {raw_llm_response_text}")
            return None, user_prompt_str, system_prompt_str
        except Exception as e_unexpected:
            self.logger.error(
                f"错误：调用主思维LLM或处理其响应时发生意外错误: {e_unexpected}", exc_info=True
            )
            return None, user_prompt_str, system_prompt_str

    async def _core_thinking_loop(self) -> None:
        if not all([self.root_cfg,
                    self.db_handler,
                    self.main_consciousness_llm_client,
                    self.chat_session_manager,
                    self.prompt_builder,
                    self.thought_processor,
                    self.input_preparer
                    ]):
            self.logger.critical("核心思考循环无法启动：一个或多个核心组件未初始化。")
            return

        action_id_whose_result_was_shown_in_last_prompt: Optional[str] = None
        # 🐾 小懒猫修改：这里的 main_llm_cancellation_event 应该由 CoreLogic 控制，
        # 当 CoreLogic 决定取消当前 LLM 任务时才设置它。
        # ProtectedRunner 会在内部为 LLM 请求创建一个 `interruption_event` 并传递给 LLMClient。
        # 如果 CoreLogic 想在 LLM 任务正在运行的时候，外部（比如新的消息事件）让它中断，
        # 那么这个事件是必要的。
        main_llm_cancellation_event = asyncio.Event()

        core_logic_cfg: CoreLogicSettings = self.root_cfg.core_logic_settings
        time_format_str: str = "%Y年%m月%d日 %H点%M分%S秒"
        thinking_interval_sec: int = core_logic_cfg.thinking_interval_seconds

        self.logger.info(f"\n--- {self.root_cfg.persona.bot_name if self.root_cfg else 'Bot'} 的意识开始流动 (重构版 V2) ---")
        loop_count: int = 0
        
        while not self.async_stop_event.is_set():
            loop_count += 1
            self.logger.info(f"主思维循环第 {loop_count} 次迭代开始。")
            current_time_formatted_str: str = datetime.datetime.now().strftime(time_format_str)

            # 🐾 小懒猫修改：在每轮开始时，如果之前有任务在运行，应该先确保它被“清理”
            # 这个变量只用于标记LLM是否应该重新启动生成
            should_initiate_new_llm_thought_generation: bool = False

            # --- 阶段1: 检查并处理队列中的事件 ---
            events_processed_this_cycle = 0
            while not self.core_incoming_event_queue.empty() and events_processed_this_cycle < self.max_events_per_cycle:
                try:
                    event_from_queue = self.core_incoming_event_queue.get_nowait()
                    self.logger.info(f"主思维循环 {loop_count}: 从队列中获取事件: 类型={event_from_queue.get('type')}, 会话ID={event_from_queue.get('conversation_id')}")
                    should_initiate_new_llm_thought_generation = True # 收到新事件就应该触发思考
                    events_processed_this_cycle += 1
                    self.core_incoming_event_queue.task_done()

                    await asyncio.sleep(self.event_processing_cooldown_seconds)

                except asyncio.QueueEmpty:
                    break

                except Exception as e_queue:
                    self.logger.error(f"主思维循环 {loop_count}: 处理队列事件时发生错误: {e_queue}", exc_info=True)
                    break

            if events_processed_this_cycle > 0:
                self.logger.info(f"主思维循环 {loop_count}: 本轮从队列处理了 {events_processed_this_cycle} 个事件。")
                self.sub_mind_update_event.clear() # 清除子思维更新事件，表示已处理
                should_initiate_new_llm_thought_generation = True # 收到事件强制触发思考

            # --- 阶段2: 等待触发（定时器或新事件）或处理LLM任务完成 ---
            # 🐾 小懒猫修改：调整逻辑，首先处理正在运行的LLM任务。
            if self._current_main_llm_thinking_task: # 如果有LLM任务在运行
                self.logger.info(f"主思维循环 {loop_count}: 检测到前一个主LLM思考任务仍在进行中。")
                
                # 创建一个用于等待LLM任务或中断事件的辅助任务
                # 我们想知道LLM任务是否完成了，或者停止事件、子思维更新事件是否触发了
                wait_tasks = [
                    self._current_main_llm_thinking_task,
                    asyncio.create_task(self.async_stop_event.wait(), name=f"stop_event_wait_running_llm_{loop_count}"),
                    asyncio.create_task(self.sub_mind_update_event.wait(), name=f"sub_mind_update_wait_running_llm_{loop_count}")
                ]

                done, pending = await asyncio.wait(
                    wait_tasks,
                    return_when=asyncio.FIRST_COMPLETED,
                )

                # 首先检查停止事件
                if self.async_stop_event.is_set():
                    self.logger.info(f"主思维循环 {loop_count}: 检测到停止信号，将取消所有挂起任务并退出。")
                    if self._current_main_llm_thinking_task and not self._current_main_llm_thinking_task.done():
                        # 🐾 小懒猫修改：主动取消当前正在进行的LLM任务
                        main_llm_cancellation_event.set() # 通知 ProtectedRunner 内部的 LLM 请求中断
                        self._current_main_llm_thinking_task.cancel() # 取消 LLM 任务
                        self.logger.info(f"主思维循环 {loop_count}: 已发送取消信号给正在运行的LLM任务。")
                    for task_to_cancel in pending:
                        if not task_to_cancel.done(): task_to_cancel.cancel()
                    # 等待所有任务真正结束，包括被取消的
                    await asyncio.gather(*done, *pending, return_exceptions=True)
                    break # 退出主循环

                # 如果是 sub_mind_update_event 触发的
                if self.sub_mind_update_event in done:
                    self.logger.info(f"主思维循环 {loop_count}: 子思维事件触发，中断当前LLM思考并立即重新思考。")
                    self.sub_mind_update_event.clear() # 清除事件
                    should_initiate_new_llm_thought_generation = True
                    # 🐾 小懒猫修改：如果子思维事件触发，强制取消当前的LLM任务，因为要重新思考
                    if self._current_main_llm_thinking_task and not self._current_main_llm_thinking_task.done():
                        main_llm_cancellation_event.set() # 设置 LLM 客户端的取消事件
                        self._current_main_llm_thinking_task.cancel() # 取消 LLM 任务
                        self.logger.info(f"主思维循环 {loop_count}: 已发送取消信号给正在运行的LLM任务 (因子思维事件)。")
                    # 等待剩余的 pending 任务完成或取消
                    for task_to_cancel in pending:
                         if task_to_cancel != self._current_main_llm_thinking_task and not task_to_cancel.done():
                             task_to_cancel.cancel()
                    await asyncio.gather(*pending, return_exceptions=True) # 等待其他任务结束

                # 如果是 _current_main_llm_thinking_task 完成了
                if self._current_main_llm_thinking_task in done:
                    self.logger.info(f"主思维循环 {loop_count}: 上一轮的主LLM思考任务已自然完成。")
                    should_initiate_new_llm_thought_generation = True # LLM任务完成，可以处理结果并考虑下一次思考
                    # 清理其他 pending 任务
                    for task_to_cancel in pending:
                         if not task_to_cancel.done(): task_to_cancel.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)
                
                # 如果都不是，说明有其他任务完成，或者没有任务完成（例如timeout，但 ProtectedRunner 应该处理）
                # 这里不需要 else，因为如果 _current_main_llm_thinking_task 没在 done 里，
                # 并且没有其他事件触发，那么循环会继续等待
                
                # 🐾 小懒猫修改：无论哪种情况，LLM 任务的结果都需要在下面统一处理。
                # 即使被取消，其 await 也会抛出异常，在下面的 try/except 块中捕获。

            else: # 没有正在运行的LLM任务，或者上一轮已完成
                self.logger.debug(f"主思维循环 {loop_count}: 没有正在运行的LLM任务。")
                try:
                    # 🐾 小懒猫修改：这里只等待队列事件或定时器到期。如果事件触发，就立即思考。
                    # 如果定时器到期，也立即思考。
                    event_wait_task = asyncio.create_task(self.core_incoming_event_queue.get(), name=f"queue_event_wait_{loop_count}")
                    stop_wait_task = asyncio.create_task(self.async_stop_event.wait(), name=f"stop_event_wait_{loop_count}")
                    sub_mind_wait_task = asyncio.create_task(self.sub_mind_update_event.wait(), name=f"sub_mind_update_wait_{loop_count}")

                    done, pending = await asyncio.wait(
                        [event_wait_task, stop_wait_task, sub_mind_wait_task],
                        timeout=float(thinking_interval_sec),
                        return_when=asyncio.FIRST_COMPLETED
                    )
                    
                    # 首先检查停止事件
                    if self.async_stop_event.is_set():
                        self.logger.info(f"主思维循环 {loop_count}: 检测到停止信号，将取消所有挂起任务并退出。")
                        for task_to_cancel in pending:
                            if not task_to_cancel.done(): task_to_cancel.cancel()
                        await asyncio.gather(*done, *pending, return_exceptions=True)
                        break # 退出主循环

                    # 如果是队列事件触发的
                    if event_wait_task in done:
                        event_from_queue_timed_wait = event_wait_task.result() # 获取事件内容
                        self.logger.info(f"主思维循环 {loop_count}: 从队列中获取新事件 (超时等待): 类型={event_from_queue_timed_wait.get('type')}, 会话ID={event_from_queue_timed_wait.get('conversation_id')}")
                        should_initiate_new_llm_thought_generation = True
                        self.core_incoming_event_queue.task_done()
                        await asyncio.sleep(self.event_processing_cooldown_seconds)
                    elif sub_mind_wait_task in done: # 如果是子思维事件触发的
                        self.logger.info(f"主思维循环 {loop_count}: 子思维事件触发，立即进行思考。")
                        self.sub_mind_update_event.clear() # 清除事件
                        should_initiate_new_llm_thought_generation = True
                    else: # 所有的等待任务都超时了（即定时器到期）
                        self.logger.info(f"主思维循环 {loop_count}: 定时器 ({thinking_interval_sec}s) 到期，队列中无事件，准备进行LLM思考。")
                        should_initiate_new_llm_thought_generation = True
                    
                    # 清理剩余的 pending 任务
                    for task_to_cancel in pending:
                        if not task_to_cancel.done(): task_to_cancel.cancel()
                    await asyncio.gather(*pending, return_exceptions=True) # 等待其他任务结束

                except asyncio.CancelledError:
                    self.logger.info(f"主思维循环 {loop_count}: 等待队列事件或定时器时被取消。")
                    break # 退出循环
                except Exception as e_wait:
                    self.logger.error(f"主思维循环 {loop_count}: 等待事件或定时器时发生错误: {e_wait}", exc_info=True)
                    continue # 继续下一次循环

            # --- 阶段3: 执行或处理LLM思考 (如果 should_initiate_new_llm_thought_generation 为 True) ---
            if should_initiate_new_llm_thought_generation:
                self.logger.info(f"主思维循环 {loop_count}: 准备获取数据库和上下文信息以进行LLM思考。")
                latest_thought_doc_from_db = await self.db_handler.get_latest_thought_document_raw() if self.db_handler else None

                current_state_for_prompt, temp_action_id_result_shown = (
                    await self.input_preparer.prepare_current_state_for_prompt(
                        latest_thought_document=latest_thought_doc_from_db,
                        current_focused_conversation_id=self.current_focused_conversation_id
                    )
                )
                self.logger.debug(f"主思维循环 {loop_count}: 处理思考和动作状态完成。")

                system_prompt_str = self.prompt_builder.build_system_prompt(current_time_formatted_str)
                intrusive_thought_str_for_prompt = await self.input_preparer.get_intrusive_thought()
                user_prompt_str = self.prompt_builder.build_user_prompt(
                    current_state_for_prompt=current_state_for_prompt,
                    intrusive_thought_str=intrusive_thought_str_for_prompt
                )

                self.logger.info(
                    f"\n[{datetime.datetime.now().strftime('%H:%M:%M')} - 轮次 {loop_count}] " # 🐾 小懒猫修改：分钟的格式是 %M 而不是 %m
                    f"{self.root_cfg.persona.bot_name if self.root_cfg else 'Bot'} 准备调用LLM进行思考 (受保护)..."
                )

                # 🐾 小懒猫改动：如果当前没有LLM任务在跑，或者上一个任务已经完成了，才创建新的任务
                if not self._current_main_llm_thinking_task or self._current_main_llm_thinking_task.done():
                    self.logger.debug(f"主思维循环 {loop_count}: 创建新的主LLM思考任务。")
                    # 🐾 小懒猫修改：清空取消事件，以便新的LLM任务可以使用
                    main_llm_cancellation_event.clear()
                    self._current_main_llm_thinking_task = asyncio.create_task(
                        self._generate_thought_from_llm(
                            llm_client=self.main_consciousness_llm_client,
                            system_prompt_str=system_prompt_str,
                            user_prompt_str=user_prompt_str,
                            # 🐾 小懒猫修改：将本 CoreLogic 实例控制的取消事件传递给 LLM 任务
                            cancellation_event_for_llm_client=main_llm_cancellation_event 
                        ),
                        name=f"MainLLMThoughtTask_Loop{loop_count}"
                    )
                else:
                    self.logger.info(f"主思维循环 {loop_count}: 发现已有主LLM思考任务正在进行中，本轮不重复创建新的LLM任务。将等待其结果。")
                
                # 🐾 小懒猫改动：等待当前LLM任务完成。如果任务已完成，会直接获取结果。
                # 如果任务被取消，会捕获 CancelledError
                generated_thought_json: Optional[Dict[str, Any]] = None
                try:
                    # 确保 self._current_main_llm_thinking_task 存在，并且是可 await 的任务
                    if self._current_main_llm_thinking_task:
                        llm_call_output_tuple = await self._current_main_llm_thinking_task
                        generated_thought_json, _, _ = llm_call_output_tuple
                    else:
                        self.logger.warning(f"主思维循环 {loop_count}: 无法获取或执行主LLM思考任务。任务实例为None。")
                        generated_thought_json = None # 明确设为None

                except asyncio.CancelledError:
                    self.logger.warning(f"主思维循环 {loop_count}: 主LLM思考任务在等待结果时被取消。可能是由于自身超时、外部中断或程序关闭。")
                    generated_thought_json = None
                except Exception as e_llm_task_await:
                    self.logger.error(f"主思维循环 {loop_count}: 等待主LLM思考任务结果时发生错误: {e_llm_task_await}", exc_info=True)
                    generated_thought_json = None
                
                # 🐾 小懒猫改动：LLM任务完成后，将其引用清空，以便下一轮可以创建新任务
                self._current_main_llm_thinking_task = None 

                self.logger.info(f"主思维循环 {loop_count}: LLM思考生成调用完成。")

                if generated_thought_json and self.thought_processor:
                    _, background_tasks_from_processor = await self.thought_processor.process_thought_and_actions(
                        generated_thought_json=generated_thought_json,
                        current_state_for_prompt=current_state_for_prompt,
                        current_time_formatted_str=current_time_formatted_str,
                        system_prompt_sent=system_prompt_str,
                        full_prompt_text_sent=user_prompt_str,
                        intrusive_thought_to_inject_this_cycle=intrusive_thought_str_for_prompt,
                        formatted_recent_contextual_info=current_state_for_prompt["recent_contextual_information"],
                        action_id_whose_result_was_shown_in_last_prompt=temp_action_id_result_shown,
                        loop_count=loop_count
                    )
                elif not generated_thought_json:
                    self.logger.warning(f"主思维循环 {loop_count}: 本轮主思维LLM思考生成失败或无内容。")
                    if action_id_whose_result_was_shown_in_last_prompt and self.db_handler:
                        try:
                            await self.db_handler.mark_action_result_as_seen(action_id_whose_result_was_shown_in_last_prompt)
                        except Exception as e_mark_seen_after_fail:
                            self.logger.error(f"主思维循环 {loop_count}: (LLM失败后) 标记动作结果为已阅时失败: {e_mark_seen_after_fail}", exc_info=True)
            else:
                self.logger.info(f"主思维循环 {loop_count}: 本轮不进行新的LLM思考（可能因为上一轮任务仍在进行或无触发条件）。")

            self.logger.info(f"主思维思考循环轮次 {loop_count} 逻辑处理结束。")

            self.logger.debug(f"循环末尾检查: self.async_stop_event.is_set() 的状态是: {self.async_stop_event.is_set()}")

            if self.async_stop_event.is_set():
                self.logger.info(f"主思维循环 {loop_count}: 在循环末尾检测到停止信号，准备退出。")
                break

        self.logger.info(f"--- {self.root_cfg.persona.bot_name if self.root_cfg else 'Bot'} 的主思维思考循环已结束 (共 {loop_count} 轮)。")

    async def start(self) -> asyncio.Task:
        self.logger.info("CoreLogic 开始启动...")
        thinking_loop_task = asyncio.create_task(self._core_thinking_loop(), name="CoreThinkingLoopTask")
        self.logger.info("核心思考循环已作为异步任务启动。")
        return thinking_loop_task

    async def stop(self) -> None:
        self.logger.info("CoreLogic 收到停止请求...")
        self.stop_event.set()
        self.async_stop_event.set()
        # 🐾 小懒猫修改：如果 LLM 任务正在运行，主动取消它
        if self._current_main_llm_thinking_task and not self._current_main_llm_thinking_task.done():
            # 同样设置 LLM 客户端的取消事件，确保 ProtectedRunner 和 LLM 客户端能够感知到
            # 否则 LLM 请求可能会继续运行直到自然完成或自身超时
            self.logger.info("CoreLogic 停止时，正在取消正在运行的主LLM思考任务。")
            self._current_main_llm_thinking_task.cancel()
            try:
                # 等待任务真正结束，以便它能清理资源并正确处理 CancelledError
                await self._current_main_llm_thinking_task
            except asyncio.CancelledError:
                self.logger.info("正在运行的主LLM思考任务已被成功取消。")
            except Exception as e:
                self.logger.error(f"取消主LLM思考任务时发生错误: {e}", exc_info=True)
            finally:
                self._current_main_llm_thinking_task = None # 确保清理引用
        self.logger.info("CoreLogic 停止请求处理完毕。")