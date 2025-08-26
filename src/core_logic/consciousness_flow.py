# src/core_logic/consciousness_flow.py
import asyncio
import contextlib
import threading
import traceback
from typing import TYPE_CHECKING, Optional

from src.action.action_handler import ActionHandler
from src.common.custom_logging.logging_config import get_logger
from src.common.interruption_broker import InterruptionEventBroker
from src.config import config
from src.core_logic.decision_dispatcher import process_aicos_decision
from src.core_logic.intrusive_thoughts import IntrusiveThoughtsGenerator
from src.core_logic.sanitizer import LLMOutputSanitizer
from src.core_logic.state_manager import AIStateManager
from src.core_logic.thought_generator import ThoughtGenerator
from src.core_logic.thought_persistor import ThoughtPersistor
from src.database import ThoughtStorageService
from src.database.models import ThoughtChainDocument
from src.domain.models import Stimulus
from src.focus_chat_mode.components import PromptComponents
from src.prompt_builder import PromptBuilderError, ThoughtPromptBuilder

if TYPE_CHECKING:
    from src.aicos.application_manager import ApplicationManager
    from src.aicos.state_generator import AICOSStateGenerator
    from src.aicos.window_manager import WindowManager
    from src.core_communication.core_ws_server import CoreWebsocketServer
    from src.database.services.entity_graph_service import EntityGraphService
    from src.focus_chat_mode.chat_session import ChatSession
    from src.focus_chat_mode.chat_session_manager import ChatSessionManager

logger = get_logger(__name__)


class ThoughtGenerationError(Exception):
    """Custom exception for critical failures during thought generation or persistence."""

    pass


class CoreLogic:
    """核心逻辑处理类."""

    def __init__(
        self,
        window_manager: "WindowManager",
        application_manager: "ApplicationManager",
        core_comm_layer: "CoreWebsocketServer",
        action_handler_instance: ActionHandler,
        state_manager: AIStateManager,
        chat_session_manager: "ChatSessionManager",
        thought_storage_service: ThoughtStorageService,
        entity_graph_service: "EntityGraphService",
        thought_generator: ThoughtGenerator,
        thought_persistor: ThoughtPersistor,
        prompt_builder: ThoughtPromptBuilder,
        stop_event: threading.Event,
        interruption_broker: "InterruptionEventBroker",
        immediate_thought_trigger: asyncio.Event,
        aicos_state_generator: "AICOSStateGenerator",
        intrusive_generator_instance: IntrusiveThoughtsGenerator | None = None,
    ) -> None:
        self.window_manager = window_manager
        self.application_manager = application_manager
        self.core_comm_layer = core_comm_layer
        self.action_handler_instance = action_handler_instance
        self.state_manager = state_manager
        self.chat_session_manager = chat_session_manager
        self.thought_generator = thought_generator
        self.thought_persistor = thought_persistor
        self.thought_storage_service = thought_storage_service
        self.entity_graph_service = entity_graph_service
        self.aicos_state_generator = aicos_state_generator
        self.prompt_builder = prompt_builder
        self.stop_event = stop_event
        self.interruption_broker = interruption_broker
        self.immediate_thought_trigger = immediate_thought_trigger
        self.intrusive_generator_instance = intrusive_generator_instance
        self.thinking_loop_task: asyncio.Task | None = None
        self._last_interrupt_context_stimulus: Stimulus | None = None
        logger.info(f"{self.__class__.__name__} 已创建 (AIC-OS 适配版)")

    def trigger_immediate_thought_cycle(self) -> None:
        """立即触发思考循环，唤醒主意识."""
        logger.info("接收到立即思考触发信号，主意识将被唤醒。")
        self.immediate_thought_trigger.set()

    # _get_current_session 在 AIC-OS 模式下不再需要，由 prompt_builder._extract_context_from_ui 代替
    # 但为了兼容性暂时保留
    def _get_current_session(self) -> Optional["ChatSession"]:
        if self.prompt_builder:
            _, _, _, session = self.prompt_builder._extract_context_from_ui()
            return session
        return None

    async def _core_thinking_loop(self) -> None:
        """核心思考循环. 只负责维持循环和处理顶层异常."""
        is_continuous = config.core_logic_settings.enable_continuous_thinking
        active_interval = (
            config.core_logic_settings.continuous_thinking_interval_seconds
            if is_continuous
            else config.core_logic_settings.thinking_interval_seconds
        )
        mode_desc = (
            f"连续思考模式 (间隔: {active_interval}s)"
            if is_continuous
            else f"标准间隔模式 (间隔: {active_interval}s)"
        )

        logger.info(f"=== {config.persona.bot_name} 苏醒了 ({mode_desc}) ===")

        # 进入主循环，直到 stop_event 被设置
        while not self.stop_event.is_set():
            try:
                # 开始一轮思考循环
                await self._run_full_thought_cycle()
                # 等待下一个思考周期的开始
                await self._wait_for_next_cycle(active_interval)

            except asyncio.CancelledError:
                logger.info("意识流主循环被取消。")
                break
            except Exception as e:
                logger.error(f"意识流主循环发生严重错误: {e}")
                logger.exception("核心思考循环中发生未处理的异常。")
                traceback.print_exc()
                await asyncio.sleep(10)
        logger.info(f"--- {config.persona.bot_name} 的意识流已停止 ---")

    async def _run_full_thought_cycle(self) -> None:
        """执行完整的思考循环，主要负责编排."""
        try:
            # --- [修复 3] prompt_builder 现在返回 ui_mapping ---
            prompt_components, _, ui_mapping = await self.prompt_builder.build_prompts_components()
        except PromptBuilderError as e:
            logger.error(f"构建Prompt失败，中止本轮思考循环: {e}")
            return

        try:
            new_thought_pearl, saved_key = await self._generate_and_persist_thought(
                prompt_components
            )
        except ThoughtGenerationError as e:
            logger.error(f"核心思考过程失败，中止本轮循环: {e}")
            return

        # --- 调用新的 process_aicos_decision 并传入所有需要的服务和状态 ---
        await process_aicos_decision(
            decision_json=new_thought_pearl.action_payload,
            ui_mapping=ui_mapping,
            window_manager=self.window_manager,
            application_manager=self.application_manager,
            action_handler=self.action_handler_instance,
            chat_session_manager=self.chat_session_manager,
            state_manager=self.state_manager,
            aicos_state_generator=self.aicos_state_generator,
        )

    async def _generate_and_persist_thought(
        self, prompt_components: PromptComponents
    ) -> tuple[ThoughtChainDocument, str]:
        """生成思考，创建文档，并将其持久化."""
        system_prompt, user_prompt, response_schema = self.prompt_builder.finalize_prompts(
            prompt_components
        )
        self.prompt_builder.is_context_switch_flag = False

        generated_thought_json = await self.thought_generator.generate_thought(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            image_inputs=prompt_components.image_references,
            response_schema=response_schema,
            focus_path=None,  # 在 AIC-OS 模式下，focus_path 由 UI 状态隐式定义
        )
        if not generated_thought_json:
            raise ThoughtGenerationError("LLM未能生成有效的思考JSON。")

        # 1. 实例化修正器，传入本轮思考所需的所有上下文
        sanitizer = LLMOutputSanitizer(
            user_map=prompt_components.user_map,
            uid_str_to_platform_id_map=prompt_components.uid_str_to_platform_id_map,
        )

        # 2. 执行修正
        sanitized_thought_json = sanitizer.sanitize(generated_thought_json)

        # 3. 记录修正前后的对比，便于调试
        if generated_thought_json != sanitized_thought_json:
            logger.warning("LLM输出被拦截且修正")
            logger.debug(f"修正前: {generated_thought_json}")
            logger.debug(f"修正后: {sanitized_thought_json}")

        # 委托 ThoughtPersistor 来处理打包和存储
        saved_key, new_thought_pearl = await self.thought_persistor.store_thought(
            thought_json=sanitized_thought_json,
            source_type="aicos_unified",
            source_id=None,
        )

        if not saved_key or not new_thought_pearl:
            raise ThoughtGenerationError("未能将新的思考持久化到数据库。")

        return new_thought_pearl, saved_key

    async def _wait_for_next_cycle(self, interval: float) -> None:
        """等待下一个思考周期，可以被 immediate_thought_trigger 立即中断."""
        try:
            await asyncio.wait_for(self.immediate_thought_trigger.wait(), timeout=interval)
            logger.info("被动思考被触发，立即开始新一轮思考。")
        except TimeoutError:
            pass  # 正常超时，无需记录
        finally:
            self.immediate_thought_trigger.clear()

    async def start_thinking_loop(self) -> asyncio.Task:
        """启动核心逻辑的思考循环."""
        logger.info(f"=== {config.persona.bot_name} 的大脑准备开始持续思考 ===")
        self.thinking_loop_task = asyncio.create_task(self._core_thinking_loop())
        return self.thinking_loop_task

    async def stop(self) -> None:
        """停止核心逻辑的思考循环."""
        logger.info(f"--- {config.persona.bot_name} 的意识流动正在停止 ---")
        self.stop_event.set()
        if self.thinking_loop_task and not self.thinking_loop_task.done():
            self.thinking_loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.thinking_loop_task
            logger.info("主思考循环任务已被取消。")
