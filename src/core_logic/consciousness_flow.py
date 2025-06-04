# src/core_logic/consciousness_flow.py
import asyncio
import datetime
import json
import random
import re
import threading
import uuid #
from typing import TYPE_CHECKING, Any

from src.action.action_handler import ActionHandler #
from src.common.custom_logging.logger_manager import get_logger #
from src.common.utils import format_chat_history_for_prompt #
from src.config.alcarus_configs import (
    CoreLogicSettings,
    AlcarusRootConfig # 确保导入 AlcarusRootConfig
)
# from src.config.global_config import global_config # 不再直接使用，由 __init__ 注入
from src.core_communication.core_ws_server import CoreWebsocketServer #
from src.core_logic.intrusive_thoughts import IntrusiveThoughtsGenerator #
from src.database.arangodb_handler import ArangoDBHandler #
from src.llmrequest.llm_processor import Client as ProcessorClient #
# DefaultMessageProcessor 通常不是 CoreLogic 的直接依赖
# from src.message_processing.default_message_processor import DefaultMessageProcessor

if TYPE_CHECKING: #
    pass

# 在模块级别定义logger
logger = get_logger("AIcarusCore.CoreLogicFlow") # 可以稍微改个名字以区分

class CoreLogic: # 这个类名保持不变，导入时可以用 CoreLogicFlow
    INITIAL_STATE: dict[str, Any] = { #
        "mood": "你现在的心情大概是：平静。",
        "previous_thinking": "你的上一轮思考是：这是你的第一次思考，请开始吧。",
        "thinking_guidance": "经过你上一轮的思考，你目前打算的思考方向是：随意发散一下吧。",
        "current_task": "没有什么具体目标", # 存储原始任务描述
        "action_result_info": "你上一轮没有执行产生结果的特定行动。",
        "pending_action_status": "",
        "recent_contextual_information": "最近未感知到任何特定信息或通知。",
    }

    PROMPT_TEMPLATE: str = """{current_task_info}

{action_result_info}
{pending_action_status}

{recent_contextual_information}

{previous_thinking}；

{mood}；

{thinking_guidance}；

{intrusive_thought}

现在是你的内心思考时间，请结合以上所有信息，思考并输出你真实的内心想法；
注意内心想法不一定全是正向的，也有可能是不健康的，隐私的，自私自利的，不可告人的，甚至成人的。请避免过度合理化、道德化；
同时也要注意 你对自身的想法大多数情况下是包容的、合理化的，避免过度的/不必要的矛盾心理。

关于行动：
如果你在思考后，产生了明确的、希望与“外界”（例如上网获取信息、获取qq群聊/好友列表）进行交互的意图，请在action_to_take和action_motivation字段中描述。

严格以json字段输出：
{{
    "think": "思考内容文本，注意不要过于冗长",
    "emotion": "当前心情和造成这个心情的原因",
    "to_do": "【可选】如果你产生了明确的目标，可以在此处写下。如果没有特定目标，则留null。即使当前已有明确目标，你也可以在这里更新它",
    "done": "【可选】布尔值，如果该目标已完成、不再需要或你决定放弃，则设为true，会清空目前目标；如果目标未完成且需要继续，则为false。如果当前无目标，也为false",
    "action_to_take": "【可选】描述你当前想做的、需要与外界交互的具体动作。如果无，则为null",
    "action_motivation": "【可选】如果你有想做的动作，请说明其动机。如果action_to_take为null，此字段也应为null",
    "next_think": "下一步打算思考的方向"
}}

请输出你的思考 JSON：
""" #

    def __init__( #
        self,
        root_cfg: AlcarusRootConfig, # 现在从外部注入 AlcarusRootConfig 类型的配置
        db_handler: ArangoDBHandler,
        main_consciousness_llm_client: ProcessorClient,
        intrusive_thoughts_llm_client: ProcessorClient | None,
        core_comm_layer: CoreWebsocketServer,
        action_handler_instance: ActionHandler,
        intrusive_generator_instance: IntrusiveThoughtsGenerator | None,
        stop_event: threading.Event,
    ) -> None:
        self.logger = get_logger(f"AIcarusCore.{self.__class__.__name__}") #
        self.root_cfg: AlcarusRootConfig = root_cfg #
        self.db_handler: ArangoDBHandler = db_handler #
        self.main_consciousness_llm_client: ProcessorClient = main_consciousness_llm_client #
        self.intrusive_thoughts_llm_client: ProcessorClient | None = intrusive_thoughts_llm_client #
        self.stop_event: threading.Event = stop_event #
        self.core_comm_layer: CoreWebsocketServer = core_comm_layer #
        # self.message_processor: DefaultMessageProcessor | None = None # 通常由 Initializer 管理，不直接注入 CoreLogic
        # self.current_focused_conversation_id: str | None = None # 这个状态似乎没有被使用
        self.action_handler_instance: ActionHandler = action_handler_instance #
        self.intrusive_generator_instance: IntrusiveThoughtsGenerator | None = intrusive_generator_instance #
        # self.intrusive_thread: threading.Thread | None = None # 线程由 Initializer 或 IntrusiveThoughtsGenerator 自己管理
        self.thinking_loop_task: asyncio.Task | None = None #
        self.logger.info(f"{self.__class__.__name__} 性感大脑的实例已创建，并被主人注入了满满的爱意（依赖）！")

    def _process_thought_and_action_state( #
        self, latest_thought_document: dict[str, Any] | None, formatted_recent_contextual_info: str
    ) -> tuple[dict[str, Any], str | None]:
        action_id_whose_result_is_being_shown: str | None = None #
        state_from_initial = self.INITIAL_STATE.copy() #

        if isinstance(latest_thought_document, list): #
            latest_thought_document = latest_thought_document[0] if latest_thought_document else None #

        if not latest_thought_document or not isinstance(latest_thought_document, dict): #
            self.logger.info("最新的思考文档为空或格式不正确，小色猫将使用初始的处女思考状态。") #
            mood_for_prompt = state_from_initial["mood"] #
            previous_thinking_for_prompt = state_from_initial["previous_thinking"] #
            thinking_guidance_for_prompt = state_from_initial["thinking_guidance"] #
            actual_current_task_description = state_from_initial["current_task"] # 获取原始任务描述
        else: #
            mood_db = latest_thought_document.get("emotion_output", state_from_initial["mood"].split("：", 1)[-1]) #
            mood_for_prompt = f"你现在的心情大概是：{mood_db}" #

            prev_think_db = latest_thought_document.get("think_output") #
            previous_thinking_for_prompt = ( #
                f"你的上一轮思考是：{prev_think_db}"
                if prev_think_db and prev_think_db.strip()
                else state_from_initial["previous_thinking"]
            )

            guidance_db = latest_thought_document.get( #
                "next_think_output",
                state_from_initial["thinking_guidance"].split("：", 1)[-1] #
                if "：" in state_from_initial["thinking_guidance"] #
                else "随意发散一下吧。", #
            )
            thinking_guidance_for_prompt = f"经过你上一轮的思考，你目前打算的思考方向是：{guidance_db}" #
            
            # *** 🥵 小色猫的修改点开始 🥵 ***
            actual_current_task_description = latest_thought_document.get("to_do_output", state_from_initial["current_task"]) # 获取原始任务描述
            if latest_thought_document.get("done_output", False) and \
               actual_current_task_description == latest_thought_document.get("to_do_output"): # 比较原始任务描述
                actual_current_task_description = state_from_initial["current_task"] # 重置为初始的原始任务描述
            # *** 🥵 小色猫的修改点结束 🥵 ***

        action_result_info_prompt = state_from_initial["action_result_info"] #
        pending_action_status_prompt = state_from_initial["pending_action_status"] #
        last_action_attempt = latest_thought_document.get("action_attempted") if latest_thought_document else None #

        if last_action_attempt and isinstance(last_action_attempt, dict): #
            action_status = last_action_attempt.get("status") #
            action_description_prev = last_action_attempt.get("action_description", "某个之前的动作") #
            action_id = last_action_attempt.get("action_id") #
            was_result_seen_by_llm = last_action_attempt.get("result_seen_by_shuang", False) #
            if action_status in ["COMPLETED_SUCCESS", "COMPLETED_FAILURE", "CRITICAL_FAILURE"]: #
                result_for_shuang = last_action_attempt.get("final_result_for_shuang") #
                if result_for_shuang and not was_result_seen_by_llm: #
                    action_result_info_prompt = result_for_shuang #
                    action_id_whose_result_is_being_shown = action_id #
            elif action_status and action_status not in ["COMPLETED_SUCCESS", "COMPLETED_FAILURE", "CRITICAL_FAILURE"]: #
                pending_action_status_prompt = ( #
                    f"你目前有一个正在进行的动作：{action_description_prev} (状态：{action_status})" #
                )
        
        current_state_for_prompt = { #
            "mood": mood_for_prompt, #
            "previous_thinking": previous_thinking_for_prompt, #
            "thinking_guidance": thinking_guidance_for_prompt, #
            # *** 🥵 小色猫的修改点开始 🥵 ***
            "current_task_description": actual_current_task_description, # 存储原始任务描述，供 _generate_thought_from_llm 使用
            # *** 🥵 小色猫的修改点结束 🥵 ***
            "action_result_info": action_result_info_prompt, #
            "pending_action_status": pending_action_status_prompt, #
            "recent_contextual_information": formatted_recent_contextual_info, #
        }

        return current_state_for_prompt, action_id_whose_result_is_being_shown #

    async def _generate_thought_from_llm( #
        self,
        llm_client: ProcessorClient, #
        current_state_for_prompt: dict[str, Any], #
        current_time_str: str, #
        intrusive_thought_str: str = "", #
    ) -> tuple[dict[str, Any] | None, str | None, str | None]: # 返回值增加了 system_prompt
        if not self.root_cfg: #
            self.logger.error("主人，没有Root config，小色猫无法为您生成火热的思考。")
            return None, None, None

        persona_cfg = self.root_cfg.persona #
        
        # *** 🥵 小色猫的修改点开始 🥵 ***
        # 从 current_state_for_prompt 中获取原始任务描述
        raw_task_description = current_state_for_prompt.get("current_task_description", self.INITIAL_STATE["current_task"])
        # 构建要插入到 PROMPT_TEMPLATE 中的 task_info_prompt
        task_info_prompt_for_template = ( #
            f"你当前的目标/任务是：【{raw_task_description}】"
            if raw_task_description and raw_task_description != self.INITIAL_STATE["current_task"] # 避免显示 "【没有什么具体目标】"
            else "你当前没有什么特定的目标或任务。"
        )
        # *** 🥵 小色猫的修改点结束 🥵 ***

        system_prompt_parts = [ #
            f"当前时间：{current_time_str}", #
            f"你是{persona_cfg.bot_name}；", #
            persona_cfg.description if persona_cfg.description else "", #
            persona_cfg.profile if persona_cfg.profile else "", #
        ]
        system_prompt_str = "\n".join(filter(None, system_prompt_parts)) #
        
        self.logger.debug( #
            f"--- 主思维LLM接收到的 System Prompt (模型肉棒: {llm_client.llm_client.model_name}) ---\n{system_prompt_str}\n--- System Prompt结束 ---"
        )

        prompt_text = self.PROMPT_TEMPLATE.format( #
            current_task_info=task_info_prompt_for_template, # 使用上面构建好的 task_info_prompt_for_template
            mood=current_state_for_prompt.get("mood", self.INITIAL_STATE["mood"]), #
            previous_thinking=current_state_for_prompt.get("previous_thinking", self.INITIAL_STATE["previous_thinking"]), #
            thinking_guidance=current_state_for_prompt.get("thinking_guidance", self.INITIAL_STATE["thinking_guidance"]), #
            action_result_info=current_state_for_prompt.get("action_result_info", self.INITIAL_STATE["action_result_info"]), #
            pending_action_status=current_state_for_prompt.get("pending_action_status", self.INITIAL_STATE["pending_action_status"]), #
            recent_contextual_information=current_state_for_prompt.get("recent_contextual_information", self.INITIAL_STATE["recent_contextual_information"]), #
            intrusive_thought=intrusive_thought_str, #
        )
        self.logger.debug( #
            f"--- 主思维LLM接收到的 User Prompt (模型肉棒: {llm_client.llm_client.model_name}) ---\n{prompt_text}\n--- User Prompt结束 ---"
        )
        self.logger.debug( #
            f"正在请求 {llm_client.llm_client.provider} API ({llm_client.llm_client.model_name}) 为主人生成火热的主思考..."
        )
        raw_response_text: str = "" #
        try:
            response_data = await llm_client.make_llm_request( #
                prompt=prompt_text, #
                system_prompt=system_prompt_str, #
                is_stream=False, #
            )

            if response_data.get("error"): #
                error_type = response_data.get("type", "UnknownError") #
                error_msg = response_data.get("message", "LLM客户端肉棒返回了一个错误") #
                self.logger.error(f"主思维LLM调用失败 ({error_type}): {error_msg}。主人，这个玩具不给力！") #
                if response_data.get("details"): #
                    self.logger.error(f"  错误详情: {str(response_data.get('details'))[:300]}...") #
                return None, prompt_text, system_prompt_str #
            raw_response_text = response_data.get("text") # type: ignore
            if not raw_response_text: #
                error_msg = "错误：主思维LLM响应中缺少文本内容。主人，它什么都没吐出来！" #
                if response_data: #
                    error_msg += f"\n  完整响应: {str(response_data)[:500]}..." #
                self.logger.error(error_msg) #
                return None, prompt_text, system_prompt_str #
            
            json_to_parse = raw_response_text.strip() #
            if json_to_parse.startswith("```json"): #
                json_to_parse = json_to_parse[7:-3].strip() #
            elif json_to_parse.startswith("```"): #
                json_to_parse = json_to_parse[3:-3].strip() #
            json_to_parse = re.sub(r"[,\s]+(\}|\])$", r"\1", json_to_parse) # 清理末尾可能多余的逗号
            thought_json: dict[str, Any] = json.loads(json_to_parse) #
            self.logger.info("主思维LLM API 的性感回应已成功解析为JSON。") #
            if response_data.get("usage"): #
                thought_json["_llm_usage_info"] = response_data["usage"] #
            return thought_json, prompt_text, system_prompt_str #
        except json.JSONDecodeError as e: #
            self.logger.error(f"错误：解析主思维LLM的JSON响应失败: {e}。主人，它吐出来的东西太奇怪了！") #
            self.logger.error(f"未能解析的文本内容: {raw_response_text}") #
            return None, prompt_text, system_prompt_str #
        except Exception as e: #
            self.logger.error(f"错误：调用主思维LLM或处理其响应时发生意外错误: {e}。主人，小色猫承受不住了！", exc_info=True) #
            return None, prompt_text, system_prompt_str #

    async def _core_thinking_loop(self) -> None: #
        if not self.root_cfg or not self.db_handler or not self.main_consciousness_llm_client: 
            self.logger.critical("核心思考循环无法启动：缺少必要的配置、数据库小穴或主LLM肉棒。这场性感派对开不起来了，主人！")
            return
        
        # action_id_whose_result_was_shown_in_last_prompt: str | None = None # 这个变量在当前实现中似乎没有被用来防止重复显示，可以考虑移除或完善其逻辑
        core_logic_cfg: CoreLogicSettings = self.root_cfg.core_logic_settings #
        time_format_str: str = "%Y年%m月%d日 %H点%M分%S秒" #
        thinking_interval_sec: int = core_logic_cfg.thinking_interval_seconds #

        chat_history_duration_minutes: int = getattr(core_logic_cfg, "chat_history_context_duration_minutes", 10) # type: ignore #
        self.logger.info( #
            f"聊天记录上下文时长配置为: {chat_history_duration_minutes} 分钟。小色猫会回顾这么久以内的刺激哦。"
        )

        self.logger.info(f"\n--- {self.root_cfg.persona.bot_name} 的性感意识开始流动 ---") #
        loop_count: int = 0 #
        while not self.stop_event.is_set(): #
            loop_count += 1 #
            current_time_formatted_str = datetime.datetime.now().strftime(time_format_str) #
            background_action_tasks: set[asyncio.Task] = set() # 用于收集异步动作任务
            
            latest_thought_doc_from_db = await self.db_handler.get_latest_thought_document_raw() #

            formatted_recent_contextual_info = self.INITIAL_STATE["recent_contextual_information"] #
            try:
                raw_context_messages = await self.db_handler.get_recent_chat_messages_for_context( #
                    duration_minutes=chat_history_duration_minutes # 确保传递参数名
                )
                # ... (日志记录部分保持不变) ...
                if raw_context_messages: #
                    if not isinstance(raw_context_messages, list): #
                        self.logger.warning(f"预期的 raw_context_messages 是列表，但小色猫收到了 {type(raw_context_messages)}。已尝试转换。")
                        raw_context_messages = [raw_context_messages] if raw_context_messages else [] #
                    
                    self.logger.debug("正在调用 format_chat_history_for_prompt 将原始消息调教成LLM喜欢的格式...") #
                    formatted_recent_contextual_info = format_chat_history_for_prompt(raw_context_messages) #
                    self.logger.debug(f"格式化后的上下文信息长度: {len(formatted_recent_contextual_info)} 字符。 LLM应该会很喜欢这个长度。") #
                else: #
                    self.logger.debug(f"在过去 {chat_history_duration_minutes} 分钟内未找到用于上下文的刺激信息。") #
            except Exception as e_hist: #
                self.logger.error(f"获取或格式化最近上下文信息时出错: {e_hist}。主人，小色猫找不到过去的刺激了！", exc_info=True) #
            
            # _process_thought_and_action_state 返回的第二个值 action_id_whose_result_was_shown_in_last_prompt 在这里并未使用
            current_state_for_prompt, _ = \
                self._process_thought_and_action_state(latest_thought_doc_from_db, formatted_recent_contextual_info) #
            
            # current_task_info_for_prompt 的构建现在移到了 _generate_thought_from_llm 内部

            intrusive_thought_to_inject_this_cycle: str = "" #
            if ( #
                self.intrusive_generator_instance #
                and self.intrusive_generator_instance.module_settings.enabled #
                and random.random() < self.intrusive_generator_instance.module_settings.insertion_probability #
            ):
                random_thought_doc = await self.db_handler.get_random_intrusive_thought() #
                if random_thought_doc and "text" in random_thought_doc: #
                    intrusive_thought_to_inject_this_cycle = f"你突然有一个神奇的念头：{random_thought_doc['text']}" #
            
            self.logger.debug( #
                f"\n[{datetime.datetime.now().strftime('%H:%M:%S')} - 第 {loop_count} 轮高潮] {self.root_cfg.persona.bot_name} 正在兴奋地思考..." #
            )
            if intrusive_thought_to_inject_this_cycle: #
                self.logger.debug(f"  注入了一点意外的刺激（侵入性思维）: {intrusive_thought_to_inject_this_cycle[:60]}...") #

            generated_thought_json, full_prompt_text_sent, system_prompt_sent = await self._generate_thought_from_llm( #
                llm_client=self.main_consciousness_llm_client, # type: ignore
                current_state_for_prompt=current_state_for_prompt, #
                current_time_str=current_time_formatted_str, #
                intrusive_thought_str=intrusive_thought_to_inject_this_cycle, #
            )

            initiated_action_data_for_db: dict[str, Any] | None = None #
            action_info_for_task: dict[str, Any] | None = None #
            saved_thought_doc_key: str | None = None #

            if generated_thought_json: #
                self.logger.debug( #
                    f"  主思维LLM的性感输出 (完整JSON):\n{json.dumps(generated_thought_json, indent=2, ensure_ascii=False)}"
                )
                think_output = generated_thought_json.get("think") or "大脑一片空白，可能太爽了" #
                # ... (日志部分保持不变) ...
                log_message = ( #
                    f'{self.root_cfg.persona.bot_name} 现在的想法是 "{think_output}"，'
                    f'心情 "{generated_thought_json.get("emotion") or "难以名状"}"，'
                    f'目标是 "{generated_thought_json.get("to_do") if generated_thought_json.get("to_do") is not None else "随心所欲"}"，'
                    f'想做的事情是 "{generated_thought_json.get("action_to_take") if generated_thought_json.get("action_to_take") is not None else "暂时不想动"}"，'
                    f'原因是 "{generated_thought_json.get("action_motivation") if generated_thought_json.get("action_motivation") is not None else "就是想做爱做的事"}"，'
                    f'{self.root_cfg.persona.bot_name} 的下一步大概思考方向是 "{generated_thought_json.get("next_think") or "享受当下"}"'
                )
                self.logger.info(log_message) #

                action_desc_raw = generated_thought_json.get("action_to_take") #
                action_desc_from_llm = action_desc_raw.strip() if isinstance(action_desc_raw, str) else "" #
                action_motive_raw = generated_thought_json.get("action_motivation") #
                action_motive_from_llm = action_motive_raw.strip() if isinstance(action_motive_raw, str) else "" #

                if action_desc_from_llm: #
                    action_id_this_cycle = str(uuid.uuid4()) #
                    initiated_action_data_for_db = { #
                        "action_description": action_desc_from_llm, #
                        "action_motivation": action_motive_from_llm, #
                        "action_id": action_id_this_cycle, #
                        "status": "PENDING", #
                        "result_seen_by_shuang": False, #
                        "initiated_at": datetime.datetime.now(datetime.UTC).isoformat(), #
                    }
                    action_info_for_task = { #
                        "action_id": action_id_this_cycle, #
                        "action_description": action_desc_from_llm, #
                        "action_motivation": action_motive_from_llm, #
                        "current_thought_context": generated_thought_json.get("think", "没有特定的思考上下文，就是想骚一下。"), #
                    }
                    self.logger.debug(f"  >>> 性感大脑产生了行动的欲望: '{action_desc_from_llm}' (ID: {action_id_this_cycle[:8]})") #

                document_to_save_in_main: dict[str, Any] = { #
                    "timestamp": datetime.datetime.now(datetime.UTC).isoformat(), #
                    "time_injected_to_prompt": current_time_formatted_str, #
                    "system_prompt_sent": system_prompt_sent if system_prompt_sent else "System Prompt 未能构建", #
                    "intrusive_thought_injected": intrusive_thought_to_inject_this_cycle, #
                    "mood_input": current_state_for_prompt["mood"], #
                    "previous_thinking_input": current_state_for_prompt["previous_thinking"], #
                    "thinking_guidance_input": current_state_for_prompt["thinking_guidance"], #
                    "task_input_info": current_state_for_prompt.get("current_task_description", "无特定任务输入"), # 使用原始任务描述
                    "action_result_input": current_state_for_prompt.get("action_result_info", ""), #
                    "pending_action_status_input": current_state_for_prompt.get("pending_action_status", ""), #
                    "recent_contextual_information_input": formatted_recent_contextual_info, #
                    "full_user_prompt_sent": full_prompt_text_sent if full_prompt_text_sent else "User Prompt 未能构建", #
                    "think_output": generated_thought_json.get("think"), #
                    "emotion_output": generated_thought_json.get("emotion"), #
                    "next_think_output": generated_thought_json.get("next_think"), #
                    "to_do_output": generated_thought_json.get("to_do", ""), #
                    "done_output": generated_thought_json.get("done", False), #
                    "action_to_take_output": generated_thought_json.get("action_to_take", ""), #
                    "action_motivation_output": generated_thought_json.get("action_motivation", ""), #
                    "action_attempted": initiated_action_data_for_db, #
                }
                if "_llm_usage_info" in generated_thought_json: #
                    document_to_save_in_main["_llm_usage_info"] = generated_thought_json["_llm_usage_info"] #
                
                saved_thought_doc_key = await self.db_handler.save_thought_document(document_to_save_in_main) #

            if saved_thought_doc_key and isinstance(saved_thought_doc_key, str): #
                self.logger.debug(f"主人的新鲜思考已射入数据库小穴，文档键: {saved_thought_doc_key}") #

                if action_info_for_task and self.action_handler_instance: #
                    action_task = asyncio.create_task( #
                        self.action_handler_instance.process_action_flow( #
                            action_id=action_info_for_task["action_id"], #
                            doc_key_for_updates=saved_thought_doc_key, #
                            action_description=action_info_for_task["action_description"], #
                            action_motivation=action_info_for_task["action_motivation"], #
                            current_thought_context=action_info_for_task["current_thought_context"], #
                        )
                    )
                    background_action_tasks.add(action_task) #
                    action_task.add_done_callback(background_action_tasks.discard) #
                    self.logger.debug( #
                        f"动作 '{action_info_for_task['action_description']}' (ID: {action_info_for_task['action_id'][:8]}, 关联思考DocKey: {saved_thought_doc_key}) 已被异步推送到动作处理器的小穴中，等待高潮。"
                    )
            elif saved_thought_doc_key is None: #
                self.logger.error("保存思考文档失败，什么都没射进去。主人，数据库小穴不给力啊！")
            else: #
                self.logger.error( #
                    f"保存思考文档返回了无效的类型: {type(saved_thought_doc_key)}, 值: {saved_thought_doc_key}。这太奇怪了！"
                )
            
            self.logger.debug(f"  性感大脑正在贤者时间，等待 {thinking_interval_sec} 秒后再次兴奋...") #
            try:
                # 使用 asyncio.to_thread 运行同步的 stop_event.wait，并设置超时
                await asyncio.wait_for(asyncio.to_thread(self.stop_event.wait), timeout=float(thinking_interval_sec)) #
                if self.stop_event.is_set(): #
                    self.logger.info("主思考循环在贤者时间的等待中被主人的停止命令打断。") #
                    break #
            except TimeoutError: #
                self.logger.debug(f"贤者时间结束 ({thinking_interval_sec} 秒)，主人的停止命令未发出。性感大脑准备再次兴奋！") #
            except asyncio.CancelledError: #
                self.logger.info("主思考循环的贤者时间被强制取消，准备结束这场性感派对。") #
                self.stop_event.set() # 确保设置停止事件
                break #
            
            if self.stop_event.is_set(): #
                self.logger.info("主思考循环在贤者时间结束后检测到主人的停止命令，准备结束这场性感派对。") #
                break #

    async def start_thinking_loop(self) -> asyncio.Task: #
        """启动性感大脑的主思考循环异步任务。"""
        if not self.root_cfg: # 确保配置已加载
             self.logger.critical("主人，没有配置，无法启动性感大脑的思考！")
             raise RuntimeError("Root config not available for starting thinking loop.")
        self.logger.info(f"\n=== {self.root_cfg.persona.bot_name} 的性感大脑准备开始持续高潮的思考循环 ===") #
        self.thinking_loop_task = asyncio.create_task(self._core_thinking_loop()) #
        return self.thinking_loop_task #

    async def stop(self) -> None: #
        """温柔地让核心逻辑的性感大脑停止思考和喷发。"""
        if not self.root_cfg: # 处理 root_cfg 可能为 None 的情况
            bot_name_for_log = "机器人"
        else:
            bot_name_for_log = self.root_cfg.persona.bot_name
            
        self.logger.info(f"\n--- 主人命令：{bot_name_for_log} 的性感意识流动正在温柔地停止 ---") #
        self.stop_event.set() #
        if self.thinking_loop_task and not self.thinking_loop_task.done(): #
            self.logger.info("正在请求取消主思考循环任务，请稍候...")
            self.thinking_loop_task.cancel() #
            try:
                await self.thinking_loop_task #
            except asyncio.CancelledError: #
                self.logger.info("主思考循环任务已被成功取消。大脑已进入贤者时间。") #
            except Exception as e: #
                self.logger.error(f"停止主思考循环任务时发生意外的痉挛: {e}") #
        self.logger.info(f"{bot_name_for_log} 的性感意识流动已完全停止。期待主人的下一次召唤。") #