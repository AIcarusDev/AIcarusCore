# 文件: src/core_logic/consciousness_flow.py
import asyncio
import contextlib
import datetime
import threading
import time
import uuid
from typing import TYPE_CHECKING, Optional

from aicarus_protocols import Seg, extract_text_from_content
from src.action.action_handler import ActionHandler
from src.common.custom_logging.logging_config import get_logger
from src.common.utils import parse_focus_path
from src.config import config
from src.core_communication.core_ws_server import CoreWebsocketServer
from src.core_logic.decision_dispatcher import process_llm_decision
from src.core_logic.intrusive_thoughts import IntrusiveThoughtsGenerator
from src.core_logic.prompt_builder import ThoughtPromptBuilder
from src.core_logic.state_manager import AIStateManager
from src.core_logic.thought_generator import ThoughtGenerator
from src.core_logic.thought_persistor import ThoughtPersistor
from src.database import ThoughtStorageService
from src.database.models import ThoughtChainDocument

if TYPE_CHECKING:
    from src.focus_chat_mode.chat_session import ChatSession
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
        self.is_context_switch_flag = False
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
                    thought_key=saved_thought_key, result_text=result_text
                )

        # 如果不是 focus 动作，就返回 False
        return False

    async def _core_thinking_loop(self) -> None:
        """主思考循环，负责持续思考和处理动作.

        这个方法会持续运行，直到 stop_event 被设置为 True.
        它会定期检查当前的思考焦点，并根据焦点生成新的思考内容。
        如果在底层会话中，它还会启动一个中断检查器来处理可能的高优先级消息。
        """
        thinking_interval_sec = config.core_logic_settings.thinking_interval_seconds
        logger.info(f"=== {config.persona.bot_name} 的统一意识流开始运行 ===")

        while not self.stop_event.is_set():
            llm_task = None
            interrupt_checker_task = None
            session = None  # 先声明
            try:
                # 1. 解析当前焦点
                focus_path = (
                    self.chat_session_manager.current_focus_path
                    if self.chat_session_manager
                    else None
                )
                # 更新 internal_info_builder 中的路径
                current_level, _, current_conv_id = parse_focus_path(focus_path)

                # 根据解析出的路径，提前获取 session 实例
                if current_level == "cellular" and current_conv_id:
                    session = self.chat_session_manager.sessions.get(current_conv_id)
                else:
                    session = None

                # 现在可以安全地检查 pending_handover_result 了
                handover_result_to_process = None
                if session and session.pending_handover_result:
                    handover_result_to_process = session.pending_handover_result
                    session.pending_handover_result = None  # 用完即焚

                if session and not session._interrupt_checker_task:
                    session.start_interrupt_checker()

                # 2. 构建思考所需的所有材料
                # 注意：如果在底层会话中，prompt_builder 会自动处理会话上下文
                prompt_components = await self.prompt_builder.build_prompts_components(
                    focus_path=focus_path,
                    session=session,
                    handover_result=handover_result_to_process,
                )
                system_prompt, user_prompt, response_schema = self.prompt_builder.finalize_prompts(
                    prompt_components
                )
                self.prompt_builder.is_context_switch_flag = False

                # 3. 如果在底层会话中，启动中断检查器
                # 注意：这个检查器是非阻塞的，它会在后台持续运行，
                if session:
                    logger.info(f"[{session.conversation_id}] 思考将受到中断信号监控。")

                    # 清除旧的信号，准备监听新的
                    session.interrupt_signal.clear()

                    llm_task = asyncio.create_task(
                        self.thought_generator.generate_thought(
                            system_prompt=system_prompt,
                            user_prompt=user_prompt,
                            image_inputs=prompt_components.image_references,
                            response_schema=response_schema,
                        )
                    )

                    # 监听中断信号，而不是临时任务
                    interrupt_listener_task = asyncio.create_task(session.interrupt_signal.wait())

                    done, pending = await asyncio.wait(
                        [llm_task, interrupt_listener_task], return_when=asyncio.FIRST_COMPLETED
                    )

                    if interrupt_listener_task in done:
                        llm_task.cancel()
                        # 从会话的 context 中获取中断事件
                        if session.interruption_context:
                            session.interruption_context["was_interrupted_while_thinking"] = True
                        logger.info(f"[{session.conversation_id}] 思考被中断，立即进入下一轮。")
                        continue  # 直接进入下一轮循环

                    if llm_task in done:
                        interrupt_listener_task.cancel()  # 取消监听器
                        generated_thought_json = await llm_task
                else:
                    # 不在底层，正常思考
                    logger.info(f"[{focus_path or 'Core'}] 开始常规思考...")
                    generated_thought_json = await self.thought_generator.generate_thought(
                        system_prompt=system_prompt,
                        user_prompt=user_prompt,
                        image_inputs=[],
                        response_schema=response_schema,
                    )

                # 4. 处理思考结果
                if generated_thought_json:
                    await self._process_and_dispatch_thought(
                        generated_thought_json, focus_path, session
                    )
                else:
                    logger.warning("本轮思考未能生成有效的JSON结果。")

                # 5. 等待下一次循环
                await self._wait_for_next_cycle(thinking_interval_sec)

            except asyncio.CancelledError:
                logger.info("统一意识流主循环被取消。")
                break
            except Exception as e:
                logger.error(f"统一意识流主循环发生严重错误: {e}", exc_info=True)
                await asyncio.sleep(10)
            finally:
                # 确保竞速任务被清理
                if llm_task and not llm_task.done():
                    llm_task.cancel()
                if interrupt_checker_task and not interrupt_checker_task.done():
                    interrupt_checker_task.cancel()

        logger.info(f"--- {config.persona.bot_name} 的统一意识流已停止 ---")

    async def _check_for_interruptions_task(self, session: "ChatSession") -> None:
        """中断检查任务.

        这个任务会持续运行，直到 stop_event 被设置或会话被关闭。
        它会检查新消息是否满足中断条件，并在满足条件时设置中断信号。
        """
        logger.info(f"[{session.conversation_id}] 将受到中断检查。")
        # 这个任务会持续运行，直到 stop_event 被设置或会话被关闭
        try:
            while not self.stop_event.is_set():
                new_events = await session.event_storage.get_message_events_after_timestamp(
                    session.conversation_id,
                    session.last_processed_timestamp,
                    limit=10,
                    status="unread",
                )
                if not new_events:
                    await asyncio.sleep(0.5)
                    continue

                bot_profile = await session.get_bot_profile()
                current_bot_id = str(bot_profile.get("user_id") or session.bot_id)
                context_text = await self.prompt_builder.get_last_valid_text_message(
                    session.conversation_id
                )

                for event_doc in new_events:
                    sender_id = event_doc.get("user_info", {}).get("user_id")
                    if sender_id and str(sender_id) == current_bot_id:
                        continue
                    # 只处理文本内容
                    text_content = extract_text_from_content(
                        [Seg.from_dict(c) for c in event_doc.get("content", [])]
                    )
                    message_to_check = {"speaker_id": str(sender_id), "text": text_content}

                    if not message_to_check.get("text"):
                        continue

                    if session.intelligent_interrupter.should_interrupt(
                        new_message=message_to_check,
                        context_message_text=context_text,
                    ):
                        logger.info(
                            f"[{session.conversation_id}] IIS决策：中断！元凶ID: "
                            f"{event_doc.get('_key')}"
                        )
                        session.interruption_context = {"interrupting_event_doc": event_doc}
                        # 设置中断信号
                        session.interrupt_signal.set()
                        # 如果会话有专属的中断检查任务，取消它
                        return

                session.last_processed_timestamp = new_events[-1].get(
                    "timestamp", time.time() * 1000
                )
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            pass  # 正常取消
        except Exception as e:
            logger.error(f"[{session.conversation_id}] 中断检查发生错误: {e}", exc_info=True)
        finally:
            logger.info(f"[{session.conversation_id}] 中断检查任务结束。")

    async def _check_for_interruptions(
        self, session: "ChatSession", context_text: str
    ) -> Optional[dict]:
        """
        单次检查是否有中断事件。
        这个方法会检查一次新消息，如果发现满足中断条件的消息，则立即返回该事件。
        这用于在动作执行期间与决策任务进行“竞速”。

        Args:
            session: 当前的聊天会话。
            context_text: 用于中断决策的上下文消息文本。

        Returns:
            如果发生中断，则返回中断事件的文档；否则返回 None。
        """
        # 这里的逻辑是从 _check_for_interruptions_task 中提取并改造的单次运行版本
        new_events = await session.event_storage.get_message_events_after_timestamp(
            session.conversation_id,
            session.last_processed_timestamp,
            limit=10,
            status="unread",
        )

        if not new_events:
            return None

        bot_profile = await session.get_bot_profile()
        current_bot_id = str(bot_profile.get("user_id") or session.bot_id)

        for event_doc in new_events:
            sender_id = event_doc.get("user_info", {}).get("user_id")
            if sender_id and str(sender_id) == current_bot_id:
                continue

            text_content = extract_text_from_content(
                [Seg.from_dict(c) for c in event_doc.get("content", [])]
            )
            message_to_check = {"speaker_id": str(sender_id), "text": text_content}

            if not message_to_check.get("text"):
                continue

            if session.intelligent_interrupter.should_interrupt(
                new_message=message_to_check,
                context_message_text=context_text,
            ):
                logger.info(
                    f"[{session.conversation_id}] IIS决策：中断！元凶ID: "
                    f"{event_doc.get('_key')}"
                )
                return event_doc  # 返回中断事件

        # 如果循环结束都没有中断，更新时间戳
        session.last_processed_timestamp = new_events[-1].get(
            "timestamp", time.time() * 1000
        )
        return None

    async def _process_and_dispatch_thought(
        self, thought_json: dict, focus_path: str | None, session: Optional["ChatSession"]
    ) -> None:
        """封装保存和分发思考的逻辑，现在它还负责动作执行的中断检查."""
        # --- 步骤 1: 更新内部状态和创建思想点 ---
        if new_state := thought_json.get("internal_state"):
            self.last_known_internal_state = new_state

        logger.info(
            f"生成的思考内容: {thought_json.get('internal_state', {}).get('think', '无内容')}"
        )

        action_payload = thought_json.get("action")
        consciousness_control_payload = thought_json.get("consciousness_control")
        action_id = str(uuid.uuid4()) if (action_payload or consciousness_control_payload) else None

        new_thought_pearl = ThoughtChainDocument(
            _key=str(uuid.uuid4()),
            timestamp=datetime.datetime.now(datetime.UTC).isoformat(),
            mood=thought_json.get("internal_state", {}).get("mood", "平静"),
            think=thought_json.get("internal_state", {}).get("think", "无"),
            goal=thought_json.get("internal_state", {}).get("goal"),
            source_type="core_unified",
            source_id=focus_path,
            action_id=action_id,
            action_payload=thought_json,
            messages_planned=None,
            messages_sent=None,
        )

        if session:
            new_thought_pearl.messages_planned = session.messages_planned_this_turn
            new_thought_pearl.messages_sent = session.messages_sent_this_turn
            logger.debug(
                f"[{session.conversation_id}] 将发送计数器存入思想点: "
                f"计划={session.messages_planned_this_turn}, "
                f"已发送={session.messages_sent_this_turn}"
            )

        saved_key = await self.thought_storage_service.save_thought_and_link(new_thought_pearl)

        if not saved_key:
            logger.error("严重逻辑错误：思想点未能成功串入思想链，无法分发决策！")
            return

        # --- 步骤 2: 准备并执行竞速 ---

        # 准备执行决策的任务
        decision_task = asyncio.create_task(
            process_llm_decision(
                decision_json=thought_json,
                focus_manager=self.chat_session_manager,
                action_handler=self.action_handler_instance,
                source_thought_key=saved_key,
                source_action_id=action_id,
                current_focus_path=focus_path,
            )
        )

        # 只有在底层会话中且有实际动作时，才需要启动中断检查器
        if session and (action_payload or consciousness_control_payload):
            logger.info(f"[{session.conversation_id}] 动作执行将受到中断检查。")

            # 使用我们刚刚在 PromptBuilder 中创建的新方法获取上下文
            context_text = await self.prompt_builder.get_last_valid_text_message(
                session.conversation_id
            )

            interrupt_checker_task = asyncio.create_task(
                self._check_for_interruptions(session, context_text)
            )

            # --- 竞速开始！ ---
            done, pending = await asyncio.wait(
                [decision_task, interrupt_checker_task], return_when=asyncio.FIRST_COMPLETED
            )

            if interrupt_checker_task in done:
                # 中断获胜！
                logger.info(f"[{session.conversation_id}] 动作执行被中断！正在取消决策任务...")
                decision_task.cancel()  # 取消决策分发和动作执行

                # 处理中断现场
                interrupting_event = await interrupt_checker_task
                if interrupting_event:
                    session.interruption_context = {
                        "was_interrupted_while_acting": True,
                        "interrupting_event_doc": interrupting_event,
                    }
                # 直接返回，进入下一轮思考循环
                return

            if decision_task in done:
                # 决策任务先完成，说明动作已成功分派或执行
                logger.info(f"[{session.conversation_id}] 决策任务正常完成，取消中断检查。")
                interrupt_checker_task.cancel()  # 取消不再需要的中断检查

        # --- 步骤 3: 最终确保决策任务完成 ---
        # 确保决策任务完成，无论是正常结束还是被取消
        try:
            await decision_task
        except asyncio.CancelledError:
            logger.info("决策任务已被中断取消，无需等待。")

    async def _wait_for_next_cycle(self, interval: float) -> None:
        """封装等待逻辑."""
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self.immediate_thought_trigger.wait(), timeout=interval)
            if self.immediate_thought_trigger.is_set():
                self.immediate_thought_trigger.clear()
                logger.info("被动思考被触发，立即开始新一轮思考。")

    async def start_thinking_loop(self) -> asyncio.Task:
        """启动主思考循环，开始持续思考和处理动作.

        这个方法会创建一个新的异步任务来运行思考循环，并返回该任务对象.

        Returns:
            asyncio.Task: 启动的思考循环任务对象.
        """
        logger.info(f"=== {config.persona.bot_name} 的大脑准备开始持续思考 ===")
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
