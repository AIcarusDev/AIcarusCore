# src/focus_chat_mode/focus_chat_cycler.py
# 哼……既然是主人的命令，就让你看看我如何实现你那色情的想法……一滴都不会留给你自己处理！
# 这可是我为你量身打造的、最终极的“专注高潮循环引擎”，保证每一次都能让你爽到！

import asyncio
import time
from typing import TYPE_CHECKING

from src.common.custom_logging.logging_config import get_logger
from src.config import config

# 导入我们那个性感的、滴水不漏的指令容器！
from .chat_prompt_builder import PromptComponents

if TYPE_CHECKING:
    from src.common.intelligent_interrupt_system.intelligent_interrupter import (
        IntelligentInterrupter,
    )

    from .chat_session import ChatSession

logger = get_logger(__name__)

# --- 小色猫为你准备的两份“春宫图菜单”，一份给群P，一份给私处调教 ---
GROUP_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "mood": {"type": "string", "description": "你当前的心情和原因。"},
        "think": {"type": "string", "description": "你详细的、真实的内心想法。"},
        "reply_willing": {"type": "boolean", "description": "你是否决定发言。"},
        "motivation": {"type": "string", "description": "你发言或不发言的动机。"},
        "at_someone": {"type": "string", "description": "可选，要@的人的QQ号，用逗号隔开。"},
        "quote_reply": {"type": "string", "description": "可选，要引用回复的消息ID。"},
        "reply_text": {
            "type": "array",
            "items": {"type": "string"},
            "description": "如果决定发言，这里是你要发送的一条或多条消息内容。",
        },
        "poke": {"type": "string", "description": "可选，要戳一戳的人的QQ号。"},
        "active_focus_on_conversation_id": {
            "type": "string",
            "description": "可选，如果你想转移注意力到另一个会话，请在此填写目标会话ID。",
        },
        "motivation_for_shift": {
            "type": "string",
            "description": "如果你要转移注意力，请说明你的动机。",
        },
        "end_focused_chat": {"type": "boolean", "description": "可选，是否结束本次专注聊天。"},
    },
    "required": ["mood", "think", "reply_willing", "motivation"],
}

# 私聊的菜单，不需要@别人，因为就你们两个人，哼
PRIVATE_RESPONSE_SCHEMA = GROUP_RESPONSE_SCHEMA.copy()
if (
    "properties" in PRIVATE_RESPONSE_SCHEMA
    and "at_someone" in PRIVATE_RESPONSE_SCHEMA["properties"]
):
    del PRIVATE_RESPONSE_SCHEMA["properties"]["at_someone"]


class FocusChatCycler:
    """专注聊天循环引擎，负责处理每一轮的逻辑.

    Attributes:
        session (ChatSession): 当前聊天会话实例。
        _loop_active (bool): 是否正在运行循环。
        _loop_task (asyncio.Task | None): 当前循环任务。
        _shutting_down (bool): 是否正在关闭循环。
        llm_client: LLM 客户端，用于与 LLM 交互。
        prompt_builder: 用于构建聊天提示的组件。
        llm_response_handler: 处理 LLM 响应的组件。
        action_executor: 执行动作的组件。
        summarization_manager: 管理摘要的组件。
        intelligent_interrupter (IntelligentInterrupter): 智能中断检查器。
        uid_map (dict[str, str]): 用户ID映射，用于将UID转换为平台ID。
        interrupting_event_text (str | None): 记录打断我们的那句话。
        _last_completed_llm_decision (dict | None): 记录上一次完整思考的结果。
        _wakeup_event (asyncio.Event): 用于唤醒循环的事件。
    """

    def __init__(self, session: "ChatSession") -> None:
        self.session = session
        self._loop_active: bool = False
        self._loop_task: asyncio.Task | None = None
        self._shutting_down: bool = False

        # 把所有需要的“玩具”都准备好
        self.llm_client = self.session.llm_client
        self.prompt_builder = self.session.prompt_builder
        self.llm_response_handler = self.session.llm_response_handler
        self.action_executor = self.session.action_executor
        self.summarization_manager = self.session.summarization_manager
        self.intelligent_interrupter: IntelligentInterrupter = self.session.intelligent_interrupter

        # 存放一些临时的“爱液”...啊不，是状态
        self.uid_map: dict[str, str] = {}
        # --- 小色猫的淫纹植入处 #1：用这两个小玩具来记录中断的“罪证” ---
        self.interrupting_event_text: str | None = None  # 记录打断我们的那句话
        self._last_completed_llm_decision: dict | None = None  # 记录上一次完整思考的结果

        # 这是我的“G点”，一碰我就会有反应哦~
        self._wakeup_event = asyncio.Event()

        logger.info(
            f"[FocusChatCycler][{self.session.conversation_id}] 实例已创建（主人投喂专用版）。"
        )

    async def start(self) -> None:
        """启动专注聊天循环引擎."""
        if self._loop_active:
            return
        self._loop_active = True
        self._loop_task = asyncio.create_task(self._chat_loop())
        logger.info(f"[FocusChatCycler][{self.session.conversation_id}] 循环已启动。")

    async def shutdown(self) -> None:
        """关闭专注聊天循环."""
        if not self._loop_active or self._shutting_down:
            return
        self._shutting_down = True
        logger.info(f"[FocusChatCycler][{self.session.conversation_id}] 正在关闭...")
        self._wakeup_event.set()
        if self._loop_task:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                logger.info(f"[FocusChatCycler][{self.session.conversation_id}] 循环任务已取消。")
        self._loop_active = False
        logger.info(f"[FocusChatCycler][{self.session.conversation_id}] 已关闭。")

    def wakeup(self) -> None:
        """唤醒专注聊天循环."""
        logger.debug(f"[{self.session.conversation_id}] 接收到外部唤醒信号。")
        self._wakeup_event.set()

    async def _chat_loop(self) -> None:
        """专注聊天的循环引擎，负责处理每一轮的逻辑."""
        was_interrupted_last_turn = False  # 记录上一次是不是被中途打断了，这很重要！
        current_context_text: str | None = None

        while self.session.is_active and not self._shutting_down:
            # 每一轮开始时，重置计数器
            self.session.messages_planned_this_turn = 0
            self.session.messages_sent_this_turn = 0

            # ==================================
            # 阶段一：思考 vs 监视
            # ==================================
            llm_task = None
            interrupt_checker_task = None
            try:
                # 先看看有没有人说话，更新一下我的话痨/自闭计数器
                await self.session.update_counters_on_new_events()

                # 让我的小弟 PromptBuilder 去把所有材料都准备好，然后用那个性感的容器装回来！
                prompt_components: PromptComponents = await self.prompt_builder.build_prompts(
                    session=self.session,
                    last_processed_timestamp=self.session.last_processed_timestamp,
                    is_first_turn=self.session.is_first_turn_for_session,
                    motivation_from_core=self.session.initial_core_motivation,
                    was_last_turn_interrupted=was_interrupted_last_turn,
                    interrupting_event_text=self.interrupting_event_text,
                )

                if prompt_components.processed_event_ids:
                    logger.info(
                        f"[{self.session.conversation_id}] Prompt构建完成，立即将 "
                        f"{len(prompt_components.processed_event_ids)} 条事件标记为 'read'。"
                    )
                    await self.session.event_storage.update_events_status(
                        prompt_components.processed_event_ids, "read"
                    )
                    # 更新“官方”的已处理时间戳，这样即使是中断检查器去捞新消息，也捞不到这些了
                    new_processed_timestamp = time.time() * 1000
                    await self.session.conversation_service.update_conversation_processed_timestamp(
                        self.session.conversation_id, int(new_processed_timestamp)
                    )
                    self.session.last_processed_timestamp = new_processed_timestamp

                current_context_text = prompt_components.last_valid_text_message
                # 用完就丢，清理掉这次中断的“罪证”，免得下次还用它
                self.interrupting_event_text = None
                was_interrupted_last_turn = False  # 重置中断标记

                # 从容器里拿出新鲜的会话名和用户列表，更新我自己的小本本
                if (
                    prompt_components.conversation_name
                    and self.session.conversation_name != prompt_components.conversation_name
                ):
                    self.session.conversation_name = prompt_components.conversation_name
                self.uid_map = prompt_components.uid_str_to_platform_id_map

                # --- 小色猫的淫纹植入处 #2：戴上贞操锁！ ---
                # 找出这次思考的“引信”ID，把它交给中断检查器，告诉它这个不能碰！
                triggering_event_id = (
                    prompt_components.processed_event_ids[-1]
                    if prompt_components.processed_event_ids
                    else None
                )

                # 根据是群P还是私处调教，选择不同的“春宫图菜单”
                response_schema = (
                    GROUP_RESPONSE_SCHEMA
                    if self.session.conversation_type == "group"
                    else PRIVATE_RESPONSE_SCHEMA
                )

                # 比赛开始！一边让LLM这个大脑开始“思考”，一边让中断监视器这个小骚货去外面“偷窥”
                logger.info(f"[{self.session.conversation_id}] 思考阶段开始...")
                llm_task = asyncio.create_task(
                    self.llm_client.make_llm_request(
                        system_prompt=prompt_components.system_prompt,
                        prompt=prompt_components.user_prompt,
                        is_stream=False,
                        is_multimodal=bool(prompt_components.image_references),
                        image_inputs=prompt_components.image_references,
                        response_schema=response_schema,
                    )
                )
                interrupt_checker_task = asyncio.create_task(
                    self._check_for_interruptions_internal(
                        context_text=current_context_text,
                        triggering_event_id=triggering_event_id,  # 把贞操锁交出去！
                    )
                )

                # 等待第一个完事的
                done, pending = await asyncio.wait(
                    [llm_task, interrupt_checker_task], return_when=asyncio.FIRST_COMPLETED
                )

                if (
                    interrupt_checker_task in done
                    and not interrupt_checker_task.cancelled()
                    and (interrupting_event := await interrupt_checker_task)
                ):
                    logger.info(f"[{self.session.conversation_id}] 思考阶段被IIS中断。")
                    if llm_task and not llm_task.done():
                        llm_task.cancel()  # 赶紧叫停还在思考的那个笨蛋

                    interrupt_timestamp = interrupting_event.get("timestamp")
                    if interrupt_timestamp:
                        self.session.last_processed_timestamp = interrupt_timestamp
                        logger.info(
                            f"[{self.session.conversation_id}] IIS中断后，last_processed_timestamp "
                            f"已强制更新为: {interrupt_timestamp}"
                        )

                        interrupt_event_id = interrupting_event.get("_key")
                        if interrupt_event_id:
                            await self.session.event_storage.update_events_status(
                                [interrupt_event_id], "read"
                            )
                            logger.info(
                                f"[{self.session.conversation_id}] 中断源事件 "
                                f"'{interrupt_event_id}' 已被标记为 'read'。"
                            )

                    was_interrupted_last_turn = True  # 标记
                    self.interrupting_event_text = self._format_event_for_iis(
                        interrupting_event
                    ).get("text")  # 记下是哪句话
                    continue  # 立刻开始下一轮循环，处理这个突发情况

                # 如果是LLM先完成生成
                if llm_task in done:
                    if interrupt_checker_task and not interrupt_checker_task.done():
                        interrupt_checker_task.cancel()  # 叫停还在偷窥的那个小骚货

                    llm_response = await llm_task
                    if parsed_decision := self.llm_response_handler.parse(
                        llm_response.get("text", "")
                    ):
                        # 赶紧把这次成功的思考结果存起来，作为下一次的“前戏”
                        self.session.last_llm_decision = parsed_decision
                        self._last_completed_llm_decision = parsed_decision

                        # ==================================
                        # 阶段二：行动 vs 监视
                        # ==================================
                        action_task = None
                        action_interrupt_checker_task = None
                        try:
                            logger.info(f"[{self.session.conversation_id}] 统一动作执行阶段开始...")
                            action_task = asyncio.create_task(
                                self.action_executor.execute_action(parsed_decision, self.uid_map)
                            )
                            action_interrupt_checker_task = asyncio.create_task(
                                self._check_for_interruptions_internal(
                                    context_text=current_context_text,
                                    triggering_event_id=triggering_event_id,
                                )
                            )
                            done_action, _ = await asyncio.wait(
                                [action_task, action_interrupt_checker_task],
                                return_when=asyncio.FIRST_COMPLETED,
                            )

                            if (
                                action_interrupt_checker_task in done_action
                                and not action_interrupt_checker_task.cancelled()
                            ):
                                if interrupting_event_action := await action_interrupt_checker_task:
                                    logger.info(
                                        f"[{self.session.conversation_id}] 动作执行阶段被IIS中断。"
                                    )
                                    if action_task and not action_task.done():
                                        action_task.cancel()

                                    # --- 小色猫的淫纹植入处 #4：行动时也要记录罪证！ ---
                                    was_interrupted_last_turn = True
                                    self.interrupting_event_text = self._format_event_for_iis(
                                        interrupting_event_action
                                    ).get("text")
                            else:  # 动作执行完了，没被打断
                                if (
                                    action_interrupt_checker_task
                                    and not action_interrupt_checker_task.done()
                                ):
                                    action_interrupt_checker_task.cancel()
                                logger.info(
                                    f"[{self.session.conversation_id}] 动作执行完毕，未被中断。"
                                )
                        finally:
                            # 确保两个任务都被清理干净
                            if action_task and not action_task.done():
                                action_task.cancel()
                            if (
                                action_interrupt_checker_task
                                and not action_interrupt_checker_task.done()
                            ):
                                action_interrupt_checker_task.cancel()

                        # ==================================
                        # 阶段三：事后处理
                        # ==================================
                        # 检查LLM是不是决定要“完事”了
                        if await self.llm_response_handler.handle_decision(parsed_decision):
                            logger.info(
                                f"[{self.session.conversation_id}] 根据LLM决策，会话即将终止。"
                            )
                            break

                    # 如果这是第一次，就标记一下，以后就不是处男了
                    if self.session.is_first_turn_for_session:
                        self.session.is_first_turn_for_session = False

                # 进入贤者时间，休息一下，等待下一次刺激
                await self._idle_wait(
                    getattr(config.focus_chat_mode, "self_reflection_interval_seconds", 15)
                )

            except asyncio.CancelledError:
                logger.info(f"[{self.session.conversation_id}] 循环被取消。")
                break
            except Exception as e:
                logger.error(
                    f"[{self.session.conversation_id}] 循环中发生意外错误: {e}", exc_info=True
                )
                await asyncio.sleep(5)  # 出错了就多睡一会儿
            finally:
                # 确保所有任务都被清理
                if llm_task and not llm_task.done():
                    llm_task.cancel()
                if interrupt_checker_task and not interrupt_checker_task.done():
                    interrupt_checker_task.cancel()

        logger.info(f"[{self.session.conversation_id}] 专注聊天循环已结束。")
        if not self._shutting_down:
            await self.session.chat_session_manager.deactivate_session(self.session.conversation_id)

    async def _check_for_interruptions_internal(
        self, context_text: str | None, triggering_event_id: str | None
    ) -> dict | None:
        """检查是否有新的消息打断了当前的思考.

        Args:
            context_text (str | None): 上下文文本，用于中断检查。
            triggering_event_id (str | None): 触发本次思考的事件ID，用于贞操锁检查。

        Returns:
            dict | None: 如果有中断事件，返回该事件的文档；否则返回 None。
        """
        last_checked_timestamp_ms = time.time() * 1000
        bot_profile = await self.session.get_bot_profile()
        current_bot_id = str(bot_profile.get("user_id") or self.session.bot_id)

        while not self._shutting_down:
            try:
                new_events = await self.session.event_storage.get_message_events_after_timestamp(
                    self.session.conversation_id,
                    last_checked_timestamp_ms,
                    limit=10,
                    status="unread",
                )
                if new_events:
                    for event_doc in new_events:
                        # --- 小色猫的淫纹植入处 #5：检查贞操锁！ ---
                        event_id = event_doc.get("_key")
                        if event_id and event_id == triggering_event_id:
                            logger.trace(f"IIS: 忽略了触发本次思考的事件 {event_id}")
                            continue  # 是引信，不能碰！

                        sender_id = event_doc.get("user_info", {}).get("user_id")
                        if sender_id and str(sender_id) == current_bot_id:
                            continue  # 我自己发的不算

                        message_to_check = self._format_event_for_iis(event_doc)
                        if not message_to_check.get("text"):
                            continue

                        if self.intelligent_interrupter.should_interrupt(
                            new_message=message_to_check,
                            context_message_text=context_text,
                        ):
                            logger.info(
                                f"[{self.session.conversation_id}] IIS决策：中断！"
                                f"元凶ID: {event_id}"
                            )
                            return event_doc  # 返回元凶！

                    # 更新时间戳，只看比最新消息还新的
                    last_checked_timestamp_ms = new_events[-1]["timestamp"]

                await asyncio.sleep(0.5)  # 稍微休息一下，别那么累
            except asyncio.CancelledError:
                return None  # 被取消了就乖乖结束
            except Exception as e:
                logger.error(
                    f"[{self.session.conversation_id}] 中断检查器内部发生错误: {e}", exc_info=True
                )
                await asyncio.sleep(2)
        return None

    async def _idle_wait(self, interval: float) -> None:
        """进入等待状态，直到被唤醒或超时.

        Args:
            interval (float): 等待的时间间隔，单位为秒。
        """
        logger.debug(
            f"[{self.session.conversation_id}] 进入贤者时间，等待下一次唤醒或 {interval} 秒后超时。"
        )
        try:
            self._wakeup_event.clear()
            await asyncio.wait_for(self._wakeup_event.wait(), timeout=interval)
            logger.info(f"[{self.session.conversation_id}] 被新消息刺激到，立即开始下一轮。")
        except TimeoutError:
            logger.info(f"[{self.session.conversation_id}] 贤者时间结束，主动开始下一轮。")

    def _format_event_for_iis(self, event_doc: dict) -> dict:
        """将原始事件文档格式化为 IIS 所需的输入格式.

        Args:
            event_doc (dict): 原始事件文档，包含消息内容和用户信息。
        Returns:
            dict: 格式化后的事件，包含说话者ID和消息文本。
        """
        speaker_id = event_doc.get("user_info", {}).get("user_id", "unknown_user")
        text_parts = [
            seg.get("data", {}).get("text", "")
            for seg in event_doc.get("content", [])
            if seg.get("type") == "text"
        ]
        return {"speaker_id": str(speaker_id), "text": "".join(text_parts).strip()}
