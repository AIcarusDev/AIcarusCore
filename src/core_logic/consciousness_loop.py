import asyncio
import datetime
import json
import random
import re
import threading
import sys # 确保导入 sys
from typing import Any, Dict, List, Optional, Tuple, Coroutine, TYPE_CHECKING

# 更改导入路径
from action.action_handler import ActionHandler
from common.custom_logging.logger_manager import get_logger
from common.protected_runner import execute_protected_task_with_polling, TaskTimeoutError, TaskCancelledByExternalEventError
from common.utils import format_chat_history_for_prompt
from config.alcarus_configs import AlcarusRootConfig, CoreLogicSettings, PersonaSettings
from core_communication.core_ws_server import CoreWebsocketServer
from database.arangodb_handler import ArangoDBHandler
from llmrequest.llm_processor import Client as ProcessorClient
from sub_consciousness.chat_session_handler import ChatSessionManager

# 从 core_logic 内部导入（它们还在这个包里）
from .thought_builder import CorePromptBuilder
from .thought_processor import CoreThoughtProcessor

# 移除这行导入，因为它现在是插件了
# from .intrusive_thoughts import IntrusiveThoughtsGenerator

# 导入 IntrusiveThoughtsGenerator，因为它现在作为参数传入
# 但是它是一个可选参数，所以这里不直接导入具体的类，而是在 TYPE_CHECKING 里做
if TYPE_CHECKING:
    from plugins.intrusive_thoughts_plugin import IntrusiveThoughtsGenerator #

logger = get_logger("AIcarusCore.CoreLogic")

class CoreLogic:
    PROMPT_TEMPLATE: str = """\
你当前的目标/任务是：【{current_task_info}】

{action_result_info}

{pending_action_status}

{recent_contextual_information}

{active_sub_mind_latest_activity}

你的上一轮思考是：{previous_thinking}；

你现在的心情大概是：{mood}；

经过你上一轮的思考，你目前打算的思考方向是：{thinking_guidance}；

{intrusive_thought}

现在是你的内心思考时间，请结合以上所有信息，思考并输出你真实的内心想法；
注意内心想法不一定全是正向的，也有可能是不健康的，隐私的，自私自利的，不可告人的，甚至成人的。请避免过度合理化、道德化；
同时也要注意 你对自身的想法大多数情况下是包容的、合理化的，避免过度的/不必要的矛盾心理。

关于行动：
如果你在思考后，产生了明确的、希望与“外界”（例如上网获取信息、获取qq群聊/好友列表）进行交互的意图，请在action_to_take和action_motivation字段中描述。
如果你希望与某个聊天会话的“子思维”进行交互（例如，让它回复消息、激活它、休眠它），请在 sub_mind_directives字段中描述你的指令。

严格以json字段输出：
{{
    "think": "思考内容文本，注意不要过于冗长",
    "emotion": "当前心情和造成这个心情的原因",
    "to_do": "【可选】如果你产生了明确的目标，可以在此处写下。如果没有特定目标，则留null。即使当前已有明确目标，你也可以在这里更新它",
    "done": "【可选】布尔值，如果该目标已完成、不再需要或你决定放弃，则设为true，会清空目前目标；如果目标未完成且需要继续，则设为false。如果当前无目标，也为false",
    "action_to_take": "【可选】描述你当前想做的、需要与外界交互的具体动作。如果无，则为null",
    "action_motivation": "【可选】如果你有想做的动作，请说明其动机。如果action_to_take为null，此字段也应为null",
    "sub_mind_directives": [
        {{
            "conversation_id": "string, 目标会话的ID",
            "directive_type": "string, 指令类型，例如 'TRIGGER_REPLY', 'ACTIVATE_SESSION', 'DEACTIVATE_SESSION', 'SET_CHAT_STYLE'",
            "main_thought_for_reply": "string, 【可选】仅当 directive_type 为 TRIGGER_REPLY 或 ACTIVATE_SESSION 时，主思维希望注入给子思维的当前想法上下文",
            "style_details": {{}} "object, 【可选】仅当 directive_type 为 SET_CHAT_STYLE 时，具体的风格指令"
        }}
    ],
    "next_think": "下一步打算思考的方向"
}}

请输出你的思考 JSON："""

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
        stop_event: threading.Event,
        async_stop_event: asyncio.Event,
        sub_mind_update_event: asyncio.Event,
        chat_session_manager: Optional[ChatSessionManager] = None,
        core_comm_layer: Optional[CoreWebsocketServer] = None,
        # 新增一个参数来接收已初始化的 IntrusiveThoughtsGenerator 实例
        intrusive_generator_instance: Optional['IntrusiveThoughtsGenerator'] = None, #
    ):
        self.logger = logger
        self.root_cfg: AlcarusRootConfig = root_cfg
        self.db_handler: ArangoDBHandler = db_handler
        
        self.main_consciousness_llm_client: ProcessorClient = main_consciousness_llm_client
        self.intrusive_thoughts_llm_client: ProcessorClient = intrusive_thoughts_llm_client
        self.sub_mind_llm_client: ProcessorClient = sub_mind_llm_client
        
        self.chat_session_manager: Optional[ChatSessionManager] = chat_session_manager
        self.core_comm_layer: Optional[CoreWebsocketServer] = core_comm_layer

        self.stop_event: threading.Event = stop_event
        self.async_stop_event: asyncio.Event = async_stop_event
        self.sub_mind_update_event: asyncio.Event = sub_mind_update_event

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

        # 直接接收已初始化的实例，不再在这里初始化了
        self.intrusive_generator_instance: Optional['IntrusiveThoughtsGenerator'] = intrusive_generator_instance #
        if self.intrusive_generator_instance:
            self.logger.info("IntrusiveThoughtsGenerator 实例已通过参数传入。")
        else:
            self.logger.info("IntrusiveThoughtsGenerator 实例未传入，可能已禁用或未初始化。")
            
        # 实例化辅助类
        self.prompt_builder = CorePromptBuilder(self.root_cfg.persona, self.PROMPT_TEMPLATE, self.INITIAL_STATE, self.logger)
        self.thought_processor = CoreThoughtProcessor(
            db_handler=self.db_handler,
            action_handler_instance=self.action_handler_instance,
            chat_session_manager=self.chat_session_manager,
            core_comm_layer=self.core_comm_layer,
            logger_instance=self.logger,
        )

        self.current_focused_conversation_id: Optional[str] = None
        self.logger.info("CoreLogic instance created.")
        
    def _process_thought_and_action_state(
        self,
        latest_thought_document: Optional[Dict[str, Any]],
        formatted_recent_contextual_info: str
    ) -> Tuple[Dict[str, Any], Optional[str]]:
        current_state: Dict[str, Any] = {}
        action_id_result_shown_in_prompt: Optional[str] = None

        if latest_thought_document:
            self.logger.debug("使用数据库中的最新思考文档来构建当前状态。")
            current_state["mood"] = latest_thought_document.get("emotion_output", self.INITIAL_STATE["mood"])
            current_state["previous_thinking"] = latest_thought_document.get("think_output", self.INITIAL_STATE["previous_thinking"])
            current_state["thinking_guidance"] = latest_thought_document.get("next_think_output", self.INITIAL_STATE["thinking_guidance"])
            current_state["current_task"] = latest_thought_document.get("to_do_output", "")
            
            action_attempted = latest_thought_document.get("action_attempted")
            if isinstance(action_attempted, dict):
                action_status = action_attempted.get("status", "UNKNOWN")
                action_desc = action_attempted.get("action_description", "未知动作")
                action_id = action_attempted.get("action_id")

                if action_status == "PENDING":
                    current_state["pending_action_status"] = f"你当前有一个待处理的行动 '{action_desc}' (ID: {action_id[:8] if action_id else 'N/A'})。"
                    current_state["action_result_info"] = self.INITIAL_STATE["action_result_info"]
                elif action_status in ["PROCESSING_DECISION", "TOOL_EXECUTING", "PROCESSING_SUMMARY"]:
                    current_state["pending_action_status"] = f"你当前正在处理行动 '{action_desc}' (ID: {action_id[:8] if action_id else 'N/A'})，状态: {action_status}。"
                    current_state["action_result_info"] = self.INITIAL_STATE["action_result_info"]
                elif action_status in ["COMPLETED_SUCCESS", "COMPLETED_FAILURE"]:
                    result_for_shuang = action_attempted.get("final_result_for_shuang", "动作已完成，但没有具体结果文本。")
                    current_state["action_result_info"] = (
                        f"你上一轮行动 '{action_desc}' (ID: {action_id[:8] if action_id else 'N/A'}) 的结果是：【{result_for_shuang}】"
                    )
                    current_state["pending_action_status"] = ""
                    if action_id and not action_attempted.get("result_seen_by_shuang", False):
                        action_id_result_shown_in_prompt = action_id
                else:
                    current_state["pending_action_status"] = f"你上一轮的行动 '{action_desc}' (ID: {action_id[:8] if action_id else 'N/A'}) 状态未知 ({action_status})。"
                    current_state["action_result_info"] = self.INITIAL_STATE["action_result_info"]
            else:
                current_state["action_result_info"] = self.INITIAL_STATE["action_result_info"]
                current_state["pending_action_status"] = self.INITIAL_STATE["pending_action_status"]
        else:
            self.logger.info("最新的思考文档为空，主思维将使用初始思考状态。")
            current_state = self.INITIAL_STATE.copy()
            current_state["current_task"] = ""

        current_state["recent_contextual_information"] = formatted_recent_contextual_info

        if self.chat_session_manager:
            active_sessions_summary = self.chat_session_manager.get_all_active_sessions_summary() #
            
            if active_sessions_summary:
                summaries_str_parts = []
                for summary_item in active_sessions_summary:
                    summaries_str_parts.append(f"- 会话ID {summary_item.get('conversation_id', '未知')}: 状态={'活跃' if summary_item.get('is_active') else '不活跃'}, 上次回复='{str(summary_item.get('last_reply_generated', '无'))[:30]}...'")
                current_state["active_sub_mind_latest_activity"] = "\n".join(summaries_str_parts)
            else:
                current_state["active_sub_mind_latest_activity"] = self.INITIAL_STATE["active_sub_mind_latest_activity"]
            
        else:
            current_state["active_sub_mind_latest_activity"] = self.INITIAL_STATE["active_sub_mind_latest_activity"]
        
        self.logger.debug(f"在 _process_thought_and_action_state 中：成功处理并返回用于Prompt的状态。Action ID shown: {action_id_result_shown_in_prompt}")
        return current_state, action_id_result_shown_in_prompt

    async def _generate_thought_from_llm(
        self,
        llm_client: ProcessorClient,
        system_prompt_str: str,
        user_prompt_str: str,
        cancellation_event: Optional[asyncio.Event] = None 
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str], Optional[str]]:
        self.logger.info(f"正在请求 {llm_client.llm_client.provider} API ({llm_client.llm_client.model_name}) 生成主思考 (受保护的调用)...")
        if cancellation_event:
            self.logger.debug(f"传递给 ProtectedRunner 的 cancellation_event 当前状态: {cancellation_event.is_set()}")


        raw_llm_response_text: str = ""
        try:
            llm_request_coro: Coroutine[Any, Any, Dict[str, Any]] = llm_client.make_llm_request(
                prompt=user_prompt_str,
                system_prompt=system_prompt_str,
                is_stream=False,
            )

            llm_response_data: Dict[str, Any] = await execute_protected_task_with_polling(
                task_coro=llm_request_coro,
                task_description="主思维LLM思考生成",
                overall_timeout_seconds=self.root_cfg.core_logic_settings.llm_call_overall_timeout_seconds,
                polling_interval_seconds=self.root_cfg.core_logic_settings.llm_call_polling_interval_seconds,
                cancellation_event=cancellation_event
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
                self.logger.warning(f"主思维LLM思考生成任务被中断。Reason: {llm_response_data.get('finish_reason', 'N/A')}")
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
            
            json_string_to_parse = re.sub(r"[,\s]+(?=\}$)", "}", json_string_to_parse)
            json_string_to_parse = re.sub(r",\s*$", "", json_string_to_parse)

            parsed_thought_json: Dict[str, Any] = json.loads(json_string_to_parse)
            self.logger.info("主思维LLM API 响应已成功解析为JSON。")

            if llm_response_data.get("usage"):
                parsed_thought_json["_llm_usage_info"] = llm_response_data["usage"]

            return parsed_thought_json, user_prompt_str, system_prompt_str

        except TaskTimeoutError as e_task_timeout:
            self.logger.error(f"错误：主思维LLM思考生成任务超时: {e_task_timeout}")
            return None, user_prompt_str, system_prompt_str
        except TaskCancelledByExternalEventError as e_task_cancelled:
            self.logger.warning(f"主思维LLM思考生成任务被外部事件取消: {e_task_cancelled}")
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
                    self.thought_processor]):
            self.logger.critical("核心思考循环无法启动：一个或多个核心组件未初始化。")
            return

        action_id_whose_result_was_shown_in_last_prompt: Optional[str] = None
        main_llm_cancellation_event = asyncio.Event()

        core_logic_cfg: CoreLogicSettings = self.root_cfg.core_logic_settings
        time_format_str: str = "%Y年%m月%d日 %H点%M分%S秒"
        thinking_interval_sec: int = core_logic_cfg.thinking_interval_seconds
        chat_history_duration_minutes: int = getattr(core_logic_cfg, "chat_history_context_duration_minutes", 10)
        polling_interval_seconds: float = getattr(core_logic_cfg, "main_loop_polling_interval_seconds", 0.1)

        self.logger.info(f"\n--- {self.root_cfg.persona.bot_name if self.root_cfg else 'Bot'} 的意识开始流动 (重构版 V1) ---")
        loop_count: int = 0
        current_main_llm_task: Optional[asyncio.Task[Tuple[Optional[Dict[str, Any]], Optional[str], Optional[str]]]] = None

        while not self.async_stop_event.is_set():
            loop_count += 1
            self.logger.info(f"主思维循环第 {loop_count} 次迭代开始。")
            current_time_formatted_str: str = datetime.datetime.now().strftime(time_format_str)
            
            main_llm_cancellation_event.clear()

            should_proceed_with_llm_thought: bool = False
            
            if current_main_llm_task and not current_main_llm_task.done():
                self.logger.info(f"主思维循环 {loop_count}: 上一轮的主LLM思考任务仍在进行中。优先等待子思维事件或停止信号。")
                
                sub_mind_wait_task = asyncio.create_task(self.sub_mind_update_event.wait(), name="sub_mind_wait_during_llm")
                stop_event_wait_task = asyncio.create_task(self.async_stop_event.wait(), name="stop_event_wait_during_llm")
                
                done_tasks, pending_tasks = await asyncio.wait(
                    [sub_mind_wait_task, stop_event_wait_task, current_main_llm_task],
                    return_when=asyncio.FIRST_COMPLETED,
                )

                if self.async_stop_event.is_set():
                    self.logger.info(f"主思维循环 {loop_count}: 检测到停止信号，将取消正在进行的LLM任务并退出。")
                    main_llm_cancellation_event.set()
                    if current_main_llm_task and not current_main_llm_task.done():
                        current_main_llm_task.cancel()
                        try:
                            await current_main_llm_task
                        except asyncio.CancelledError:
                            self.logger.info(f"主思维循环 {loop_count}: 正在进行的LLM任务已取消。")
                        except Exception as e_cancel:
                            self.logger.error(f"主思维循环 {loop_count}: 取消LLM任务时出错: {e_cancel}", exc_info=True)
                    break

                triggered_by_sub_mind_while_llm_active = False
                if sub_mind_wait_task in done_tasks:
                     self.logger.info(f"主思维循环 {loop_count}: 子思维事件触发，当前LLM思考将被尝试中断，并立即处理新状态。")
                     main_llm_cancellation_event.set()
                     self.sub_mind_update_event.clear()
                     should_proceed_with_llm_thought = True
                     triggered_by_sub_mind_while_llm_active = True
                     if current_main_llm_task in pending_tasks and not current_main_llm_task.done():
                         current_main_llm_task.cancel()

                if current_main_llm_task in done_tasks:
                    self.logger.info(f"主思维循环 {loop_count}: LLM思考任务在优先等待期间自行完成。")
                    should_proceed_with_llm_thought = True
                    if sub_mind_wait_task in pending_tasks : sub_mind_wait_task.cancel()
                    if stop_event_wait_task in pending_tasks : stop_event_wait_task.cancel()

            else:
                timer_task = asyncio.create_task(asyncio.sleep(float(thinking_interval_sec)), name="timer_task")
                sub_mind_event_task = asyncio.create_task(self.sub_mind_update_event.wait(), name="sub_mind_event_task")
                stop_event_task = asyncio.create_task(self.async_stop_event.wait(), name="stop_event_task_main_wait")

                tasks_to_wait_on = [timer_task, sub_mind_event_task, stop_event_task]
                done_tasks, pending_tasks = await asyncio.wait(
                    tasks_to_wait_on, return_when=asyncio.FIRST_COMPLETED
                )

                self.logger.debug(f"主思维循环 {loop_count}: 等待结束，完成的任务: {[t.get_name() for t in done_tasks if hasattr(t, 'get_name')]}")

                if self.async_stop_event.is_set() or stop_event_task in done_tasks:
                    self.logger.info(f"主思维循环 {loop_count}: 检测到停止信号 (在正常等待后)，准备退出。")
                    for task_to_cancel in pending_tasks:
                        if not task_to_cancel.done(): task_to_cancel.cancel()
                    await asyncio.gather(*tasks_to_wait_on, return_exceptions=True)
                    break

                if timer_task in done_tasks:
                    self.logger.info(f"主思维循环 {loop_count}: 定时器到期，准备进行LLM思考。")
                    should_proceed_with_llm_thought = True
                
                if sub_mind_event_task in done_tasks:
                    self.logger.info(f"主思维循环 {loop_count}: 子思维事件触发，准备进行LLM思考以响应新状态。")
                    self.sub_mind_update_event.clear()
                    should_proceed_with_llm_thought = True
                
                for task_in_pending in pending_tasks:
                    if not task_in_pending.done():
                        self.logger.debug(f"主思维循环 {loop_count}: 准备取消挂起的等待任务: {task_in_pending.get_name() if hasattr(task_in_pending, 'get_name') else '未知任务'}")
                        task_in_pending.cancel()
                if pending_tasks:
                    await asyncio.gather(*pending_tasks, return_exceptions=True)

            if should_proceed_with_llm_thought:
                self.logger.info(f"主思维循环 {loop_count}: 准备获取数据库和上下文信息以进行LLM思考。")
                latest_thought_doc_from_db = await self.db_handler.get_latest_thought_document_raw() if self.db_handler else None

                formatted_recent_contextual_info = self.INITIAL_STATE["recent_contextual_information"]
                if self.db_handler:
                    try:
                        raw_context_messages = await self.db_handler.get_recent_chat_messages_for_context(
                            duration_minutes=chat_history_duration_minutes,
                            conversation_id=self.current_focused_conversation_id
                        )
                        if raw_context_messages:
                            formatted_recent_contextual_info = format_chat_history_for_prompt(raw_context_messages)
                        self.logger.debug(f"主思维循环 {loop_count}: 获取上下文信息完成。上下文长度: {len(formatted_recent_contextual_info)}")
                    except Exception as e_hist:
                        self.logger.error(f"主思维循环 {loop_count}: 获取或格式化最近上下文信息时出错: {e_hist}", exc_info=True)

                current_state_for_prompt, temp_action_id_result_shown = (
                    self._process_thought_and_action_state(
                        latest_thought_document=latest_thought_doc_from_db,
                        formatted_recent_contextual_info=formatted_recent_contextual_info
                    )
                )
                self.logger.debug(f"主思维循环 {loop_count}: 处理思考和动作状态完成。")

                system_prompt_str = self.prompt_builder.build_system_prompt(current_time_formatted_str)
                user_prompt_str = self.prompt_builder.build_user_prompt(
                    current_state_for_prompt=current_state_for_prompt,
                    intrusive_thought_str=await self._get_intrusive_thought_for_cycle()
                )

                self.logger.info(
                    f"\n[{datetime.datetime.now().strftime('%H:%M:%S')} - 轮次 {loop_count}] "
                    f"{self.root_cfg.persona.bot_name if self.root_cfg else 'Bot'} 准备调用LLM进行思考 (受保护)..."
                )
                
                if not (current_main_llm_task and not current_main_llm_task.done()):
                    current_main_llm_task = asyncio.create_task(
                        self._generate_thought_from_llm(
                            llm_client=self.main_consciousness_llm_client,
                            system_prompt_str=system_prompt_str,
                            user_prompt_str=user_prompt_str,
                            cancellation_event=main_llm_cancellation_event
                        ),
                        name=f"MainLLMThoughtTask_Loop{loop_count}"
                    )
                
                try:
                    llm_call_output_tuple = await current_main_llm_task
                except asyncio.CancelledError:
                    self.logger.warning(f"主思维循环 {loop_count}: 主LLM思考任务在等待时被取消。")
                    llm_call_output_tuple = None
                except Exception as e_llm_task_await:
                    self.logger.error(f"主思维循环 {loop_count}: 等待主LLM思考任务时发生错误: {e_llm_task_await}", exc_info=True)
                    llm_call_output_tuple = None
                finally:
                    current_main_llm_task = None

                generated_thought_json: Optional[Dict[str, Any]] = None
                
                if llm_call_output_tuple:
                    generated_thought_json, _, _ = llm_call_output_tuple
                    if generated_thought_json:
                        action_id_whose_result_was_shown_in_last_prompt = temp_action_id_result_shown
                
                self.logger.info(f"主思维循环 {loop_count}: LLM思考生成调用完成。")

                if generated_thought_json and self.thought_processor:
                    _, background_tasks_from_processor = await self.thought_processor.process_thought_and_actions(
                        generated_thought_json=generated_thought_json,
                        current_state_for_prompt=current_state_for_prompt,
                        current_time_formatted_str=current_time_formatted_str,
                        system_prompt_sent=system_prompt_str,
                        full_prompt_text_sent=user_prompt_str,
                        intrusive_thought_to_inject_this_cycle=await self._get_intrusive_thought_for_cycle(used=True),
                        formatted_recent_contextual_info=formatted_recent_contextual_info,
                        action_id_whose_result_was_shown_in_last_prompt=action_id_whose_result_was_shown_in_last_prompt,
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

    async def _get_intrusive_thought_for_cycle(self, used: bool = False) -> str:
        # 这里使用传入的 self.intrusive_generator_instance
        intrusive_thought_to_inject_this_cycle: str = ""
        # 确保 self.intrusive_generator_instance 存在且已启用
        if self.intrusive_generator_instance and \
           self.intrusive_generator_instance.module_settings.enabled and \
           random.random() < self.intrusive_generator_instance.module_settings.insertion_probability:
            if self.db_handler:
                random_thought_doc = await self.db_handler.get_random_intrusive_thought()
                if random_thought_doc and "text" in random_thought_doc:
                    intrusive_thought_to_inject_this_cycle = f"你突然有一个神奇的念头：{random_thought_doc['text']}"
                    if used:
                        self.logger.debug(f"侵入性思维 '{random_thought_doc['text'][:30]}...' 已被用于Prompt。")
            else:
                self.logger.warning("无法获取侵入性思维，因为数据库处理器 (db_handler) 未初始化。")
        
        if intrusive_thought_to_inject_this_cycle and not used:
             self.logger.info(f"  本轮注入侵入性思维: {intrusive_thought_to_inject_this_cycle[:60]}...")
        return intrusive_thought_to_inject_this_cycle

    async def start(self) -> asyncio.Task:
        self.logger.info("CoreLogic 开始启动...")
        thinking_loop_task = asyncio.create_task(self._core_thinking_loop(), name="CoreThinkingLoopTask")
        self.logger.info("核心思考循环已作为异步任务启动。")
        return thinking_loop_task

    async def stop(self) -> None:
        self.logger.info("CoreLogic 收到停止请求...")
        self.stop_event.set()
        self.async_stop_event.set()
        self.logger.info("CoreLogic 停止请求处理完毕。")
