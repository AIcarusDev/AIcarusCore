# 文件路径: src/mind/consciousness_flow.py

import asyncio
import contextlib
import threading
import traceback
from typing import TYPE_CHECKING, Optional

from src.common.custom_logging.logging_config import get_logger
from src.common.interruption_broker import InterruptionEventBroker
from src.config import config
from src.domain.models import Stimulus
from src.mind.intrusive_thoughts import IntrusiveThoughtsGenerator
from src.mind.state_manager import AIStateManager
from src.mind.thought_generator import ThoughtGenerator
from src.mind.thought_persistor import ThoughtPersistor
from src.os.decision_dispatcher import process_aicos_decision
from src.prompting import PromptBuilderError, ThoughtPromptBuilder
from src.prompting.components import PromptComponents
from src.services.action.action_handler import ActionHandler
from src.services.database import ThoughtStorageService
from src.services.database.models import ThoughtChainDocument

if TYPE_CHECKING:
    from src.bootstrap.container import ServiceContainer
    from src.os.application_manager import ApplicationManager
    from src.os.apps.interfaces import ISession
    from src.os.state_generator import AICOSStateGenerator
    from src.os.window_manager import WindowManager
    from src.services.core_communication.core_ws_server import CoreWebsocketServer
    from src.services.database.services.entity_graph_service import EntityGraphService

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
        thought_generator: ThoughtGenerator,
        thought_persistor: ThoughtPersistor,
        prompt_builder: ThoughtPromptBuilder,
        stop_event: threading.Event,
        interruption_broker: "InterruptionEventBroker",
        immediate_thought_trigger: asyncio.Event,
        aicos_state_generator: "AICOSStateGenerator",
        thought_storage_service: ThoughtStorageService,
        entity_graph_service: "EntityGraphService",
        intrusive_generator_instance: IntrusiveThoughtsGenerator | None = None,
    ) -> None:
        self.window_manager = window_manager
        self.application_manager = application_manager
        self.core_comm_layer = core_comm_layer
        self.action_handler_instance = action_handler_instance
        self.state_manager = state_manager
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
        self.container: ServiceContainer | None = None
        logger.info(f"{self.__class__.__name__} 已创建 ")

    def trigger_immediate_thought_cycle(self) -> None:
        """立即触发思考循环，唤醒主意识."""
        logger.info("接收到立即思考触发信号，主意识将被唤醒。")
        self.immediate_thought_trigger.set()

    async def _get_current_session(self) -> Optional["ISession"]:
        """异步获取当前UI焦点所在的会话."""
        if self.prompt_builder:
            _, _, _, session = await self.prompt_builder._extract_context_from_ui()
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

        while not self.stop_event.is_set():
            try:
                await self._run_full_thought_cycle()
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
            prompt_components, session, ui_mapping = (
                await self.prompt_builder.build_prompts_components()
            )
        except PromptBuilderError as e:
            logger.error(f"构建Prompt失败，中止本轮思考循环: {e}")
            return

        try:
            # 将 session 传递下去
            new_thought_pearl, saved_key = await self._generate_and_persist_thought(
                prompt_components, session
            )
            if not new_thought_pearl or not new_thought_pearl.action_payload:
                logger.info("本轮思考未产生任何决策，进入下一周期。")
                return

        except ThoughtGenerationError as e:
            logger.error(f"核心思考过程失败，中止本轮循环: {e}")
            return

        if not self.container:
            logger.critical("ServiceContainer 未注入到 CoreLogic，无法执行决策分发！")
            return

        await process_aicos_decision(
            decision_json=new_thought_pearl.action_payload,
            ui_mapping=ui_mapping,
            container=self.container,
        )

    async def _generate_and_persist_thought(
        self, prompt_components: PromptComponents, session: Optional["ISession"]
    ) -> tuple[ThoughtChainDocument | None, str | None]:
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
            focus_path=None, # focus_path 暂时未使用，保持 None
        )
        if not generated_thought_json:
            raise ThoughtGenerationError("LLM未能生成有效的思考JSON。")

        sanitized_thought_json = generated_thought_json

        # [修改] 传递 session id
        source_id = session.conversation_id if session else None
        saved_key, new_thought_pearl = await self.thought_persistor.store_thought(
            thought_json=sanitized_thought_json,
            source_type="aicos_unified",
            source_id=source_id,
        )

        if not saved_key or not new_thought_pearl:
            raise ThoughtGenerationError("未能将新的思考持久化到数据库。")

        # 在这里检查是否需要处理慢思考的副作用
        # 注意：这里的 session 是 ISession 接口，我们只关心 working_memory
        if session and self.state_manager and session.working_memory:
            resolution = session.working_memory.get("deliberation_resolution")
            if resolution:
                self.state_manager.add_strategic_memo(resolution)
                session.working_memory.pop("deliberation_resolution", None) # 用完就删
                self.trigger_immediate_thought_cycle()

        return new_thought_pearl, saved_key

    async def _wait_for_next_cycle(self, interval: float) -> None:
        """等待下一个思考周期，可以被 immediate_thought_trigger 立即中断."""
        try:
            await asyncio.wait_for(self.immediate_thought_trigger.wait(), timeout=interval)
            logger.info("被动思考被触发，立即开始新一轮思考。")
        except TimeoutError:
            pass
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

    async def handle_deliberation_resolution(self, resolution: dict | None) -> None:
        """接收并处理来自慢思考服务的决议，将其存入全局状态管理器."""
        if not resolution:
            return

        logger.info("CoreLogic 正在将慢思考决议添加为全局战略备忘录...")

        # 将结果交给 AIStateManager 管理
        self.state_manager.add_strategic_memo(resolution)

        # 重要的结论应该立即影响下一轮思考
        self.trigger_immediate_thought_cycle()
