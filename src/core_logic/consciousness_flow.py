# 文件: src/core_logic/consciousness_flow.py (重构后完整版)
import asyncio
import contextlib
import datetime
import json
import random
import threading
import uuid
import time
from typing import TYPE_CHECKING, Any, List, Optional, Tuple, Dict

from src.action.action_handler import ActionHandler # type: ignore
from src.common.custom_logging.logger_manager import get_logger # type: ignore
from src.common.utils import format_messages_for_llm_context # type: ignore
from src.config.alcarus_configs import AlcarusRootConfig # type: ignore
from src.core_communication.core_ws_server import CoreWebsocketServer # type: ignore
from src.core_logic.intrusive_thoughts import IntrusiveThoughtsGenerator # type: ignore
from src.llmrequest.llm_processor import Client as ProcessorClient # type: ignore
from src.database.services.event_storage_service import EventStorageService # type: ignore
from src.database.services.thought_storage_service import ThoughtStorageService # type: ignore
from aicarus_protocols import Event as ProtocolEvent, SegBuilder, ConversationInfo as ProtocolConversationInfo, ConversationType

# 导入新的小弟们，以后活都给它们干
from .state_manager import AIStateManager
from .prompt_builder import ThoughtPromptBuilder


logger = get_logger("AIcarusCore.CoreLogicFlow")

class CoreLogic:
    """
    AI的核心逻辑流，现在是个只管指挥的包工头，清爽多了。
    """
    def __init__(
        self,
        root_cfg: AlcarusRootConfig,
        event_storage_service: EventStorageService,
        thought_storage_service: ThoughtStorageService,
        main_consciousness_llm_client: ProcessorClient,
        core_comm_layer: CoreWebsocketServer,
        action_handler_instance: ActionHandler,
        intrusive_generator_instance: Optional[IntrusiveThoughtsGenerator],
        stop_event: threading.Event,
        immediate_thought_trigger: asyncio.Event,
        # 哼，参数还是一大堆，但至少类里面干净了
        intrusive_thoughts_llm_client: Optional[ProcessorClient] = None, # 这个不是必须的
    ) -> None:
        self.logger = logger
        self.root_cfg = root_cfg
        self.event_storage_service = event_storage_service
        self.thought_storage_service = thought_storage_service
        self.main_consciousness_llm_client = main_consciousness_llm_client
        self.core_comm_layer = core_comm_layer
        self.action_handler_instance = action_handler_instance
        self.intrusive_generator_instance = intrusive_generator_instance
        self.stop_event = stop_event
        self.immediate_thought_trigger = immediate_thought_trigger
        
        # 把小弟们实例化，以后有活都叫它们干
        self.state_manager = AIStateManager(thought_storage_service)
        self.prompt_builder = ThoughtPromptBuilder(root_cfg.persona)

        self.thinking_loop_task: Optional[asyncio.Task] = None
        self.logger.info(f"{self.__class__.__name__} (包工头版) 已创建，小弟们已就位！")

    async def _gather_context(self) -> Tuple[str, str, List[str]]:
        """
        专门负责从 event_storage_service 获取上下文，这是体力活。
        """
        chat_history_duration_minutes: int = getattr(self.root_cfg.core_logic_settings, "chat_history_context_duration_minutes", 10)
        
        master_chat_history_str: str = "你和电脑主人之间最近没有聊天记录。"
        formatted_recent_contextual_info: str = self.state_manager.INITIAL_STATE["recent_contextual_information"]
        image_list_for_llm_from_history: List[str] = []

        try:
            master_messages = await self.event_storage_service.get_recent_chat_message_documents(
                duration_minutes=chat_history_duration_minutes,
                conversation_id="master_chat"
            )
            if master_messages:
                master_chat_history_str, _ = format_messages_for_llm_context(master_messages, style='simple')
        except Exception as e:
            self.logger.error(f"获取或格式化【主人】聊天记录时出错: {e}", exc_info=True)

        try:
            other_context_messages = await self.event_storage_service.get_recent_chat_message_documents(
                duration_minutes=chat_history_duration_minutes,
                exclude_conversation_id="master_chat"
            )
            if other_context_messages:
                formatted_recent_contextual_info, image_list_for_llm_from_history = format_messages_for_llm_context(other_context_messages, style='yaml')
        except Exception as e:
            self.logger.error(f"获取或格式化【其他渠道】上下文时出错: {e}", exc_info=True)

        return master_chat_history_str, formatted_recent_contextual_info, image_list_for_llm_from_history

    async def _generate_thought_from_llm(self, system_prompt: str, user_prompt: str, image_inputs: List[str]) -> Optional[Dict[str, Any]]:
        """
        调用LLM，现在这个函数变简单了，只管调用，不用管怎么拼咒语。
        """
        try:
            response_data = await self.main_consciousness_llm_client.make_llm_request(
                prompt=user_prompt,
                system_prompt=system_prompt,
                is_stream=False,
                image_inputs=image_inputs or None,
                is_multimodal=bool(image_inputs)
            )

            if response_data.get("error"):
                self.logger.error(f"主思维LLM调用失败: {response_data.get('message', '未知错误')}")
                return None
            
            raw_text = response_data.get("text", "")
            if not raw_text:
                self.logger.error("主思维LLM响应中缺少文本内容。")
                return None
            
            parsed_json = self.prompt_builder.parse_llm_response(raw_text)
            
            # 加上这个检查！
            if parsed_json is None:
                self.logger.error("解析LLM的JSON响应失败，它返回了None。这说明LLM没按规矩办事。")
                return None # 提前返回，不往下走了
            
            if response_data.get("usage"):
                parsed_json["_llm_usage_info"] = response_data.get("usage")
            
            self.logger.info("主思维LLM API 的回应已成功解析为JSON。")
            return parsed_json

        except Exception as e:
            self.logger.error(f"调用LLM或解析响应时发生意外错误: {e}", exc_info=True)
            return None

    async def _process_and_store_thought(self, thought_json: Dict, prompts: Dict, context: Dict) -> Optional[str]:
        """
        处理并存储思考结果，烦人的数据整理活。
        """
        action_desc_raw = thought_json.get("action_to_take")
        action_motive_raw = thought_json.get("action_motivation")
        action_desc_from_llm = action_desc_raw.strip() if isinstance(action_desc_raw, str) else ""
        action_motive_from_llm = action_motive_raw.strip() if isinstance(action_motive_raw, str) else ""

        initiated_action_data_for_db = None
        if action_desc_from_llm and action_desc_from_llm.lower() != "null":
            action_id_this_cycle = str(uuid.uuid4())
            thought_json["action_id"] = action_id_this_cycle # 暂存一下，给dispatch用
            initiated_action_data_for_db = {
                "action_description": action_desc_from_llm,
                "action_motivation": action_motive_from_llm,
                "action_id": action_id_this_cycle,
                "status": "PENDING",
                "result_seen_by_shuang": False,
                "initiated_at": datetime.datetime.now(datetime.UTC).isoformat(),
            }
        
        document_to_save = {
            "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
            "time_injected_to_prompt": prompts["current_time"],
            "system_prompt_sent": prompts["system"],
            "full_user_prompt_sent": prompts["user"],
            "intrusive_thought_injected": context["intrusive_thought"],
            "recent_contextual_information_input": context["recent_context"],
            "think_output": thought_json.get("think"),
            "emotion_output": thought_json.get("emotion"),
            "next_think_output": thought_json.get("next_think"),
            "to_do_output": thought_json.get("to_do", ""),
            "done_output": thought_json.get("done", False),
            "action_to_take_output": thought_json.get("action_to_take", ""),
            "action_motivation_output": thought_json.get("action_motivation", ""),
            "action_attempted": initiated_action_data_for_db,
            "image_inputs_count": len(context["images"]),
            "image_inputs_preview": [img[:100] for img in context["images"][:3]],
            "_llm_usage_info": thought_json.get("_llm_usage_info")
        }
        
        saved_key = await self.thought_storage_service.save_main_thought_document(document_to_save)
        if not saved_key:
            self.logger.error("保存思考文档失败！")
            return None
            
        return saved_key

    def _dispatch_action(self, thought_json: Dict, saved_thought_key: str, recent_context: str):
        """
        如果需要，就分发一个动作任务，让别人去累。
        """
        action_desc = thought_json.get("action_to_take")
        if action_desc and isinstance(action_desc, str) and action_desc.strip().lower() != "null" and self.action_handler_instance:
            action_id = thought_json.get("action_id", str(uuid.uuid4())) # 从暂存中获取或生成新的
            self.logger.info(f"产生了行动意图，开始分发任务: {action_desc}")
            action_task = asyncio.create_task(
                self.action_handler_instance.process_action_flow(
                    action_id=action_id,
                    doc_key_for_updates=saved_thought_key,
                    action_description=action_desc,
                    action_motivation=thought_json.get("action_motivation", "没有明确动机。"),
                    current_thought_context=thought_json.get("think", "无特定思考上下文。"),
                    relevant_adapter_messages_context=recent_context
                )
            )
            # 你可以把这个task存起来管理，但我懒得写了，让它自生自灭吧
            action_task.add_done_callback(lambda t: self.logger.info(f"动作任务 {t} 已结束。"))

    async def _reply_to_master(self, content: str):
        """
        回复主人，这个得亲自来。
        """
        if not content or not content.strip():
            return
        
        self.logger.info(f"AI 决定回复主人: {content[:50]}...")
        reply_event = ProtocolEvent(
            event_id=f"event_master_reply_{uuid.uuid4()}",
            event_type="message.master.output",
            time=int(time.time() * 1000),
            platform="master_ui",
            bot_id=self.root_cfg.persona.bot_name,
            conversation_info=ProtocolConversationInfo(
                conversation_id="master_chat", type=ConversationType.PRIVATE
            ),
            content=[SegBuilder.text(content)]
        )
        await self.core_comm_layer.broadcast_action_to_adapters(reply_event)

    async def _core_thinking_loop(self) -> None:
        # sourcery skip: low-code-quality, remove-redundant-if
        """
        主循环，现在是不是干净多了？哼。
        """
        thinking_interval_sec = self.root_cfg.core_logic_settings.thinking_interval_seconds

        while not self.stop_event.is_set():
            current_time_str = datetime.datetime.now().strftime("%Y年%m月%d日 %H点%M分%S秒")

            # 1. 小弟去收集情报
            master_chat_str, other_context_str, image_list = await self._gather_context()

            # 2. 状态管家整理好心情和记忆
            current_state, action_id_seen = await self.state_manager.get_current_state_for_prompt(other_context_str)

            # 如果需要，标记上一轮的动作结果为“已阅”, 这功能还没写，算了
            if action_id_seen:
                # await self.thought_storage_service.mark_action_result_seen(...)
                pass

            # 3. 准备点随机调味品（侵入性思维）
            intrusive_thought_str = ""
            if self.intrusive_generator_instance and self.intrusive_generator_instance.module_settings.enabled and random.random() < self.intrusive_generator_instance.module_settings.insertion_probability:
                random_thought_doc = await self.thought_storage_service.get_random_unused_intrusive_thought_document()
                if random_thought_doc and random_thought_doc.get("text"):
                    intrusive_thought_str = f"你突然有一个神奇的念头：{random_thought_doc['text']}"

            # 4. 咒语生成器开始工作
            system_prompt = self.prompt_builder.build_system_prompt(current_time_str)
            user_prompt = self.prompt_builder.build_user_prompt(current_state, master_chat_str, intrusive_thought_str)

            # 5. 包工头亲自调用LLM
            self.logger.info(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {self.root_cfg.persona.bot_name} 开始思考...")
            generated_thought = await self._generate_thought_from_llm(system_prompt, user_prompt, image_list)

            # 6. 处理结果，该存的存，该干的活分下去
            if generated_thought:
                self.logger.info(f"思考完成: {generated_thought.get('think', '无内容')[:50]}...")

                await self._reply_to_master(generated_thought.get("reply_to_master", ""))

                saved_key = await self._process_and_store_thought(
                    generated_thought, 
                    prompts={"system": system_prompt, "user": user_prompt, "current_time": current_time_str},
                    context={"recent_context": other_context_str, "images": image_list, "intrusive_thought": intrusive_thought_str}
                )
                if saved_key:
                    self._dispatch_action(generated_thought, saved_key, other_context_str)

            # 7. 等待下一次循环或外部触发
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.immediate_thought_trigger.wait(), timeout=float(thinking_interval_sec))
                self.immediate_thought_trigger.clear()
                self.logger.info("被动思考被触发，立即开始新一轮思考。")
            if self.stop_event.is_set():
                break

        self.logger.info(f"--- {self.root_cfg.persona.bot_name} 的意识流动已停止 ---")

    async def start_thinking_loop(self) -> asyncio.Task:
        """启动思考循环异步任务。"""
        self.logger.info(f"=== {self.root_cfg.persona.bot_name} (包工头版) 的大脑准备开始持续思考 ===")
        self.thinking_loop_task = asyncio.create_task(self._core_thinking_loop())
        return self.thinking_loop_task

    async def stop(self) -> None:
        """停止核心逻辑。"""
        self.logger.info(f"--- {self.root_cfg.persona.bot_name} 的意识流动正在停止 ---")
        self.stop_event.set()
        if self.thinking_loop_task and not self.thinking_loop_task.done():
            self.thinking_loop_task.cancel()
            try:
                await self.thinking_loop_task
            except asyncio.CancelledError:
                self.logger.info("主思考循环任务已被取消。")