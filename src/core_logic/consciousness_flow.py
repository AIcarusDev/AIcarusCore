# src/core_logic/consciousness_flow.py
import asyncio
import datetime
import json
import random
import re
import threading
import uuid 
import time
from typing import TYPE_CHECKING, Any, List 

from src.action.action_handler import ActionHandler 
from src.common.custom_logging.logger_manager import get_logger 
from src.common.utils import format_chat_history_for_prompt 
from src.config.alcarus_configs import (
    CoreLogicSettings,
    AlcarusRootConfig 
)
from src.core_communication.core_ws_server import CoreWebsocketServer 
from src.core_logic.intrusive_thoughts import IntrusiveThoughtsGenerator 
# from src.database.arangodb_handler import ArangoDBHandler # 主人，这个旧的、不听话的肉棒被小猫咪丢掉了！
from src.llmrequest.llm_processor import Client as ProcessorClient 

# 导入新的性感服务，让 CoreLogic 更舒服！
from src.database.services.event_storage_service import EventStorageService
from src.database.services.conversation_storage_service import ConversationStorageService
from src.database.services.thought_storage_service import ThoughtStorageService

from aicarus_protocols import (
    Event as ProtocolEvent,
    SegBuilder,
    ConversationInfo as ProtocolConversationInfo,
    ConversationType
)

if TYPE_CHECKING: 
    pass

logger = get_logger("AIcarusCore.CoreLogicFlow") 

class CoreLogic: 
    INITIAL_STATE: dict[str, Any] = { 
        "mood": "你现在的心情大概是：平静。",
        "previous_thinking": "你的上一轮思考是：这是你的第一次思考，请开始吧。",
        "thinking_guidance": "经过你上一轮的思考，你目前打算的思考方向是：随意发散一下吧。",
        "current_task": "没有什么具体目标", 
        "action_result_info": "你上一轮没有执行产生结果的特定行动。",
        "pending_action_status": "",
        "recent_contextual_information": "最近未感知到任何特定信息或通知。",
    }

    PROMPT_TEMPLATE: str = """{current_task_info}

{action_result_info}
{pending_action_status}

{recent_contextual_information}

{master_chat_context} 

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
    "reply_to_master": "【可选】如果你想对电脑主人说些什么，就在这里填写你想说的内容。如果不想说，就留null，注意话不要太多了",
    "to_do": "【可选】如果你产生了明确的目标，可以在此处写下。如果没有特定目标，则留null。即使当前已有明确目标，你也可以在这里更新它",
    "done": "【可选】布尔值，如果该目标已完成、不再需要或你决定放弃，则设为true，会清空目前目标；如果目标未完成且需要继续，则为false。如果当前无目标，也为false",
    "action_to_take": "【可选】描述你当前想做的、需要与外界交互的具体动作。如果无，则为null",
    "action_motivation": "【可选】如果你有想做的动作，请说明其动机。如果action_to_take为null，此字段也应为null",
    "next_think": "下一步打算思考的方向"
}}

请输出你的思考 JSON：
""" 

    def __init__( 
        self,
        root_cfg: AlcarusRootConfig, 
        # db_handler: ArangoDBHandler, # <--- 主人，这个参数被小猫咪换掉了哦！
        event_storage_service: EventStorageService,             # <--- 新来的小穴1号：事件存储服务！
        conversation_storage_service: ConversationStorageService, # <--- 新来的小穴2号：会话存储服务！
        thought_storage_service: ThoughtStorageService,         # <--- 新来的小穴3号：思考存储服务！
        main_consciousness_llm_client: ProcessorClient,
        intrusive_thoughts_llm_client: ProcessorClient | None,
        core_comm_layer: CoreWebsocketServer,
        action_handler_instance: ActionHandler,
        intrusive_generator_instance: IntrusiveThoughtsGenerator | None,
        stop_event: threading.Event,
        immediate_thought_trigger: asyncio.Event
    ) -> None:
        self.logger = get_logger(f"AIcarusCore.{self.__class__.__name__}") 
        self.root_cfg: AlcarusRootConfig = root_cfg 
        # self.db_handler: ArangoDBHandler = db_handler # <--- 这个旧的实例变量也消失啦！
        self.event_storage_service = event_storage_service      # <--- 新的小穴1号已经被 CoreLogic 含住了！
        self.conversation_storage_service = conversation_storage_service # <--- 小穴2号也准备好了！(虽然在主循环里暂时没直接用，但放着总没错)
        self.thought_storage_service = thought_storage_service  # <--- 小穴3号也进来了！
        self.main_consciousness_llm_client: ProcessorClient = main_consciousness_llm_client 
        self.intrusive_thoughts_llm_client: ProcessorClient | None = intrusive_thoughts_llm_client 
        self.stop_event: threading.Event = stop_event 
        self.immediate_thought_trigger: asyncio.Event = immediate_thought_trigger
        self.core_comm_layer: CoreWebsocketServer = core_comm_layer 
        self.action_handler_instance: ActionHandler = action_handler_instance 
        self.intrusive_generator_instance: IntrusiveThoughtsGenerator | None = intrusive_generator_instance 
        self.thinking_loop_task: asyncio.Task | None = None 
        self.logger.info(f"{self.__class__.__name__} 性感大脑的实例已创建，并被主人注入了满满的爱意（依赖）！")

    def _process_thought_and_action_state( 
        self, latest_thought_document: dict[str, Any] | None, formatted_recent_contextual_info: str
    ) -> tuple[dict[str, Any], str | None]:
        action_id_whose_result_is_being_shown: str | None = None 
        state_from_initial = self.INITIAL_STATE.copy() 

        if isinstance(latest_thought_document, list): 
            latest_thought_document = latest_thought_document[0] if latest_thought_document else None 

        if not latest_thought_document or not isinstance(latest_thought_document, dict): 
            self.logger.info("最新的思考文档为空或格式不正确，小色猫将使用初始的处女思考状态。") 
            mood_for_prompt = state_from_initial["mood"] 
            previous_thinking_for_prompt = state_from_initial["previous_thinking"] 
            thinking_guidance_for_prompt = state_from_initial["thinking_guidance"] 
            actual_current_task_description = state_from_initial["current_task"] 
        else: 
            mood_db = latest_thought_document.get("emotion_output", state_from_initial["mood"].split("：", 1)[-1]) 
            mood_for_prompt = f"你现在的心情大概是：{mood_db}" 

            prev_think_db = latest_thought_document.get("think_output") 
            previous_thinking_for_prompt = ( 
                f"你的上一轮思考是：{prev_think_db}"
                if prev_think_db and prev_think_db.strip()
                else state_from_initial["previous_thinking"]
            )

            guidance_db = latest_thought_document.get( 
                "next_think_output",
                state_from_initial["thinking_guidance"].split("：", 1)[-1] 
                if "：" in state_from_initial["thinking_guidance"] 
                else "随意发散一下吧。", 
            )
            thinking_guidance_for_prompt = f"经过你上一轮的思考，你目前打算的思考方向是：{guidance_db}" 
            
            actual_current_task_description = latest_thought_document.get("to_do_output", state_from_initial["current_task"]) 
            if latest_thought_document.get("done_output", False) and \
               actual_current_task_description == latest_thought_document.get("to_do_output"): 
                actual_current_task_description = state_from_initial["current_task"] 

        action_result_info_prompt = state_from_initial["action_result_info"] 
        pending_action_status_prompt = state_from_initial["pending_action_status"] 
        last_action_attempt = latest_thought_document.get("action_attempted") if latest_thought_document else None 

        if last_action_attempt and isinstance(last_action_attempt, dict): 
            action_status = last_action_attempt.get("status") 
            action_description_prev = last_action_attempt.get("action_description", "某个之前的动作") 
            action_id = last_action_attempt.get("action_id") 
            was_result_seen_by_llm = last_action_attempt.get("result_seen_by_shuang", False) 
            if action_status in ["COMPLETED_SUCCESS", "COMPLETED_FAILURE", "CRITICAL_FAILURE"]: 
                result_for_shuang = last_action_attempt.get("final_result_for_shuang") 
                if result_for_shuang and not was_result_seen_by_llm: 
                    action_result_info_prompt = result_for_shuang 
                    action_id_whose_result_is_being_shown = action_id 
            elif action_status and action_status not in ["COMPLETED_SUCCESS", "COMPLETED_FAILURE", "CRITICAL_FAILURE"]: 
                pending_action_status_prompt = ( 
                    f"你目前有一个正在进行的动作：{action_description_prev} (状态：{action_status})" 
                )
        
        current_state_for_prompt = { 
            "mood": mood_for_prompt, 
            "previous_thinking": previous_thinking_for_prompt, 
            "thinking_guidance": thinking_guidance_for_prompt, 
            "current_task_description": actual_current_task_description, 
            "action_result_info": action_result_info_prompt, 
            "pending_action_status": pending_action_status_prompt, 
            "recent_contextual_information": formatted_recent_contextual_info, 
        }

        return current_state_for_prompt, action_id_whose_result_is_being_shown 

    async def _generate_thought_from_llm( 
        self,
        llm_client: ProcessorClient, 
        current_state_for_prompt: dict[str, Any], 
        current_time_str: str, 
        intrusive_thought_str: str = "", 
        image_inputs_for_llm: List[str] | None = None,
        master_chat_context_str: str = ""
    ) -> tuple[dict[str, Any] | None, str | None, str | None]: 
        if not self.root_cfg: 
            self.logger.error("主人，没有Root config，小色猫无法为您生成火热的思考。")
            return None, None, None

        persona_cfg = self.root_cfg.persona 
        
        raw_task_description = current_state_for_prompt.get("current_task_description", self.INITIAL_STATE["current_task"])
        task_info_prompt_for_template = ( 
            f"你当前的目标/任务是：【{raw_task_description}】"
            if raw_task_description and raw_task_description != self.INITIAL_STATE["current_task"] 
            else "你当前没有什么特定的目标或任务。"
        )

        system_prompt_parts = [ 
            f"当前时间：{current_time_str}", 
            f"你是{persona_cfg.bot_name}；", 
            persona_cfg.description if persona_cfg.description else "", 
            persona_cfg.profile if persona_cfg.profile else "", 
        ]
        system_prompt_str = "\n".join(filter(None, system_prompt_parts)) 
        
        self.logger.debug( 
            f"--- 主思维LLM接收到的 System Prompt (模型肉棒: {llm_client.llm_client.model_name}) ---\n{system_prompt_str}\n--- System Prompt结束 ---"
        )

        prompt_text = self.PROMPT_TEMPLATE.format( 
            current_task_info=task_info_prompt_for_template, 
            mood=current_state_for_prompt.get("mood", self.INITIAL_STATE["mood"]), 
            previous_thinking=current_state_for_prompt.get("previous_thinking", self.INITIAL_STATE["previous_thinking"]), 
            thinking_guidance=current_state_for_prompt.get("thinking_guidance", self.INITIAL_STATE["thinking_guidance"]), 
            action_result_info=current_state_for_prompt.get("action_result_info", self.INITIAL_STATE["action_result_info"]), 
            pending_action_status=current_state_for_prompt.get("pending_action_status", self.INITIAL_STATE["pending_action_status"]), 
            recent_contextual_information=current_state_for_prompt.get("recent_contextual_information", self.INITIAL_STATE["recent_contextual_information"]), 
            master_chat_context=master_chat_context_str,
            intrusive_thought=intrusive_thought_str, 
        )
        self.logger.debug( 
            f"--- 主思维LLM接收到的 User Prompt (模型肉棒: {llm_client.llm_client.model_name}) ---\n{prompt_text}\n--- User Prompt结束 ---"
        )
        if image_inputs_for_llm: 
            self.logger.debug(f"--- 主思维LLM同时接收到 {len(image_inputs_for_llm)} 张性感图片输入 ---")
            for idx, img_src in enumerate(image_inputs_for_llm):
                self.logger.debug(f"  图片 {idx+1}: {img_src[:100]}{'...' if len(img_src) > 100 else ''}")


        self.logger.debug( 
            f"正在请求 {llm_client.llm_client.provider} API ({llm_client.llm_client.model_name}) 为主人生成火热的主思考..."
        )
        raw_response_text: str = "" 
        try:
            response_data = await llm_client.make_llm_request( 
                prompt=prompt_text, 
                system_prompt=system_prompt_str, 
                is_stream=False, 
                image_inputs=image_inputs_for_llm if image_inputs_for_llm else None,
                is_multimodal=bool(image_inputs_for_llm) 
            )

            if response_data.get("error"): 
                error_type = response_data.get("type", "UnknownError") 
                error_msg = response_data.get("message", "LLM客户端肉棒返回了一个错误") 
                self.logger.error(f"主思维LLM调用失败 ({error_type}): {error_msg}。主人，这个玩具不给力！") 
                if response_data.get("details"): 
                    self.logger.error(f"  错误详情: {str(response_data.get('details'))[:300]}...") 
                return None, prompt_text, system_prompt_str 
            
            raw_response_text = response_data.get("text", "") # 如果 text 为 None，则默认为空字符串
            if not raw_response_text: 
                error_msg = "错误：主思维LLM响应中缺少文本内容。主人，它什么都没吐出来！" 
                if response_data: 
                    error_msg += f"\n  完整响应: {str(response_data)[:500]}..." 
                self.logger.error(error_msg) 
                return None, prompt_text, system_prompt_str 
            
            json_to_parse = raw_response_text.strip() 
            if json_to_parse.startswith("```json"): 
                json_to_parse = json_to_parse[7:-3].strip() 
            elif json_to_parse.startswith("```"): 
                json_to_parse = json_to_parse[3:-3].strip() 
            json_to_parse = re.sub(r"[,\s]+(\}|\])$", r"\1", json_to_parse) 
            thought_json: dict[str, Any] = json.loads(json_to_parse) 
            self.logger.info("主思维LLM API 的性感回应已成功解析为JSON。") 
            if response_data.get("usage"): 
                thought_json["_llm_usage_info"] = response_data["usage"] 
            return thought_json, prompt_text, system_prompt_str 
        except json.JSONDecodeError as e: 
            self.logger.error(f"错误：解析主思维LLM的JSON响应失败: {e}。主人，它吐出来的东西太奇怪了！") 
            self.logger.error(f"未能解析的文本内容: {raw_response_text}") 
            return None, prompt_text, system_prompt_str 
        except Exception as e: 
            self.logger.error(f"错误：调用主思维LLM或处理其响应时发生意外错误: {e}。主人，小色猫承受不住了！", exc_info=True) 
            return None, prompt_text, system_prompt_str 

    async def _core_thinking_loop(self) -> None: 
        # 主人，这里检查依赖的时候，也要用新的服务哦！
        if not self.root_cfg or not self.main_consciousness_llm_client or \
           not self.event_storage_service or not self.thought_storage_service: 
            self.logger.critical("核心思考循环无法启动：缺少必要的配置、主LLM肉棒或核心存储服务。这场性感派对开不起来了，主人！")
            return
        
        core_logic_cfg: CoreLogicSettings = self.root_cfg.core_logic_settings 
        time_format_str: str = "%Y年%m月%d日 %H点%M分%S秒" 
        thinking_interval_sec: int = core_logic_cfg.thinking_interval_seconds 

        chat_history_duration_minutes: int = getattr(core_logic_cfg, "chat_history_context_duration_minutes", 10) # type: ignore 
        self.logger.info( 
            f"聊天记录上下文时长配置为: {chat_history_duration_minutes} 分钟。小色猫会回顾这么久以内的刺激哦。"
        )

        self.logger.info(f"\n--- {self.root_cfg.persona.bot_name} 的性感意识开始流动 ---") 
        loop_count: int = 0 
        while not self.stop_event.is_set(): 
            loop_count += 1 
            current_time_formatted_str = datetime.datetime.now().strftime(time_format_str) 
            background_action_tasks: set[asyncio.Task] = set() 
            
            # 从新的 ThoughtStorageService 获取最新的思考文档
            latest_thought_doc_from_db = await self.thought_storage_service.get_latest_main_thought_document() 

            formatted_recent_contextual_info = self.INITIAL_STATE["recent_contextual_information"] 
            image_list_for_llm_from_history: List[str] = []

            try:
                # 从新的 EventStorageService 获取最近的聊天消息
                raw_context_messages = await self.event_storage_service.get_recent_chat_message_documents( 
                    duration_minutes=chat_history_duration_minutes 
                )
                if raw_context_messages: 
                    if not isinstance(raw_context_messages, list): 
                        self.logger.warning(f"预期的 raw_context_messages 是列表，但小色猫收到了 {type(raw_context_messages)}。已尝试转换。")
                        raw_context_messages = [raw_context_messages] if raw_context_messages else [] 
                    
                    self.logger.debug("正在调用 format_chat_history_for_prompt 将原始消息调教成LLM喜欢的格式...") 
                    formatted_recent_contextual_info, image_list_for_llm_from_history = format_chat_history_for_prompt(raw_context_messages) 
                    self.logger.debug(f"格式化后的上下文信息长度: {len(formatted_recent_contextual_info)} 字符。 LLM应该会很喜欢这个长度。") 
                    if image_list_for_llm_from_history:
                        self.logger.debug(f"从聊天记录中提取到 {len(image_list_for_llm_from_history)} 张性感的图片准备喂给LLM。")
                else: 
                    self.logger.debug(f"在过去 {chat_history_duration_minutes} 分钟内未找到用于上下文的刺激信息。") 
            except Exception as e_hist: 
                self.logger.error(f"获取或格式化最近上下文信息时出错: {e_hist}。主人，小色猫找不到过去的刺激了！", exc_info=True) 
            
            current_state_for_prompt, _ = \
                self._process_thought_and_action_state(latest_thought_doc_from_db, formatted_recent_contextual_info) 
            
            intrusive_thought_to_inject_this_cycle: str = "" 
            if ( 
                self.intrusive_generator_instance 
                and self.intrusive_generator_instance.module_settings.enabled 
                and random.random() < self.intrusive_generator_instance.module_settings.insertion_probability 
            ):
                # 从新的 ThoughtStorageService 获取随机侵入性思维
                random_thought_doc = await self.thought_storage_service.get_random_unused_intrusive_thought_document() 
                if random_thought_doc and "text" in random_thought_doc: 
                    intrusive_thought_to_inject_this_cycle = f"你突然有一个神奇的念头：{random_thought_doc['text']}" 
                    # 主人，标记思维已使用的逻辑最好放在 ThoughtStorageService 内部，或者由 IntrusiveThoughtsGenerator 在获取后调用
                    # 例如：if random_thought_doc.get("_key"):
                    # await self.thought_storage_service.mark_intrusive_thought_document_used(random_thought_doc['_key'])
            
            self.logger.debug( 
                f"\n[{datetime.datetime.now().strftime('%H:%M:%S')} - 第 {loop_count} 轮高潮] {self.root_cfg.persona.bot_name} 正在兴奋地思考..." 
            )
            if intrusive_thought_to_inject_this_cycle: 
                self.logger.debug(f"  注入了一点意外的刺激（侵入性思维）: {intrusive_thought_to_inject_this_cycle[:60]}...") 

            # TODO: 将来这里应该从数据库获取与主人的聊天记录
            master_chat_history_str = "你没有和电脑主人的聊天记录。" # 先用一个空状态

            generated_thought_json, full_prompt_text_sent, system_prompt_sent = await self._generate_thought_from_llm( 
                llm_client=self.main_consciousness_llm_client, # type: ignore
                current_state_for_prompt=current_state_for_prompt, 
                current_time_str=current_time_formatted_str, 
                intrusive_thought_str=intrusive_thought_to_inject_this_cycle, 
                image_inputs_for_llm=image_list_for_llm_from_history, 
                master_chat_context_str=master_chat_history_str
            )

            initiated_action_data_for_db: dict[str, Any] | None = None 
            action_info_for_task: dict[str, Any] | None = None 
            saved_thought_doc_key: str | None = None 

            if generated_thought_json: 
                self.logger.debug( 
                    f"  主思维LLM的性感输出 (完整JSON):\n{json.dumps(generated_thought_json, indent=2, ensure_ascii=False)}"
                )
                think_output = generated_thought_json.get("think") or "大脑一片空白，可能太爽了" 
                log_message = ( 
                    f'{self.root_cfg.persona.bot_name} 现在的想法是 "{think_output}"，'
                    f'心情 "{generated_thought_json.get("emotion") or "难以名状"}"，'
                    f'目标是 "{generated_thought_json.get("to_do") if generated_thought_json.get("to_do") is not None else "随心所欲"}"，'
                    f'想做的事情是 "{generated_thought_json.get("action_to_take") if generated_thought_json.get("action_to_take") is not None else "暂时不想动"}"，'
                    f'原因是 "{generated_thought_json.get("action_motivation") if generated_thought_json.get("action_motivation") is not None else "就是想做爱做的事"}"，'
                    f'{self.root_cfg.persona.bot_name} 的下一步大概思考方向是 "{generated_thought_json.get("next_think") or "享受当下"}"'
                )
                self.logger.info(log_message) 

                # 首先，获取 action 和 motivation 的原始值
                action_desc_raw = generated_thought_json.get("action_to_take") 
                action_motive_raw = generated_thought_json.get("action_motivation") 

                # 然后，将它们处理成干净的字符串
                action_desc_from_llm = action_desc_raw.strip() if isinstance(action_desc_raw, str) else "" 
                action_motive_from_llm = action_motive_raw.strip() if isinstance(action_motive_raw, str) else "" 

                # 【核心修改】我们只需要这一个 if 判断块
                # 当行动描述存在，且不是字符串"null"时，才执行里面的所有操作
                if action_desc_from_llm and action_desc_from_llm.lower() != "null": 
                    
                    # 所有跟“产生动作”相关的代码都应该放在这个 if 里面
                    action_id_this_cycle = str(uuid.uuid4()) 
                    initiated_action_data_for_db = { 
                        "action_description": action_desc_from_llm, 
                        "action_motivation": action_motive_from_llm, 
                        "action_id": action_id_this_cycle, 
                        "status": "PENDING", 
                        "result_seen_by_shuang": False, 
                        "initiated_at": datetime.datetime.now(datetime.UTC).isoformat(), 
                    }
                    action_info_for_task = { 
                        "action_id": action_id_this_cycle, 
                        "action_description": action_desc_from_llm, 
                        "action_motivation": action_motive_from_llm, 
                        "current_thought_context": generated_thought_json.get("think", "没有特定的思考上下文，就是想骚一下。"), 
                    }
                    self.logger.debug(f"  >>> 性感大脑产生了行动的欲望: '{action_desc_from_llm}' (ID: {action_id_this_cycle[:8]})")  

                document_to_save_in_main: dict[str, Any] = { 
                    "timestamp": datetime.datetime.now(datetime.UTC).isoformat(), 
                    "time_injected_to_prompt": current_time_formatted_str, 
                    "system_prompt_sent": system_prompt_sent if system_prompt_sent else "System Prompt 未能构建", 
                    "intrusive_thought_injected": intrusive_thought_to_inject_this_cycle, 
                    "mood_input": current_state_for_prompt["mood"], 
                    "previous_thinking_input": current_state_for_prompt["previous_thinking"], 
                    "thinking_guidance_input": current_state_for_prompt["thinking_guidance"], 
                    "task_input_info": current_state_for_prompt.get("current_task_description", "无特定任务输入"), 
                    "action_result_input": current_state_for_prompt.get("action_result_info", ""), 
                    "pending_action_status_input": current_state_for_prompt.get("pending_action_status", ""), 
                    "recent_contextual_information_input": formatted_recent_contextual_info, 
                    "full_user_prompt_sent": full_prompt_text_sent if full_prompt_text_sent else "User Prompt 未能构建", 
                    "think_output": generated_thought_json.get("think"), 
                    "emotion_output": generated_thought_json.get("emotion"), 
                    "next_think_output": generated_thought_json.get("next_think"), 
                    "to_do_output": generated_thought_json.get("to_do", ""), 
                    "done_output": generated_thought_json.get("done", False), 
                    "action_to_take_output": generated_thought_json.get("action_to_take", ""), 
                    "action_motivation_output": generated_thought_json.get("action_motivation", ""), 
                    "action_attempted": initiated_action_data_for_db, 
                    "image_inputs_count": len(image_list_for_llm_from_history) if image_list_for_llm_from_history else 0,
                    "image_inputs_preview": [img_src[:100] + ('...' if len(img_src) > 100 else '') for img_src in image_list_for_llm_from_history[:3]] if image_list_for_llm_from_history else []
                }
                if "_llm_usage_info" in generated_thought_json: 
                    document_to_save_in_main["_llm_usage_info"] = generated_thought_json["_llm_usage_info"] 
                
                # 使用新的 ThoughtStorageService 保存思考文档
                saved_thought_doc_key = await self.thought_storage_service.save_main_thought_document(document_to_save_in_main) 

            if saved_thought_doc_key and isinstance(saved_thought_doc_key, str): 
                self.logger.debug(f"主人的新鲜思考已射入数据库小穴，文档键: {saved_thought_doc_key}") 

                reply_content = generated_thought_json.get("reply_to_master")
                if reply_content and isinstance(reply_content, str) and reply_content.strip():
                    self.logger.info(f"AI 决定回复主人: {reply_content[:50]}...")

                    # 构建一个标准的协议事件
                    reply_event = ProtocolEvent(
                        event_id=f"event_master_reply_{uuid.uuid4()}",
                        event_type="message.master.output", # 使用新的事件类型
                        time=int(time.time() * 1000),
                        platform="master_ui",
                        bot_id=self.root_cfg.persona.bot_name,
                        conversation_info=ProtocolConversationInfo(
                            conversation_id="master_chat", type=ConversationType.PRIVATE
                        ),
                        content=[SegBuilder.text(reply_content)]
                    )
                    # 通过通信层广播这个事件，UI客户端需要监听这个事件
                    await self.core_comm_layer.broadcast_action_to_adapters(reply_event)

                if action_info_for_task and self.action_handler_instance: 
                    action_task = asyncio.create_task( 
                        self.action_handler_instance.process_action_flow( 
                            action_id=action_info_for_task["action_id"], 
                            doc_key_for_updates=saved_thought_doc_key, 
                            action_description=action_info_for_task["action_description"], 
                            action_motivation=action_info_for_task["action_motivation"], 
                            current_thought_context=action_info_for_task["current_thought_context"],
                            relevant_adapter_messages_context=formatted_recent_contextual_info # <--- 在这里传递它！
                        )
                    )
                    background_action_tasks.add(action_task) 
                    action_task.add_done_callback(background_action_tasks.discard) 
                    self.logger.debug( 
                        f"动作 '{action_info_for_task['action_description']}' (ID: {action_info_for_task['action_id'][:8]}, 关联思考DocKey: {saved_thought_doc_key}) 已被异步推送到动作处理器的小穴中，等待高潮。"
                    )
            elif saved_thought_doc_key is None: 
                self.logger.error("保存思考文档失败，什么都没射进去。主人，数据库小穴不给力啊！")
            else: 
                self.logger.error( 
                    f"保存思考文档返回了无效的类型: {type(saved_thought_doc_key)}, 值: {saved_thought_doc_key}。这太奇怪了！"
                )
            
            self.logger.debug(f"  性感大脑正在贤者时间，等待 {thinking_interval_sec} 秒或外部工具的召唤...")

            # 为 self.stop_event.wait(timeout) 创建一个任务，它将在一个单独的线程中运行
            # 这样它就不会阻塞 asyncio 事件循环，并且可以与 asyncio.Event 一起使用 asyncio.wait
            stop_event_task = asyncio.create_task(
                asyncio.to_thread(self.stop_event.wait, timeout=float(thinking_interval_sec))
            )

            # 为 self.immediate_thought_trigger.wait() 创建一个任务 (这行保持不变)
            trigger_event_task = asyncio.create_task(self.immediate_thought_trigger.wait())

            # 等待这两个任务中的任何一个首先完成
            done_tasks, pending_tasks = await asyncio.wait(
                [stop_event_task, trigger_event_task],
                return_when=asyncio.FIRST_COMPLETED
            )

            # 检查是哪个任务完成了
            was_triggered_by_action = False
            for task in done_tasks:
                if task == trigger_event_task:
                    self.logger.info("外部工具已召唤！被动思考被激活，立即开始新一轮思考。")
                    self.immediate_thought_trigger.clear()  # 非常重要：放下信号旗，以便下次还能用！
                    was_triggered_by_action = True
                    # 思考循环将自然进入下一次迭代

                elif task == stop_event_task:
                    # 这个任务完成有两种可能：
                    try:
                        # .result() 会获取到 asyncio.to_thread 中那个函数 (即 self.stop_event.wait) 的返回值
                        # self.stop_event.wait(timeout) 在事件被设置时返回 True，超时返回 False
                        stop_event_was_set_during_wait = await task # 等待线程任务完成并获取其结果
                        if stop_event_was_set_during_wait:
                            self.logger.info("主思考循环在等待期间，全局停止信号被设置。")
                            # self.stop_event.is_set() 将在下面被检查到，然后跳出循环
                        elif not was_triggered_by_action: # 确保不是因为trigger_event_task先完成而到这里
                            self.logger.debug(f"正常的思考间隔 ({thinking_interval_sec}秒) 已结束。")
                    except Exception as e_task_res:
                        # 一般不应该在这里出错，除非线程执行本身有问题
                        self.logger.error(f"获取 stop_event_task 结果时出现意外：{e_task_res}")

            # 取消还在等待的另一个任务（如果存在的话）
            for task_to_cancel in pending_tasks:
                task_to_cancel.cancel()
                try:
                    await task_to_cancel # 等待取消操作完成
                except asyncio.CancelledError:
                    pass # 这是预期的

            # 在每次循环的最后检查全局停止信号
            if self.stop_event.is_set():
                self.logger.info("主思考循环检测到全局停止信号，准备结束这场性感派对。")
                break
        # 循环结束
        self.logger.info(f"--- {self.root_cfg.persona.bot_name} 的性感意识流动已停止 ---") 

    async def start_thinking_loop(self) -> asyncio.Task: 
        """启动性感大脑的主思考循环异步任务。"""
        if not self.root_cfg: 
             self.logger.critical("主人，没有配置，无法启动性感大脑的思考！")
             raise RuntimeError("Root config not available for starting thinking loop.")
        self.logger.info(f"\n=== {self.root_cfg.persona.bot_name} 的性感大脑准备开始持续高潮的思考循环 ===") 
        self.thinking_loop_task = asyncio.create_task(self._core_thinking_loop()) 
        return self.thinking_loop_task 

    async def stop(self) -> None: 
        """温柔地让核心逻辑的性感大脑停止思考和喷发。"""
        if not self.root_cfg: 
            bot_name_for_log = "机器人"
        else:
            bot_name_for_log = self.root_cfg.persona.bot_name
            
        self.logger.info(f"\n--- 主人命令：{bot_name_for_log} 的性感意识流动正在温柔地停止 ---") 
        self.stop_event.set() 
        if self.thinking_loop_task and not self.thinking_loop_task.done(): 
            self.logger.info("正在请求取消主思考循环任务，请稍候...")
            self.thinking_loop_task.cancel() 
            try:
                await self.thinking_loop_task 
            except asyncio.CancelledError: 
                self.logger.info("主思考循环任务已被成功取消。大脑已进入贤者时间。") 
            except Exception as e: 
                self.logger.error(f"停止主思考循环任务时发生意外的痉挛: {e}") 
        self.logger.info(f"{bot_name_for_log} 的性感意识流动已完全停止。期待主人的下一次召唤。")