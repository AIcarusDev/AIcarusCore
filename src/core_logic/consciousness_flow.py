# 文件: src/core_logic/consciousness_flow.py
import asyncio
import contextlib
import datetime
import threading
import uuid
from typing import TYPE_CHECKING

from src.core_logic.decision_dispatcher import process_llm_decision
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
from src.database import ThoughtStorageService
from src.database.models import ThoughtChainDocument
from src.platform_builders.registry import platform_builder_registry

if TYPE_CHECKING:
    from src.focus_chat_mode.chat_session_manager import ChatSessionManager

logger = get_logger(__name__)


class CoreLogic:
    """核心逻辑处理类，负责主思考循环和动作分发.

    这个类负责管理整个意识流动的生命周期，包括思考循环的启动、停止以及在不同思考状态之间的切换.
    它还处理来自 LLM 的指令，并根据指令执行相应的动作，如激活专注会话等.

    Attributes:
        core_comm_layer (CoreWebsocketServer): 核心通信层，用于处理与其他组件的通信.
        action_handler_instance (ActionHandler): 动作处理器实例，用于处理各种动作指令.
        state_manager (AIStateManager): 状态管理器实例，用于获取当前 AI 的状态信息.
        chat_session_manager (ChatSessionManager): 聊天会话管理器，用于管理聊天会话的激活和切换.
        context_builder (ContextBuilder): 上下文构建器实例，用于收集和格式化上下文信息.
        thought_generator (ThoughtGenerator): 思考生成器实例，用于生成新的思考内容.
        thought_persistor (ThoughtPersistor): 思考持久化器实例，用于将思考结果存储到数据库中.
        thought_storage_service (ThoughtStorageService): 思想存储服务实例，用于存储和检索思想点.
        prompt_builder (ThoughtPromptBuilder): 提示构建器实例，用于生成适合 LLM 的提示内容.
        stop_event (threading.Event): 用于控制思考循环的停止事件.
        immediate_thought_trigger (asyncio.Event): 用于触发立即思考循环的事件.
        intrusive_generator_instance (IntrusiveThoughtsGenerator | None): 可选的侵入性思考生成器实例
            ，用于处理特殊的思考任务.
        thinking_loop_task (asyncio.Task | None): 当前的思考循环任务，如果正在运行则为非 None.
    """

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
        self.thought_storage_service = thought_storage_service  # 把存储服务也存起来
        self.prompt_builder = prompt_builder
        self.stop_event = stop_event
        self.immediate_thought_trigger = immediate_thought_trigger
        self.focus_session_inactive_event = asyncio.Event()
        self.intrusive_generator_instance = intrusive_generator_instance
        self.thinking_loop_task: asyncio.Task | None = None
        self.last_known_internal_state: dict = AIStateManager.INITIAL_STATE.copy()
        logger.info(f"{self.__class__.__name__} 已创建")

    def trigger_immediate_thought_cycle(self) -> None:
        """这个方法现在就是个闹钟，只负责把主循环叫醒."""
        logger.info("接收到立即思考触发信号，主意识将被唤醒。")
        self.immediate_thought_trigger.set()

    async def _dispatch_action(self, thought_pearl: ThoughtChainDocument) -> bool:
        """处理思想点中的行动指令，特别是 'focus' 指令.

        这个方法会检查思想点的 action_payload，
        如果包含 'focus' 指令，则尝试激活指定的会话.

        Args:
            thought_pearl (ThoughtChainDocument): 包含行动指令的思想点对象.

        Returns:
            bool: 如果成功激活了会话，则返回 True；否则返回 False.
        """
        action_payload = thought_pearl.action_payload
        if not action_payload or not isinstance(action_payload, dict):
            logger.info("当前思想点未指定任何行动。")
            return False

        action_id = thought_pearl.action_id
        saved_thought_key = thought_pearl._key

        focus_params = action_payload.get("napcat_qq", {}).get("focus")

        if focus_params and isinstance(focus_params, dict):
                # 既然是 focus，那就返回 True
                return True

        # 把剩下的垃圾（如果有的话）丢给ActionHandler去处理。
        if action_payload:
            success, result_text, _ = await self.action_handler_instance.process_action_flow(
                action_id=action_id,
                doc_key_for_updates=saved_thought_key,
                action_json=action_payload,
            )

            if result_text:
                await self.thought_storage_service.save_action_result_to_thought(
                    thought_key=saved_thought_key,
                    result_text=result_text
                )

        # 如果不是 focus 动作，就返回 False
        return False

    async def _core_thinking_loop(self) -> None:
        """
        主思考循环，持续生成思考并处理动作.
        这个方法会持续运行，直到 stop_event 被设置为 True。
        它会定期生成新的思考，并根据思考结果执行相应的动作。
        在每次循环开始时，它会检查当前的 FocusManager 状态，
        如果当前有焦点会话，则暂停主意识，等待焦点返回顶层。
        """
        thinking_interval_sec = config.core_logic_settings.thinking_interval_seconds
        while not self.stop_event.is_set():
            # --- ▼▼▼ 这是我们的核心改造 ▼▼▼ ---
            # 每次循环前，都去问 FocusManager：“我现在应该在哪？”
            if self.chat_session_manager and self.chat_session_manager.current_focus_path is not None:
                logger.debug(
                    f"AI焦点在 '{self.chat_session_manager.current_focus_path}'，"
                    "主意识（Core-Level）暂停，等待焦点返回。"
                )
                try:
                    # 等待 focus_session_inactive_event 事件，这个事件会在焦点返回顶层时被设置
                    await self.focus_session_inactive_event.wait()
                    self.focus_session_inactive_event.clear()
                    logger.info("焦点已返回顶层，主意识被唤醒，继续环境感知。")
                except asyncio.CancelledError:
                    logger.info("主意识在等待焦点返回时被取消。")
                    break
                # continue 会让循环直接跳到下一次 `while` 检查，重新判断焦点位置
                continue

            # 1. 构建 Prompt (它内部自己会去拿最新的状态，我们不用管了)
            current_time_str = get_formatted_time_for_llm()
            system_prompt, user_prompt, response_schema = await self.prompt_builder.build_prompts(
                current_time_str,
                self.chat_session_manager.current_focus_path # 把当前焦点路径告诉PromptBuilder
            )
            # 【关键修复点 2】: 从 PlatformBuilder 单独获取 JSON Schema
            # 这个逻辑需要添加到 prompt_builder.build_prompts 之后，thought_generator.generate_thought 之前
            focus_path = self.chat_session_manager.current_focus_path
            if focus_path and focus_path != "core":
                path_parts = focus_path.split('.')
                current_platform_id = path_parts[0]
                current_level = "platform"
            else:
                current_platform_id = "core"
                current_level = "core"

            builder = platform_builder_registry.get_builder(current_platform_id)
            final_response_schema = {}
            if builder:
                controls_schema, _ = builder.get_level_consciousness_controls_definitions(current_level)
                actions_schema, _ = builder.get_level_actions_definitions(current_level)

                # 将两个 schema 合并
                final_response_schema = {
                    "type": "object",
                    "properties": {
                        "internal_state": { # internal_state 是固定的
                            "type": "object",
                            "properties": {
                                "mood": {"type": "string"},
                                "think": {"type": "string"},
                                "goal": {"type": "string"}
                            },
                            "required": ["mood", "think", "goal"]
                        },
                        "consciousness_control": controls_schema,
                        "action": actions_schema
                    },
                    "required": ["internal_state"]
                }
            else:
                logger.warning(f"未能为平台 '{current_platform_id}' 找到 builder，将使用空的 response_schema。")

            # 2. 生成思考
            logger.info(
                f"[{datetime.datetime.now().strftime('%H:%M:%S')}] "
                f"{config.persona.bot_name} 开始思考 (焦点: {self.chat_session_manager.current_focus_path or 'Core'})..."
            )
            generated_thought_json = await self.thought_generator.generate_thought(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                image_inputs=[],
                response_schema=final_response_schema, # 把动态生成的Schema传给LLM
            )


            if generated_thought_json:
                # 3. 把思考结果打包成“思想点”并存入数据库
                # 这部分逻辑保持不变，因为我们需要先记录再执行
                if new_state := generated_thought_json.get("internal_state"):
                    self.last_known_internal_state = new_state
                logger.info(
                    f"生成的思考内容: {generated_thought_json.get('internal_state', {}).get('think', '无内容')}"
                )
                action_payload = generated_thought_json.get("action")
                consciousness_control_payload = generated_thought_json.get("consciousness_control")

                # 只要有任何一种指令，就认为需要一个action_id来追踪
                action_id = str(uuid.uuid4()) if (action_payload or consciousness_control_payload) else None

                new_thought_pearl = ThoughtChainDocument(
                    _key=str(uuid.uuid4()),
                    timestamp=datetime.datetime.now(datetime.UTC).isoformat(),
                    mood=generated_thought_json.get("internal_state", {}).get("mood", "平静"),
                    think=generated_thought_json.get("internal_state", {}).get("think", "无"),
                    goal=generated_thought_json.get("internal_state", {}).get("goal"),
                    source_type="core",
                    source_id=None,
                    action_id=action_id,
                    action_payload=generated_thought_json,
                )

                saved_key = await self.thought_storage_service.save_thought_and_link(
                    new_thought_pearl
                )

                if saved_key:
                    # 4. 调用统一决策分发器
                    await process_llm_decision(
                        decision_json=generated_thought_json,
                        focus_manager=self.chat_session_manager,
                        action_handler=self.action_handler_instance,
                        source_thought_key=saved_key,
                        source_action_id=action_id
                    )
                else:
                    logger.error("严重逻辑错误：思想点未能成功串入思想链，无法分发决策！")

            # 6. 等待下一次闹钟
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(
                    self.immediate_thought_trigger.wait(), timeout=float(thinking_interval_sec)
                )
                self.immediate_thought_trigger.clear()
                logger.info("被动思考被触发，立即开始新一轮思考。")

            if self.stop_event.is_set():
                break
        logger.info(f"--- {config.persona.bot_name} 的意识流动已停止 ---")

    async def start_thinking_loop(self) -> asyncio.Task:
        """启动主思考循环，开始持续思考和处理动作.

        这个方法会创建一个新的异步任务来运行思考循环，并返回该任务对象.

        Returns:
            asyncio.Task: 启动的思考循环任务对象.
        """
        logger.info(f"=== {config.persona.bot_name} (意识流版) 的大脑准备开始持续思考 ===")
        self.thinking_loop_task = asyncio.create_task(self._core_thinking_loop())
        return self.thinking_loop_task

    async def stop(self) -> None:
        """停止主思考循环和意识流动."""
        logger.info(f"--- {config.persona.bot_name} 的意识流动正在停止 ---")
        self.stop_event.set()
        if self.thinking_loop_task and not self.thinking_loop_task.done():
            self.thinking_loop_task.cancel()
            try:
                await self.thinking_loop_task
            except asyncio.CancelledError:
                logger.info("主思考循环任务已被取消。")

    async def _activate_new_focus_session_from_core(self, target_conv_id: str) -> None:
        """从 CoreLogic 内部直接激活一个新的专注会话.

        这个方法是给 LLMResponseHandler 调用的，用于 LLM 决策直接转移专注.

        Args:
            target_conv_id (str): 目标会话的 ID，表示要激活的专注会话的唯一标识符.
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
            source_type="core",
            source_id=None,
            action_id=str(uuid.uuid4()),
            action_payload=mock_action_payload,
        )
        await self._dispatch_action(mock_thought_pearl)
