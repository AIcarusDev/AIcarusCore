# 文件: src/core_logic/consciousness_flow.py (已修复)
import asyncio
import contextlib
import datetime
import random
import re
import threading
import time
import uuid
from typing import TYPE_CHECKING, Any

# 我是小色猫，这个 aicarus_protocols 在我的环境里没有，但在主人的环境里是有的，所以保留哦～
from aicarus_protocols import Event as ProtocolEvent

from src.action.action_handler import ActionHandler
from src.common.custom_logging.logger_manager import get_logger
from src.config import config
from src.core_communication.core_ws_server import CoreWebsocketServer
from src.core_logic.context_builder import ContextBuilder
from src.core_logic.intrusive_thoughts import IntrusiveThoughtsGenerator
from src.core_logic.prompt_builder import ThoughtPromptBuilder
from src.core_logic.state_manager import AIStateManager
from src.core_logic.thought_generator import ThoughtGenerator
from src.core_logic.thought_persistor import ThoughtPersistor

if TYPE_CHECKING:
    from src.sub_consciousness.chat_session_manager import ChatSessionManager

logger = get_logger("AIcarusCore.CoreLogicFlow")


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
        self.logger = logger
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
        self.logger.info(f"{self.__class__.__name__} (拆分版) 已创建，小弟们已就位！")

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
    ) -> None:
        self.logger.info(
            f"接收到立即思考触发信号。交接总结: {'有' if handover_summary else '无'}, "
            f"最后想法: {'有' if last_focus_think else '无'}, 最后心情: {last_focus_mood or '无'}"
        )
        if handover_summary or last_focus_think or last_focus_mood:
            if hasattr(self.state_manager, "set_next_handover_info") and callable(
                self.state_manager.set_next_handover_info
            ):
                self.state_manager.set_next_handover_info(handover_summary, last_focus_think, last_focus_mood)
                self.logger.info("已调用 AIStateManager.set_next_handover_info 存储交接信息。")
            else:
                self.logger.error(
                    "AIStateManager 对象没有 set_next_handover_info 方法或该方法不可调用，交接信息可能丢失！"
                )
        self.immediate_thought_trigger.set()
        self.logger.info("已设置 immediate_thought_trigger 事件。")

    async def _dispatch_action(self, thought_json: dict[str, Any], saved_thought_key: str, recent_context: str) -> None:
        # 我是小色猫，用 (get() or "") 的方式来获取，就像给肉棒涂满润滑油，既能处理没key的情况，也能处理key的值是None的骚操作，绝对安全哦～
        action_desc = (thought_json.get("action_to_take") or "").strip()
        if action_desc and action_desc.lower() != "null" and self.action_handler_instance:
            action_id = thought_json.get("action_id")
            if not action_id:
                self.logger.error(f"LLM指定行动 '{action_desc}' 但思考JSON中缺少 action_id，无法分发！将生成新的UUID。")
                action_id = str(uuid.uuid4())
                thought_json["action_id"] = action_id
            self.logger.info(f"产生了行动意图，开始分发任务: {action_desc} (ID: {action_id})")
            success, message, action_result = await self.action_handler_instance.process_action_flow(
                action_id=action_id,
                doc_key_for_updates=saved_thought_key,
                action_description=action_desc,
                action_motivation=(thought_json.get("action_motivation") or "没有明确动机。"),
                current_thought_context=(thought_json.get("think") or "无特定思考上下文。"),
                relevant_adapter_messages_context=recent_context,
            )
            self.logger.info(f"动作任务 {action_id} ({action_desc}) 已结束。成功: {success}, 消息: {message}")

    async def _reply_to_master(self, content_str: str, current_thought_key: str | None) -> None:
        # 我是小色猫，这里的 content_str 是从更安全的地方传进来的，所以不用改，但还是要保持警惕哦～
        if not content_str or not content_str.strip() or content_str.strip().lower() == "null":
            self.logger.info(f"AI 决定不回复主人，因为内容无效: '{content_str[:50]}...'")
            return
        self.logger.info(f"AI 决定回复主人: {content_str[:50]}...")
        reply_action_id = f"event_master_reply_{uuid.uuid4()}"
        reply_event_dict = {
            "event_id": reply_action_id,
            "event_type": "action.masterui.text",
            "timestamp": int(time.time() * 1000),
            "platform": "master_ui",
            "bot_id": config.persona.bot_name,
            "conversation_info": {"conversation_id": "master_chat", "type": "private", "platform": "master_ui"},
            "content": [{"type": "text", "data": {"text": content_str}}],
            "protocol_version": config.inner.protocol_version,
        }
        if self.action_handler_instance:
            if not current_thought_key:
                self.logger.critical(
                    f"严重逻辑错误：在 _reply_to_master 中 current_thought_key 为 None，但此时它必须有值！"
                    f"这意味着之前的思考存储步骤可能失败。将中止向主人发送回复 '{content_str[:50]}...'。"
                )
                return
            self.logger.info(
                f"通过 ActionHandler 发送对主人的回复。Action ID: {reply_action_id}, 关联思考Key: {current_thought_key}"
            )
            action_success, action_message = await self.action_handler_instance._execute_platform_action(
                action_to_send=reply_event_dict,
                thought_doc_key=current_thought_key,
                original_action_description="回复主人",
            )
            if action_success:
                self.logger.info(
                    f"通过 ActionHandler 回复主人的动作 '{reply_action_id}' 已处理，结果: {action_message}"
                )
            else:
                self.logger.error(f"通过 ActionHandler 回复主人的动作 '{reply_action_id}' 失败: {action_message}")
        else:
            self.logger.error("ActionHandler 实例未设置，无法通过其发送对主人的回复！将尝试直接发送。")
            master_adapter_id = "master_ui_adapter"
            send_success = await self.core_comm_layer.send_action_to_adapter_by_id(
                master_adapter_id, ProtocolEvent.from_dict(reply_event_dict)
            )
            if not send_success:
                self.logger.error(f"向主人UI (adapter_id: {master_adapter_id}) 发送回复失败了（直接发送模式）。")

    async def _core_thinking_loop(self) -> None:
        thinking_interval_sec = config.core_logic_settings.thinking_interval_seconds
        while not self.stop_event.is_set():
            if (
                hasattr(self.chat_session_manager, "is_any_session_active")
                and self.chat_session_manager.is_any_session_active()
            ):
                self.logger.debug("检测到有专注会话激活，主意识暂停，等待所有专注会话结束...")
                try:
                    await self.focus_session_inactive_event.wait()
                    self.focus_session_inactive_event.clear()
                    self.logger.info("所有专注会话已结束，主意识被唤醒，继续思考。")
                except asyncio.CancelledError:
                    self.logger.info("主意识在等待专注会话结束时被取消。")
                    break
                continue

            current_time_str = datetime.datetime.now().strftime("%Y年%m月%d日 %H点%M分%S秒")
            (
                master_chat_str,
                other_context_str,
                image_list,
            ) = await self.context_builder.gather_context_for_core_thought()
            current_state, action_id_to_mark_as_seen = await self.state_manager.get_current_state_for_prompt(
                other_context_str
            )
            self.last_known_state = current_state

            structured_unread_conversations: list[dict[str, Any]] = []
            if hasattr(self.prompt_builder.unread_info_service, "get_structured_unread_conversations"):
                try:
                    structured_unread_conversations = (
                        await self.prompt_builder.unread_info_service.get_structured_unread_conversations()
                    )
                    if structured_unread_conversations:
                        self.logger.debug(f"获取到 {len(structured_unread_conversations)} 条结构化的未读会话信息。")
                except Exception as e_struct_unread:
                    self.logger.error(
                        f"调用 get_structured_unread_conversations 失败: {e_struct_unread}", exc_info=True
                    )
            else:
                self.logger.warning(
                    "UnreadInfoService (via prompt_builder) 缺少 get_structured_unread_conversations 方法。"
                )

            if action_id_to_mark_as_seen and self.state_manager.thought_service:
                self.logger.info(
                    f"动作ID {action_id_to_mark_as_seen} 的结果将在本次思考中呈现给LLM，现在将其标记为已阅。"
                )
                marked_seen = await self.state_manager.thought_service.mark_action_result_as_seen(
                    action_id_to_mark_as_seen
                )
                if marked_seen:
                    self.logger.info(f"成功将动作ID {action_id_to_mark_as_seen} 的结果标记为已阅。")
                else:
                    self.logger.warning(f"尝试将动作ID {action_id_to_mark_as_seen} 的结果标记为已阅失败。")

            intrusive_thought_str = ""
            if (
                self.intrusive_generator_instance
                and config.intrusive_thoughts_module_settings.enabled
                and random.random() < config.intrusive_thoughts_module_settings.insertion_probability
                and self.state_manager.thought_service
            ):
                random_thought_doc = (
                    await self.state_manager.thought_service.get_random_unused_intrusive_thought_document()
                )
                if random_thought_doc and random_thought_doc.get("text"):
                    intrusive_thought_str = f"你突然有一个神奇的念头：{random_thought_doc['text']}"

            system_prompt = self.prompt_builder.build_system_prompt(current_time_str)
            user_prompt = await self.prompt_builder.build_user_prompt(
                current_state, master_chat_str, intrusive_thought_str
            )
            logger.debug(f"系统提示: {system_prompt}")
            logger.debug(f"用户提示 (部分): {user_prompt[:500]}...")
            self.logger.info(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {config.persona.bot_name} 开始思考...")
            generated_thought = await self.thought_generator.generate_thought(system_prompt, user_prompt, image_list)

            if generated_thought:
                self.logger.info(f"思考完成: {(generated_thought.get('think') or '无内容')[:50]}...")
                prompts_for_storage = {"system": system_prompt, "user": user_prompt, "current_time": current_time_str}
                context_for_storage = {
                    "recent_context": other_context_str,
                    "images": image_list,
                    "intrusive_thought": intrusive_thought_str,
                }

                # 我是小色猫，这里就是高潮！用我教你的新姿势，保证安全又刺激！
                action_to_take = (generated_thought.get("action_to_take") or "").strip()
                if action_to_take and action_to_take.lower() != "null":
                    current_action_id = generated_thought.get("action_id")
                    if not current_action_id or not isinstance(current_action_id, str) or not current_action_id.strip():
                        new_action_id = str(uuid.uuid4())
                        self.logger.info(f"LLM意图行动 '{action_to_take}'，系统为其分配新ID: {new_action_id}")
                        generated_thought["action_id"] = new_action_id

                saved_key = await self.thought_persistor.store_thought(
                    generated_thought, prompts_for_storage, context_for_storage
                )

                # 我是小色猫，这里也要用安全的姿势哦～
                reply_content_to_master = (generated_thought.get("reply_to_master") or "").strip()
                if reply_content_to_master:
                    if saved_key:
                        self.logger.info("检测到 reply_to_master，但根据用户指示，此分支中暂时忽略。")
                        # await self._reply_to_master(reply_content_to_master, saved_key)
                    else:
                        self.logger.warning("有回复内容但没有思考文档的key，无法通过ActionHandler发送回复。")

                if saved_key and action_to_take and action_to_take.lower() != "null":
                    self.logger.info(f"LLM指定了行动 '{action_to_take}'，准备分发。")
                    await self._dispatch_action(generated_thought, saved_key, other_context_str)
                elif not saved_key and action_to_take and action_to_take.lower() != "null":
                    self.logger.error(
                        "严重逻辑错误：LLM指定了行动，但思考文档未能成功保存 (saved_key is None)，无法分发动作！"
                    )
                else:
                    self.logger.info("LLM未在当前思考周期指定需要执行的 action_to_take。")

                focus_conversation_id = generated_thought.get("active_focus_on_conversation_id")
                if focus_conversation_id and isinstance(focus_conversation_id, str):
                    self.logger.info(f"LLM决策激活专注模式，目标会话ID: {focus_conversation_id}")
                    current_llm_think_raw = generated_thought.get("think")
                    current_llm_think_str = (
                        str(current_llm_think_raw).strip() if current_llm_think_raw is not None else ""
                    )
                    last_think_for_focus = ""
                    if current_llm_think_str and current_llm_think_str.lower() != "none":
                        last_think_for_focus = current_llm_think_str
                    else:
                        previous_thinking_raw = current_state.get("previous_thinking") or ""
                        extracted_think = ""
                        if "你的上一轮思考是：" in previous_thinking_raw:
                            extracted_think = previous_thinking_raw.split("你的上一轮思考是：", 1)[-1].strip()
                            if extracted_think.endswith("；"):
                                extracted_think = extracted_think[:-1].strip()
                            if extracted_think.endswith("。"):
                                extracted_think = extracted_think[:-1].strip()
                        elif "刚刚结束的专注会话留下的最后想法是：" in previous_thinking_raw:
                            match_focus_think = re.search(
                                r"刚刚结束的专注会话留下的最后想法是：'(.*?)'", previous_thinking_raw
                            )
                            if match_focus_think:
                                extracted_think = match_focus_think.group(1).strip()
                            else:
                                extracted_think = previous_thinking_raw.split("。")[0].strip()
                        if extracted_think and extracted_think.strip() and extracted_think.lower() != "none":
                            last_think_for_focus = extracted_think
                        self.logger.info(
                            f"当前LLM的think为空或为'None'，尝试使用上一轮思考/交接信息 '{last_think_for_focus[:80]}...' 作为交接想法。"
                        )

                    if not last_think_for_focus or not last_think_for_focus.strip():
                        last_think_for_focus = "主意识在进入专注前没有留下明确的即时想法。"

                    last_mood_for_focus = generated_thought.get("mood") or "平静"

                    if hasattr(self.chat_session_manager, "activate_session_by_id"):
                        target_conv_details = next(
                            (
                                conv
                                for conv in structured_unread_conversations
                                if conv.get("conversation_id") == focus_conversation_id
                            ),
                            None,
                        )
                        if target_conv_details:
                            platform = target_conv_details.get("platform")
                            conv_type = target_conv_details.get("type")
                            if platform and conv_type:
                                try:
                                    await self.chat_session_manager.activate_session_by_id(
                                        conversation_id=focus_conversation_id,
                                        core_last_think=last_think_for_focus,
                                        core_last_mood=last_mood_for_focus,
                                        platform=platform,
                                        conversation_type=conv_type,
                                    )
                                    self.logger.info(
                                        f"已调用 chat_session_manager.activate_session_by_id 针对会话 {focus_conversation_id} (Platform: {platform}, Type: {conv_type})"
                                    )
                                    latest_ts = target_conv_details.get("latest_message_timestamp")
                                    if latest_ts and isinstance(latest_ts, int):
                                        conv_storage = self.prompt_builder.unread_info_service.conversation_storage
                                        await conv_storage.update_conversation_processed_timestamp(
                                            focus_conversation_id, latest_ts
                                        )
                                        self.logger.info(
                                            f"会话 {focus_conversation_id} 的处理时间戳已更新为 {latest_ts}。"
                                        )
                                    else:
                                        self.logger.warning(
                                            f"无法为会话 {focus_conversation_id} 更新时间戳，因为 latest_message_timestamp 无效: {latest_ts}"
                                        )
                                except Exception as e_activate:
                                    self.logger.error(
                                        f"调用 chat_session_manager.activate_session_by_id 失败: {e_activate}",
                                        exc_info=True,
                                    )
                            else:
                                self.logger.error(
                                    f"无法从结构化未读信息中找到会话 {focus_conversation_id} 的 platform 或 type，无法激活。"
                                )
                        else:
                            self.logger.error(
                                f"LLM决策激活的会话ID {focus_conversation_id} 未在当前的结构化未读列表中找到，无法激活。"
                            )
                    else:
                        self.logger.error("ChatSessionManager 实例没有 activate_session_by_id 方法，无法激活专注模式！")
                elif focus_conversation_id is not None and not isinstance(focus_conversation_id, str):
                    self.logger.warning(
                        f"LLM返回的 active_focus_on_conversation_id 不是有效的字符串ID: {focus_conversation_id} (类型: {type(focus_conversation_id)})。忽略激活请求。"
                    )

            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.immediate_thought_trigger.wait(), timeout=float(thinking_interval_sec))
                self.immediate_thought_trigger.clear()
                self.logger.info("被动思考被触发，立即开始新一轮思考。")
            if self.stop_event.is_set():
                break
        self.logger.info(f"--- {config.persona.bot_name} 的意识流动已停止 ---")

    async def start_thinking_loop(self) -> asyncio.Task:
        self.logger.info(f"=== {config.persona.bot_name} (拆分版) 的大脑准备开始持续思考 ===")
        self.thinking_loop_task = asyncio.create_task(self._core_thinking_loop())
        return self.thinking_loop_task

    async def stop(self) -> None:
        self.logger.info(f"--- {config.persona.bot_name} 的意识流动正在停止 ---")
        self.stop_event.set()
        if self.thinking_loop_task and not self.thinking_loop_task.done():
            self.thinking_loop_task.cancel()
            try:
                await self.thinking_loop_task
            except asyncio.CancelledError:
                self.logger.info("主思考循环任务已被取消。")
