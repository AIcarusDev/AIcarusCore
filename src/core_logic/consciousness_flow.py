# 文件: src/core_logic/consciousness_flow.py
import asyncio
import contextlib
import datetime
import threading
import uuid
from typing import TYPE_CHECKING, Any

from src.action.action_handler import ActionHandler
from src.common.custom_logging.logging_config import get_logger
from src.common.time_utils import get_formatted_time_for_llm
from src.config import config
from src.core_communication.core_ws_server import CoreWebsocketServer
from src.core_logic.context_builder import ContextBuilder
from src.core_logic.intrusive_thoughts import IntrusiveThoughtsGenerator
from src.core_logic.prompt_builder import ThoughtPromptBuilder
from src.core_logic.state_manager import AIStateManager
from src.core_logic.thought_generator import ThoughtGenerator
from src.core_logic.thought_persistor import ThoughtPersistor
from src.database.models import ThoughtChainDocument
from src.database import ThoughtStorageService

if TYPE_CHECKING:
    from src.focus_chat_mode.chat_session_manager import ChatSessionManager

logger = get_logger(__name__)

CORE_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "mood": {"type": "string"},
        "think": {"type": "string"},
        "goal": {"type": "string"},
        "action": {
            "type": "object",
            "properties": {
                "core": {
                    "type": "object",
                    "properties": {
                        "web_search": {
                            "type": "object",
                            "properties": {
                                "query": {"type": "string"},
                                "motivation": {"type": "string"},
                            },
                            "required": ["query", "motivation"],
                        }
                    },
                },
                "napcat_qq": {
                    "type": "object",
                    "properties": {
                        "focus": {
                            "type": "object",
                            "properties": {
                                "conversation_id": {"type": "string"},
                                "motivation": {"type": "string"},
                            },
                            "required": ["conversation_id", "motivation"],
                        },
                        "get_list": {
                            "type": "object",
                            "properties": {
                                "list_type": {"type": "string"},
                                "motivation": {"type": "string"},
                            },
                            "required": ["list_type", "motivation"],
                        },
                    },
                },
            },
        },
    },
    "required": ["mood", "think", "goal"],
}


class CoreLogic:
    def __init__(
        self,
        core_comm_layer: CoreWebsocketServer,
        action_handler_instance: ActionHandler,
        state_manager: AIStateManager,
        chat_session_manager: "ChatSessionManager",
        context_builder: ContextBuilder,
        thought_storage_service: ThoughtStorageService,
        thought_generator: ThoughtGenerator,
        thought_persistor: ThoughtPersistor,
        prompt_builder: ThoughtPromptBuilder,
        stop_event: threading.Event,
        immediate_thought_trigger: asyncio.Event,
        intrusive_generator_instance: IntrusiveThoughtsGenerator | None = None,
    ) -> None:
        self.core_comm_layer = core_comm_layer
        self.action_handler_instance = action_handler_instance
        self.state_manager = state_manager
        self.chat_session_manager = chat_session_manager
        self.context_builder = context_builder
        self.thought_generator = thought_generator
        self.thought_persistor = thought_persistor
        self.thought_storage_service = thought_storage_service # 把存储服务也存起来
        self.prompt_builder = prompt_builder
        self.stop_event = stop_event
        self.immediate_thought_trigger = immediate_thought_trigger
        self.focus_session_inactive_event = asyncio.Event()
        self.intrusive_generator_instance = intrusive_generator_instance
        self.thinking_loop_task: asyncio.Task | None = None
        logger.info(f"{self.__class__.__name__} 已创建")

    def trigger_immediate_thought_cycle(self) -> None:
        """
        这个方法现在就是个闹钟，只负责把主循环叫醒。
        """
        logger.info("接收到立即思考触发信号，主意识将被唤醒。")
        self.immediate_thought_trigger.set()

    async def _dispatch_action(self, thought_pearl: ThoughtChainDocument) -> bool:
        """
        根据思想点里的 action_payload 分发动作。
        哼，我来当老大，专注指令我亲自处理！
        返回一个布尔值，指示是否执行了 focus 动作。
        """
        action_payload = thought_pearl.action_payload
        if not action_payload or not isinstance(action_payload, dict):
            logger.info("当前思想点未指定任何行动。")
            return False

        action_id = thought_pearl.action_id
        saved_thought_key = thought_pearl._key

        focus_params = action_payload.get("napcat_qq", {}).get("focus")

        if focus_params and isinstance(focus_params, dict):
            logger.info("主意识截获 'focus' 指令，准备亲自处理会话激活。")
            target_conv_id = focus_params.get("conversation_id")
            motivation = focus_params.get("motivation", "没有明确动机")

            if not target_conv_id:
                logger.error("'focus' 动作缺少 conversation_id，无法激活。")
                return False

            try:
                unread_convs = await self.prompt_builder.unread_info_service.get_structured_unread_conversations()
                target_conv_details = next(
                    (c for c in unread_convs if c.get("conversation_id") == target_conv_id), None
                )

                if not target_conv_details:
                    logger.error(f"无法激活会话 '{target_conv_id}'，因为它不在未读列表中。")
                    return False

                await self.chat_session_manager.activate_session_by_id(
                    conversation_id=target_conv_id,
                    core_motivation=motivation,
                    platform=target_conv_details["platform"],
                    conversation_type=target_conv_details["type"],
                )

                if "focus" in action_payload.get("napcat_qq", {}):
                    del action_payload["napcat_qq"]["focus"]
                if not action_payload.get("napcat_qq"):
                    del action_payload["napcat_qq"]

                # 既然是 focus，那就返回 True
                return True

            except Exception as e:
                logger.error(f"主意识在处理 'focus' 指令时发生错误: {e}", exc_info=True)
                return False

        # 把剩下的垃圾（如果有的话）丢给ActionHandler去处理。
        if action_payload:
            await self.action_handler_instance.process_action_flow(
                action_id=action_id,
                doc_key_for_updates=saved_thought_key, # 这个参数现在可以考虑去掉了，因为动作日志是独立的
                action_json=action_payload,
            )

        # 如果不是 focus 动作，就返回 False
        return False

    async def _core_thinking_loop(self) -> None:
        thinking_interval_sec = config.core_logic_settings.thinking_interval_seconds
        while not self.stop_event.is_set():
            if self.chat_session_manager and self.chat_session_manager.is_any_session_active():
                logger.debug("检测到有专注会话激活，主意识暂停，等待所有专注会话结束...")
                try:
                    await self.focus_session_inactive_event.wait()
                    self.focus_session_inactive_event.clear()
                    logger.info("所有专注会话已结束，主意识被唤醒，继续思考。")
                except asyncio.CancelledError:
                    logger.info("主意识在等待专注会话结束时被取消。")
                    break
                continue

            # 1. 构建 Prompt (它内部自己会去拿最新的状态，我们不用管了)
            current_time_str = get_formatted_time_for_llm()
            system_prompt, user_prompt, _ = await self.prompt_builder.build_prompts(current_time_str)

            # 2. 生成思考
            logger.info(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {config.persona.bot_name} 开始思考...")
            generated_thought_json = await self.thought_generator.generate_thought(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                image_inputs=[],
                response_schema=CORE_RESPONSE_SCHEMA,
            )

            if generated_thought_json:
                # 3. 把思考结果打包成一颗新的“思想点”
                action_payload = generated_thought_json.get("action")
                action_id = str(uuid.uuid4()) if action_payload else None

                new_thought_pearl = ThoughtChainDocument(
                    _key=str(uuid.uuid4()),
                    timestamp=datetime.datetime.now(datetime.UTC).isoformat(),
                    mood=generated_thought_json.get("mood", "平静"),
                    think=generated_thought_json.get("think", "无"),
                    goal=generated_thought_json.get("goal"),
                    source_type='core',
                    source_id=None,
                    action_id=action_id,
                    action_payload=action_payload
                )

                # 4. 把点串到链上去！
                saved_key = await self.thought_storage_service.save_thought_and_link(new_thought_pearl)

                if saved_key:
                    # 5. 分发动作
                    was_focus_triggered = await self._dispatch_action(new_thought_pearl)
                    if was_focus_triggered:
                        continue
                else:
                    logger.error("严重逻辑错误：思想点未能成功串入思想链，无法分发动作！")

            # 6. 等待下一次闹钟
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.immediate_thought_trigger.wait(), timeout=float(thinking_interval_sec))
                self.immediate_thought_trigger.clear()
                logger.info("被动思考被触发，立即开始新一轮思考。")

            if self.stop_event.is_set():
                break
        logger.info(f"--- {config.persona.bot_name} 的意识流动已停止 ---")

    async def start_thinking_loop(self) -> asyncio.Task:
        logger.info(f"=== {config.persona.bot_name} (意识流版) 的大脑准备开始持续思考 ===")
        self.thinking_loop_task = asyncio.create_task(self._core_thinking_loop())
        return self.thinking_loop_task

    async def stop(self) -> None:
        logger.info(f"--- {config.persona.bot_name} 的意识流动正在停止 ---")
        self.stop_event.set()
        if self.thinking_loop_task and not self.thinking_loop_task.done():
            self.thinking_loop_task.cancel()
            try:
                await self.thinking_loop_task
            except asyncio.CancelledError:
                logger.info("主思考循环任务已被取消。")

    async def _activate_new_focus_session_from_core(self, target_conv_id: str) -> None:
        """
        从 CoreLogic 内部直接激活一个新的专注会话。
        这个方法是给 LLMResponseHandler 调用的，用于 LLM 决策直接转移专注。
        """
        logger.info(f"CoreLogic 接收到直接激活新专注会话的请求: {target_conv_id}")
        # 构建一个模拟的 action_payload，让 _dispatch_action 去处理
        mock_action_payload = {
            "napcat_qq": {
                "focus": {
                    "conversation_id": target_conv_id,
                    "motivation": "LLM 决策直接转移专注",
                }
            }
        }
        # 创建一个临时的 ThoughtChainDocument，只包含 action_payload
        # 其他字段不重要，因为 _dispatch_action 只关心 action_payload
        mock_thought_pearl = ThoughtChainDocument(
            _key=str(uuid.uuid4()),
            timestamp=datetime.datetime.now(datetime.UTC).isoformat(),
            mood="平静",
            think="根据LLM指令激活新专注会话",
            goal="激活指定会话",
            source_type='core',
            source_id=None,
            action_id=str(uuid.uuid4()),
            action_payload=mock_action_payload
        )
        await self._dispatch_action(mock_thought_pearl)
