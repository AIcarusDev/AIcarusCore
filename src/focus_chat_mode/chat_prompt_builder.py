# src/focus_chat_mode/chat_prompt_builder.py (小懒猫·独立思考完整版)
import contextlib
import os
from typing import TYPE_CHECKING, Any

from src.action.action_handler import ActionHandler
from src.common.custom_logging.logging_config import get_logger
from src.common.focus_chat_history_builder.chat_history_formatter import format_chat_history_for_llm
from src.common.time_utils import get_formatted_time_for_llm

# 导入你的顶层config对象
from src.config import config
from src.database.services.event_storage_service import EventStorageService
from src.prompt_templates import prompt_templates

from .components import PromptComponents

if TYPE_CHECKING:
    from .chat_session import ChatSession

logger = get_logger(__name__)


class ChatPromptBuilder:
    def __init__(
        self,
        session: "ChatSession",
        event_storage: EventStorageService,
        action_handler: ActionHandler,
        bot_id: str,
        platform: str,
        conversation_id: str,
        conversation_type: str,
    ) -> None:
        self.session = session
        self.event_storage: EventStorageService = event_storage
        self.action_handler: ActionHandler = action_handler
        self.bot_id: str = bot_id
        self.platform: str = platform
        self.conversation_id: str = conversation_id
        self.conversation_type: str = conversation_type

        try:
            self._temp_image_dir = config.runtime_environment.temp_file_directory
            if not self._temp_image_dir:
                logger.warning("配置文件中的 temp_file_directory 为空，将使用默认备用路径。")
                # 尝试从 config_paths 获取 PROJECT_ROOT 作为备用方案的基础
                try:
                    from src.config.config_paths import PROJECT_ROOT

                    self._temp_image_dir = str(PROJECT_ROOT / "temp_images_runtime_fallback")
                except ImportError:
                    logger.error(
                        "无法从 src.config.config_paths 导入 PROJECT_ROOT，备用临时目录将基于当前文件位置猜测。"
                    )
                    current_file_path = os.path.abspath(__file__)
                    # 假设此文件在 AIcarusCore/src/logic/chat/chat_prompt_builder.py
                    project_root_guess = os.path.dirname(
                        os.path.dirname(os.path.dirname(os.path.dirname(current_file_path)))
                    )
                    self._temp_image_dir = os.path.join(project_root_guess, "temp_images_runtime_fallback")
        except AttributeError as e:
            logger.error(
                f"无法从配置 (config.runtime_environment.temp_file_directory) 获取临时文件目录: {e}。"
                "请检查配置文件结构和内容。将使用默认备用路径。"
            )
            try:
                from src.config.config_paths import PROJECT_ROOT

                self._temp_image_dir = str(PROJECT_ROOT / "temp_images_runtime_fallback_attr_error")
            except ImportError:
                logger.error(
                    "无法从 src.config.config_paths 导入 PROJECT_ROOT，备用临时目录将基于当前文件位置猜测 (AttributeError)。"
                )
                current_file_path = os.path.abspath(__file__)
                project_root_guess = os.path.dirname(
                    os.path.dirname(os.path.dirname(os.path.dirname(current_file_path)))
                )
                self._temp_image_dir = os.path.join(project_root_guess, "temp_images_runtime_fallback_attr_error")

        os.makedirs(self._temp_image_dir, exist_ok=True)
        logger.info(
            f"[ChatPromptBuilder][{self.conversation_id}] 实例已创建 (bot_id: {self.bot_id}, type: {self.conversation_type}). "
            f"将使用临时图片目录: {self._temp_image_dir}"
        )

    async def build_prompts(
        self,
        session: "ChatSession",
        last_processed_timestamp: float,
        is_first_turn: bool,
        motivation_from_core: str | None = None,  # 只需要知道为什么来这里就够了
        was_last_turn_interrupted: bool = False,
        interrupting_event_text: str | None = None,
    ) -> PromptComponents:
        """
        构建专注聊天模式下给LLM的System Prompt和User Prompt。
        哼，看好了，这才叫专业的组装方式！
        """
        # --- 步骤1：准备模板和一些通用信息 ---
        logger.debug(f"[{self.session.conversation_id}] 开始构建Prompt...")

        # 决定用哪个模板
        if self.session.conversation_type == "private":
            system_prompt_template = prompt_templates.PRIVATE_SYSTEM_PROMPT
            user_prompt_template = prompt_templates.PRIVATE_USER_PROMPT
        else:
            system_prompt_template = prompt_templates.GROUP_SYSTEM_PROMPT
            user_prompt_template = prompt_templates.GROUP_USER_PROMPT

        current_time_str = get_formatted_time_for_llm()
        persona_config = config.persona
        bot_name_str = persona_config.bot_name or "AI"
        bot_description_str = f"\n{persona_config.description}" if persona_config.description else ""
        bot_profile_str = f"\n{persona_config.profile}" if persona_config.profile else ""

        # 看这里！现在多省事，直接喊小弟过来干活！
        dynamic_guidance_str = self.session.guidance_generator.generate_guidance()

        unread_summary_str = "所有其他会话均无未读消息。"
        if (
            self.session.core_logic
            and hasattr(self.session.core_logic, "prompt_builder")
            and self.session.core_logic.prompt_builder
        ):
            try:
                unread_summary_str = (
                    await self.session.core_logic.prompt_builder.unread_info_service.generate_unread_summary_text(
                        exclude_conversation_id=self.session.conversation_id
                    )
                )
            except Exception as e:
                logger.error(f"[{self.session.conversation_id}] 获取未读消息摘要失败: {e}", exc_info=True)
                unread_summary_str = "获取其他会话摘要时出错。"
        else:
            logger.warning(f"[{self.session.conversation_id}] 无法获取 unread_info_service，未读消息摘要将为空。")

        # --- 步骤2：调用新玩具，获取所有格式化好的聊天记录相关信息 ---
        logger.debug(f"[{self.session.conversation_id}] 调用通用聊天记录格式化工具...")
        bot_profile = await session.get_bot_profile()

        # // 在这里调用我们新加的“情报获取术”
        conversation_details = await session.get_conversation_details()

        prompt_components = await format_chat_history_for_llm(
            event_storage=self.event_storage,
            conversation_id=self.session.conversation_id,
            bot_id=self.session.bot_id,
            platform=self.session.platform,
            bot_profile=bot_profile,
            conversation_type=self.session.conversation_type,
            conversation_name=self.session.conversation_name,
            last_processed_timestamp=last_processed_timestamp,
            is_first_turn=is_first_turn,
            raw_events_from_caller=None,  # 在专注模式下，总是从数据库拉取
        )

        # --- 步骤3：使用新玩具返回的结果，准备剩下的Prompt零件 ---
        # 处理用户昵称
        user_nick = ""
        # 如果是私聊模式，尝试从用户映射中获取对方昵称
        if self.session.conversation_type == "private":
            final_bot_id = str(bot_profile.get("user_id", self.bot_id))
            # 遍历用户映射，找到对方的昵称
            for p_id, user_data_val in prompt_components.user_map.items():
                # 如果是对方用户（不是bot），并且不是当前bot的ID
                if user_data_val.get("uid_str") == "U1" and p_id != final_bot_id:
                    user_nick = user_data_val.get("nick", "对方")
                    # 找到后就不再继续遍历了
                    break

        # // 这是最关键的改造！我们现在直接从数据库拿最新的思考状态！
        latest_thought_doc = await self.session.thought_storage_service.get_latest_thought_document()

        previous_thoughts_block_str = self._build_previous_thoughts_block(
            is_first_turn=is_first_turn,
            was_interrupted=was_last_turn_interrupted,
            latest_thought_doc=latest_thought_doc,  # 传最新的思想点进去
            session=session,
            interrupt_text=interrupting_event_text,
            motivation_from_core=motivation_from_core,
        )

        final_bot_id = str(bot_profile.get("user_id", self.bot_id))
        final_bot_nickname = bot_profile.get("nickname", persona_config.bot_name or "bot")
        final_bot_card = bot_profile.get("card", final_bot_nickname)

        # // 从我们打探到的情报里，把人数拿出来！用 .get() 是个好习惯，免得没有的时候程序哭鼻子。
        member_count = conversation_details.get("member_count", "未知")
        max_member_count = conversation_details.get("max_member_count", "未知")

        # 组装 System Prompt
        # 如果是群聊，使用群聊的名称；否则使用默认的“未知群聊”
        if self.session.conversation_type == "group":
            system_prompt = system_prompt_template.format(
                current_time=current_time_str,
                bot_name=bot_name_str,
                optional_description=bot_description_str,
                optional_profile=bot_profile_str,
                bot_id=final_bot_id,
                bot_nickname=final_bot_nickname,
                conversation_name=prompt_components.conversation_name or "未知群聊",
                bot_card=final_bot_card,
                member_count=member_count,
            )
        # 如果是私聊，使用对方的昵称；否则使用默认的“对方”
        else:
            system_prompt = system_prompt_template.format(
                current_time=current_time_str,
                bot_name=bot_name_str,
                bot_id=final_bot_id,
                optional_description=bot_description_str,
                optional_profile=bot_profile_str,
                user_nick=user_nick,
            )

        # 组装 User Prompt
        user_prompt = user_prompt_template.format(
            unread_summary=unread_summary_str,
            conversation_info_block=prompt_components.conversation_info_block,
            user_list_block=prompt_components.user_list_block,
            chat_history_log_block=prompt_components.chat_history_log_block,
            previous_thoughts_block=previous_thoughts_block_str,
            dynamic_behavior_guidance=dynamic_guidance_str,
            member_count=member_count,
            max_member_count=max_member_count,
        )

        prompt_components.system_prompt = system_prompt
        prompt_components.user_prompt = user_prompt

        logger.debug(f"[{self.session.conversation_id}] Prompts构建完成。")
        logger.debug(f"[{self.session.conversation_id}] System Prompt: {system_prompt}...")
        logger.debug(f"[{self.session.conversation_id}] User Prompt: {user_prompt}...")
        return prompt_components

    def _build_previous_thoughts_block(
        self,
        is_first_turn: bool,
        was_interrupted: bool,
        latest_thought_doc: dict[str, Any] | None,  # 接收最新的思想点
        session: "ChatSession",
        interrupt_text: str | None,
        motivation_from_core: str | None,
    ) -> str:
        # 优先处理中断情况
        if was_interrupted:
            if session.messages_sent_this_turn == 0:
                if latest_thought_doc:
                    think = latest_thought_doc.get("think", "我被打断前正在想...")
                    mood = latest_thought_doc.get("mood", "平静")
                    return f'刚刚你的心情是："{mood}"。\n刚刚你的内心想法是："{think}"。\n但你还没来得及做出任何行动，就被新的消息“{interrupt_text or "某条新消息"}”吸引了注意。'
                return f"你正准备开始思考，但就被新的消息“{interrupt_text or '某条新消息'}”打断了。你需要先处理这个新情况。"
            else:
                if latest_thought_doc:
                    think = latest_thought_doc.get("think", "我被打断前正在想...")
                    mood = latest_thought_doc.get("mood", "平静")
                    motivation = "（动机信息遗失）"
                    if latest_thought_doc.get("action_payload"):
                        with contextlib.suppress(IndexError, AttributeError):
                            motivation = next(
                                iter(next(iter(latest_thought_doc["action_payload"].values())).values())
                            ).get("motivation", "（动机信息遗失）")

                    parts = [
                        f'刚刚你的心情是："{mood}"。',
                        f'刚刚你的内心想法是："{think}"。',
                        f"出于这个想法，你决定发言，并计划发送 {session.messages_planned_this_turn} 条消息。",
                        f"原因是：{motivation}。" if motivation else "",
                        f"但是，在你发送了 {session.messages_sent_this_turn} 条消息后，新的消息“{interrupt_text or '某条新消息'}”让你感到意外，所以你停下了后续的发言。现在你需要基于这个新情况重新思考。",
                    ]
                    return "\n".join(p for p in parts if p)
                return "你在发送消息时被打断了，但上一轮的思考记录丢失了。请重新评估情况。"

        # 如果是进入专注模式的第一轮
        if is_first_turn:
            mood_part = "平静"
            think_part = "我好像忘了"
            if latest_thought_doc:
                mood_part = latest_thought_doc.get("mood", "平静")
                think_part = latest_thought_doc.get("think", "我好像忘了")

            motivation_part = motivation_from_core or "我决定过来看看。"

            # // 这就是你想要的那个开场白！
            return f"你刚才的心情是“{mood_part}”。\n你刚才的想法是：“{think_part}”。\n你现在刚刚把注意力放到这个会话中，因为：“{motivation_part}”。"

        # // 后续的正常循环也从最新的思想点里拿信息
        if latest_thought_doc:
            think = latest_thought_doc.get("think", "我好像忘了")
            mood = latest_thought_doc.get("mood", "平静")

            action_desc = "暂时不发言"
            motivation = ""
            action_payload = latest_thought_doc.get("action_payload")
            if action_payload:
                try:
                    platform, actions = next(iter(action_payload.items()))
                    action_name, params = next(iter(actions.items()))
                    motivation = params.get("motivation", "")

                    if action_name == "send_message":
                        content = params.get("content", [])
                        text_parts = [
                            seg.get("data", {}).get("text", "") for seg in content if seg.get("type") == "text"
                        ]
                        full_text = "".join(text_parts)
                        action_desc = f"发言（内容：{full_text[:30]}{'...' if len(full_text) > 30 else ''}）"
                    else:
                        action_desc = f"执行了动作：{platform}.{action_name}"

                except (IndexError, AttributeError):
                    action_desc = "执行了一个未知动作"

            parts = [f"刚刚你的心情是：“{mood}”\n刚刚你的内心想法是：“{think}”"]
            parts.append(f"出于这个想法，你刚才做了：{action_desc}")
            if motivation:
                parts.append(f"因为：{motivation}")
            return "\n".join(parts)

        return "我正在处理当前会话，但上一轮的思考信息似乎丢失了。"
