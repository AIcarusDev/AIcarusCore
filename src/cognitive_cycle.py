# src/cognitive_cycle.py
import asyncio
import contextlib
import traceback
import uuid
from typing import TYPE_CHECKING

from src.common.custom_logging.logging_config import get_logger
from src.config import config
from src.domain.models import ActionMetadata
from src.mind.consciousness_flow import CoreLogic
from src.os.ui_dispatcher import handle_os_interaction
from src.prompting.error import PromptBuilderError

if TYPE_CHECKING:
    from src.bootstrap.container import ServiceContainer

logger = get_logger(__name__)


class CognitiveCycle:
    """新的顶层协调者，负责驱动完整的“感知-思考-行动”循环.

    它位于 Mind 和 OS 之上，扮演着连接两者的媒介。
    这是机器人没有虚拟身体和物理形态的情况下的替代方案。
    """

    def __init__(self, container: "ServiceContainer") -> None:
        self.container = container
        self.stop_event = asyncio.Event()
        self.immediate_thought_trigger = asyncio.Event()
        self._last_external_info_snapshot: str | None = None
        self._main_loop_task: asyncio.Task | None = None

    def trigger_immediate_thought_cycle(self) -> None:
        """从外部触发一次即时思考（例如，在慢思考完成后）."""
        logger.info("接收到立即认知周期触发信号，意识将被唤醒。")
        self.immediate_thought_trigger.set()

    async def start(self) -> None:
        """启动认知循环."""
        if self._main_loop_task and not self._main_loop_task.done():
            logger.warning("认知循环已在运行。")
            return

        is_continuous = config.core_logic_settings.enable_continuous_thinking
        active_interval = (
            config.core_logic_settings.continuous_thinking_interval_seconds
            if is_continuous
            else config.core_logic_settings.thinking_interval_seconds
        )
        mode_desc = (
            f"连续认知周期模式 (间隔: {active_interval}s)"
            if is_continuous
            else f"标准认知周期模式 (间隔: {active_interval}s)"
        )
        logger.info(f"=== {config.persona.bot_name} 认知周期开始 ({mode_desc}) ===")

        self._main_loop_task = asyncio.create_task(self._run_loop(active_interval))
        await self._main_loop_task

    async def stop(self) -> None:
        """停止认知循环."""
        if not self.stop_event.is_set():
            logger.info(f"--- {config.persona.bot_name} 的认知周期正在停止 ---")
            self.stop_event.set()
            self.immediate_thought_trigger.set() # 确保循环可以立即退出等待
            if self._main_loop_task:
                with contextlib.suppress(asyncio.CancelledError):
                    await self._main_loop_task
                self._main_loop_task = None
            logger.info("认知周期任务已处理完毕。")


    async def _run_loop(self, interval: float) -> None:
        """核心循环的实现."""
        while not self.stop_event.is_set():
            try:
                await self._run_full_thought_cycle()
                await self._wait_for_next_cycle(interval)
            except asyncio.CancelledError:
                logger.info("认知周期循环被取消。")
                break
            except Exception:
                logger.error("认知周期循环发生严重错误，10秒后重试。", exc_info=True)
                traceback.print_exc()
                await asyncio.sleep(10)
        logger.info(f"--- {config.persona.bot_name} 的认知周期已停止 ---")


    async def _wait_for_next_cycle(self, interval: float) -> None:
        """等待下一个思考周期，可以被立即中断."""
        if self.stop_event.is_set():
            return
        try:
            await asyncio.wait_for(self.immediate_thought_trigger.wait(), timeout=interval)
            logger.info("被动思考被触发，立即开始新一轮思考。")
        except TimeoutError:
            pass # 正常超时
        finally:
            self.immediate_thought_trigger.clear()


    async def _run_full_thought_cycle(self) -> None:
        """执行一次完整的“感知-思考-行动”周期."""
        # 1. 感知 (Perception) - 从 OS 获取世界状态
        try:
            prompt_builder = self.container.prompt_builder
            (
                prompt_components,
                _, # session is handled internally by prompt_builder now
                ui_mapping,
                current_external_info_snapshot,
            ) = await prompt_builder.build_prompts_components(
                last_external_info_snapshot=self._last_external_info_snapshot
            )
            self._last_external_info_snapshot = current_external_info_snapshot
        except PromptBuilderError as e:
            logger.error(f"构建Prompt失败，中止本轮认知周期: {e}")
            return

        # 2. 思考 (Cognition) - 请求 Mind 模块生成决策
        # 注意：我们现在调用的是一个纯净的 CoreLogic
        mind: CoreLogic = self.container.core_logic
        thought_result = await mind.run_one_thought_cycle(prompt_components)

        if not thought_result:
            logger.info("本轮认知周期未产生有效决策，进入下一周期。")
            return

        new_thought_pearl, saved_key = thought_result
        if not new_thought_pearl.action_payload or not saved_key:
            logger.info("本轮认知周期决策为空或未能持久化，进入下一周期。")
            return

        # 3. 行动 (Action) - 解析决策并分发到不同执行器
        await self._orchestrate_action(
            decision_json=new_thought_pearl.action_payload,
            ui_mapping=ui_mapping,
            thought_key=saved_key,
            external_info_snapshot=current_external_info_snapshot,
        )

    async def _orchestrate_action(
        self,
        decision_json: dict | None,
        ui_mapping: dict,
        thought_key: str,
        external_info_snapshot: str | None,
    ) -> None:
        """动作总编排器.

        负责解析LLM的决策并分发到正确的处理器。
        """
        if not decision_json or not isinstance(decision_json, dict):
            return

        logger.info(f"动作编排器开始处理决策 (源自 Thought: {thought_key}): {decision_json}")

        if action_payload := decision_json.get("action"):
            # 路由内部动作 (直接调用Mind层服务)
            if internal_action := action_payload.get("internal"):
                await self._route_internal_action(
                    internal_action, thought_key, external_info_snapshot
                )

            # 路由外部动作 (调用OS或ActionHandler)
            if external_action := action_payload.get("external"):
                await self._route_external_action(external_action, ui_mapping)

    async def _route_internal_action(
        self, internal_action: dict, thought_key: str, external_info_snapshot: str | None
    ) -> None:
        """将内部动作路由到对应的 Mind 层服务执行."""
        action_name = next(iter(internal_action), None)
        if not action_name:
            return
        params = internal_action[action_name]
        logger.info(f"编排内部动作: '{action_name}'")

        if action_name == "manage_goals":
            await self.container.goal_manager.add_goals(
                params.get("add", {}).get("goals", [])
            )
            await self.container.goal_manager.remove_goals(
                params.get("remove", {}).get("goal_ids", [])
            )
        elif action_name == "deep_think":
            resolution = await self.container.deliberation_service.execute(
                pipeline_params=params,
                container=self.container,
                external_info_snapshot=external_info_snapshot,
            )
            if resolution:
                # This part is a bit tricky, the result needs to be formatted for memory
                # We can borrow the formatting logic
                from xml.etree.ElementTree import Element, SubElement, tostring
                root = Element("deliberation_result")
                SubElement(root, "summary").text = resolution.get("summary", "无总结。")
                final_state = SubElement(root, "final_internal_state")
                SubElement(final_state, "mood").text = resolution.get("final_mood", "平静")
                SubElement(final_state, "think").text = resolution.get("final_think", "...")
                SubElement(final_state, "intent").text = resolution.get("final_intent", "无")
                result_str = tostring(root, encoding='unicode')

                await self.container.thought_storage_service.save_action_result_to_thought(
                    thought_key=thought_key, result_text=result_str
                )
                self.trigger_immediate_thought_cycle()
        else:
            logger.warning(f"接收到未知的内部动作: {action_name}")

    async def _route_external_action(self, external_action: dict, ui_mapping: dict) -> None:
        """将外部动作路由到对应的 Service 或 OS 层处理器."""
        # 分发 "innate" 固有能力 (文件、搜索等) 到 ActionHandler
        if innate_action := external_action.get("innate"):
            await self.container.action_handler.process_action_flow(
                action_id=f"action_{uuid.uuid4().hex[:6]}",
                doc_key_for_updates=f"thought_for_innate_{uuid.uuid4().hex[:6]}",
                action_json={"core": innate_action},
                metadata=ActionMetadata(motivation="由 AI 核心决策发起"),
            )
        # 分发所有 AIC-OS 的 UI 交互到 UI Dispatcher
        elif aicos_interaction := external_action.get("AIC-OS"):
            await handle_os_interaction(aicos_interaction, ui_mapping, self.container)
