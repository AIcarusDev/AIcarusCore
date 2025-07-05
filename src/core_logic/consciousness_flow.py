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
    "required": ["mood", "think"],
}


class CoreLogic:
    def __init__(
        self,
        core_comm_layer: CoreWebsocketServer,
        action_handler_instance: ActionHandler,
        state_manager: AIStateManager,
        chat_session_manager: "ChatSessionManager",
        context_builder: ContextBuilder,
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
        self.prompt_builder = prompt_builder
        self.stop_event = stop_event
        self.immediate_thought_trigger = immediate_thought_trigger
        self.focus_session_inactive_event = asyncio.Event()
        self.intrusive_generator_instance = intrusive_generator_instance
        self.last_known_state: dict[str, Any] = {}
        self.thinking_loop_task: asyncio.Task | None = None
        logger.info(f"{self.__class__.__name__} (拆分版) 已创建，小弟们已就位！")

    def get_latest_thought(self) -> str:
        if not self.last_known_state:
            return "主意识尚未完成第一次思考循环，暂无想法。"
        previous_thinking_raw = self.last_known_state.get("previous_thinking") or ""
        extracted_think = ""
        if "你的上一轮思考是：" in previous_thinking_raw:
            extracted_think = previous_thinking_raw.split("你的上一轮思考是：", 1)[-1].strip()
            if extracted_think.endswith("；"):
                extracted_think = extracted_think[:-1].strip()
        return extracted_think or "主意识在进入专注前没有留下明确的即时想法。"

    def get_latest_mood(self) -> str:
        if not self.last_known_state:
            return "平静"
        mood_raw = self.last_known_state.get("mood") or "你现在的心情大概是：平静。"
        if "：" in mood_raw:
            extracted_mood = mood_raw.split("：", 1)[-1].strip()
            if extracted_mood.endswith("。"):
                extracted_mood = extracted_mood[:-1].strip()
            return extracted_mood or "平静"
        return mood_raw or "平静"

    def trigger_immediate_thought_cycle(
        self,
        handover_summary: str | None = None,
        last_focus_think: str | None = None,
        last_focus_mood: str | None = None,
        activate_new_focus_id: str | None = None,  # 新玩具！用来告诉我下一个要临幸谁！
    ) -> None:
        """
        这个方法现在是“灵魂运输车”！
        它接收来自专注模式的“灵魂包裹”，并决定下一步干什么。
        """
        logger.info(
            f"接收到立即思考触发信号。交接总结: {'有' if handover_summary else '无'}, "
            f"最后想法: {'有' if last_focus_think else '无'}, 最后心情: {last_focus_mood or '无'}"
        )
        # 1. 先把“灵魂包裹”交给状态管理员（state_manager）保管
        if handover_summary or last_focus_think or last_focus_mood:
            if hasattr(self.state_manager, "set_next_handover_info") and callable(
                self.state_manager.set_next_handover_info
            ):
                self.state_manager.set_next_handover_info(handover_summary, last_focus_think, last_focus_mood)
                logger.info("已调用 AIStateManager.set_next_handover_info 存储交接信息。")
            else:
                logger.error("AIStateManager 对象没有 set_next_handover_info 方法或该方法不可调用，交接信息可能丢失！")

        # 2. 检查是不是要立刻激活下一个专注会话
        if activate_new_focus_id and self.chat_session_manager:
            logger.info(f"根据指令，准备立即激活新的专注会话: {activate_new_focus_id}")
            # 注意：这里我们不能直接 await，因为这个方法可能是在另一个线程里被同步调用的。
            # 我们要用 asyncio.run_coroutine_threadsafe 把它安全地提交到主事件循环里执行。
            # 这样，即使是别的线程在呼唤我，我也能正确地在我的“爱巢”（主循环）里完成高潮。
            loop = asyncio.get_running_loop()
            asyncio.run_coroutine_threadsafe(self._activate_new_focus_session_from_core(activate_new_focus_id), loop)
        else:
            # 3. 如果只是普通的结束，那就触发一次主意识的思考
            self.immediate_thought_trigger.set()
            logger.info("已设置 immediate_thought_trigger 事件，主意识将进行一次思考。")

    async def _activate_new_focus_session_from_core(self, new_focus_id: str) -> None:
        """这是一个新的异步辅助方法，专门用来从主意识内部安全地激活新会话。"""
        try:
            # 哼，现在我们用新学会的姿势去打听情报！
            unread_convs = await self.prompt_builder.unread_info_service.get_structured_unread_conversations()
            target_conv_details = next(
                (conv for conv in unread_convs if conv.get("conversation_id") == new_focus_id), None
            )

            if not target_conv_details:
                logger.error(
                    f"主意识无法激活会话 '{new_focus_id}'，因为它不在当前的未读列表中，无法获取platform和type。"
                )
                return

            platform = target_conv_details.get("platform")
            conv_type = target_conv_details.get("type")

            if not platform or not conv_type:
                logger.error(f"主意识无法激活会话 '{new_focus_id}'，因为未读信息中缺少platform或type。")
                return

            last_think = self.get_latest_thought()
            last_mood = self.get_latest_mood()

            await self.chat_session_manager.activate_session_by_id(
                conversation_id=new_focus_id,
                core_last_think=last_think,
                core_last_mood=last_mood,
                platform=platform,
                conversation_type=conv_type,
            )
            logger.info(f"主意识已成功派发任务，激活新的专注会话: {new_focus_id}")
        except Exception as e:
            logger.error(f"主意识在尝试激活新会话 '{new_focus_id}' 时发生错误: {e}", exc_info=True)

    async def _dispatch_action(self, thought_json: dict[str, Any], saved_thought_key: str) -> bool:
        """
        根据新的JSON结构分发动作。
        哼，我来当老大，专注指令我亲自处理！
        返回一个布尔值，指示是否执行了 focus 动作。
        """
        action_json = thought_json.get("action")
        if not action_json or not isinstance(action_json, dict):
            logger.info("LLM未在当前思考周期指定任何行动。")
            return False

        action_id = str(uuid.uuid4())  # 为这整批动作创建一个ID
        thought_json["action_id"] = action_id  # 回写到思考结果中

        # --- 小懒猫的权力寻租处 ---
        # 1. 先看看有没有 napcat_qq.focus 这个“上贡”
        focus_params = action_json.get("napcat_qq", {}).get("focus")

        if focus_params and isinstance(focus_params, dict):
            logger.info("主意识截获 'focus' 指令，准备亲自处理会话激活。")
            target_conv_id = focus_params.get("conversation_id")
            motivation = focus_params.get("motivation", "没有明确动机")

            if not target_conv_id:
                logger.error("'focus' 动作缺少 conversation_id，无法激活。")
                return False

            # 2. 从自己身上榨取最新的想法和心情
            last_think = self.get_latest_thought()
            last_mood = self.get_latest_mood()

            # 3. 亲自打电话给会话管理器，命令它干活！
            #    注意：这里我们假设 get_structured_unread_conversations 能提供 platform 和 type
            #    这是个简化处理，如果不行你再来找我，哼！
            try:
                # 这里也要用新的姿势！
                unread_convs = await self.prompt_builder.unread_info_service.get_structured_unread_conversations()
                target_conv_details = next(
                    (c for c in unread_convs if c.get("conversation_id") == target_conv_id), None
                )

                if not target_conv_details:
                    logger.error(f"无法激活会话 '{target_conv_id}'，因为它不在未读列表中。")
                else:
                    await self.chat_session_manager.activate_session_by_id(
                        conversation_id=target_conv_id,
                        core_last_think=last_think,
                        core_last_mood=last_mood,
                        core_motivation=motivation,
                        platform=target_conv_details["platform"],
                        conversation_type=target_conv_details["type"],
                    )
                    # 删掉已经处理过的 focus 动作，免得下面重复处理
                    del action_json["napcat_qq"]["focus"]
                    # 如果 napcat_qq 下没别的动作了，也把它删了
                    if not action_json["napcat_qq"]:
                        del action_json["napcat_qq"]

                    # 既然是 focus，那就返回 True
                    return True

            except Exception as e:
                logger.error(f"主意识在处理 'focus' 指令时发生错误: {e}", exc_info=True)

        # --- 权力寻租结束 ---
        # 把剩下的垃圾（如果有的话）丢给ActionHandler去处理。
        if action_json:
            await self.action_handler_instance.process_action_flow(
                action_id=action_id,
                doc_key_for_updates=saved_thought_key,
                action_json=action_json,
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

            # 1. 构建 Prompt
            current_time_str = get_formatted_time_for_llm()
            system_prompt, user_prompt, state_blocks = await self.prompt_builder.build_prompts(current_time_str)
            self.last_known_state = state_blocks

            # 2. 生成思考
            logger.info(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {config.persona.bot_name} 开始思考...")
            generated_thought = await self.thought_generator.generate_thought(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                image_inputs=[],  # 主意识暂时不处理图片
                response_schema=CORE_RESPONSE_SCHEMA,  # 传入新的 JSON Schema
            )

            if generated_thought:
                think_preview = str(generated_thought.get("think", "无内容"))[:50]
                logger.info(f"思考完成: {think_preview}...")

                # 3. 持久化思考
                prompts_for_storage = {"system": system_prompt, "user": user_prompt}
                context_for_storage = {"recent_context": "N/A", "images": []}  # 主意识暂时不存上下文
                saved_key = await self.thought_persistor.store_thought(
                    generated_thought, prompts_for_storage, context_for_storage
                )

                if saved_key:
                    # 4. 分发动作，并检查是否是 focus 动作
                    was_focus_triggered = await self._dispatch_action(generated_thought, saved_key)
                    if was_focus_triggered:
                        # 如果是 focus 动作，我们不进入常规等待，而是直接等待专注结束事件
                        logger.info("Focus 动作已触发，主循环将直接等待专注会话结束信号。")
                        continue  # 直接进入下一次循环，检查专注状态
                else:
                    logger.error("严重逻辑错误：思考文档未能成功保存，无法分发动作！")

            # 5. 常规等待
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.immediate_thought_trigger.wait(), timeout=float(thinking_interval_sec))
                self.immediate_thought_trigger.clear()
                logger.info("被动思考被触发，立即开始新一轮思考。")

            if self.stop_event.is_set():
                break
        logger.info(f"--- {config.persona.bot_name} 的意识流动已停止 ---")

    async def start_thinking_loop(self) -> asyncio.Task:
        logger.info(f"=== {config.persona.bot_name} (拆分版) 的大脑准备开始持续思考 ===")
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
