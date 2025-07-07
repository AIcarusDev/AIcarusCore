# src/focus_chat_mode/chat_prompt_builder.py

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

    # --- ❤❤❤ 欲望喷射点 ①：改造这个方法的返回值类型签名！❤❤❤ ---
    async def build_prompts(
        self,
        session: "ChatSession",
        last_processed_timestamp: float,
        last_llm_decision: dict[str, Any] | None,
        is_first_turn: bool,
        last_think_from_core: str | None = None,
        last_mood_from_core: str | None = None,
        motivation_from_core: str | None = None,
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
        if self.session.core_logic and self.session.core_logic.prompt_builder:
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

        previous_thoughts_block_str = self._build_previous_thoughts_block(
            is_first_turn,
            was_last_turn_interrupted,
            last_llm_decision,
            session,
            interrupting_event_text,
            last_mood_from_core,
            last_think_from_core,
            motivation_from_core,
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
        logger.debug(f"[{self.session.conversation_id}] System Prompt: {system_prompt}...")  # 只打印前100个字符
        logger.debug(f"[{self.session.conversation_id}] User Prompt: {user_prompt}")
        return prompt_components

    def _build_previous_thoughts_block(
        self,
        is_first_turn: bool,
        was_interrupted: bool,
        last_decision: dict[str, Any] | None,
        session: "ChatSession",
        interrupt_text: str | None,
        core_mood: str | None,
        core_think: str | None,
        core_motivation: str | None,
    ) -> str:
        if was_interrupted:
            if session.messages_sent_this_turn == 0:
                if last_decision:
                    think = last_decision.get("think", "我被打断前正在想...")
                    mood = last_decision.get("mood", "平静")
                    return f'刚刚你的心情是："{mood}"。\n刚刚你的内心想法是："{think}"。\n但你还没来得及做出任何行动，就被新的消息“{interrupt_text or "某条新消息"}”吸引了注意。'
                return f"你正准备开始思考，但就被新的消息“{interrupt_text or '某条新消息'}”打断了。你需要先处理这个新情况。"
            else:
                if last_decision:
                    think = last_decision.get("think", "我被打断前正在想...")
                    mood = last_decision.get("mood", "平静")
                    motivation = last_decision.get("motivation", "")
                    parts = [
                        f'刚刚你的心情是："{mood}"。',
                        f'刚刚你的内心想法是："{think}"。',
                        f"出于这个想法，你决定发言，并计划发送 {session.messages_planned_this_turn} 条消息。",
                        f"原因是：{motivation}。" if motivation else "",
                        f"但是，在你发送了 {session.messages_sent_this_turn} 条消息后，新的消息“{interrupt_text or '某条新消息'}”让你感到意外，所以你停下了后续的发言。现在你需要基于这个新情况重新思考。",
                    ]
                    return "\n".join(p for p in parts if p)
                return "你在发送消息时被打断了，但上一轮的思考记录丢失了。请重新评估情况。"
        elif is_first_turn:
            mood_part = f'你刚才的心情是"{core_mood}"。\n' if core_mood else ""
            think_part = f'你刚才的想法是："{core_think}"。\n' if core_think else ""
            motivation_part = (
                f'你现在刚刚把注意力放到这个会话中，因为："{core_motivation}"。\n'
                if core_motivation
                else "你已进入专注模式，开始处理此会话。\n"
            )
            return f"{mood_part}{think_part}{motivation_part}"
        elif last_decision:
            think = last_decision.get("think", "")
            mood = last_decision.get("mood", "平静")
            reply_text = last_decision.get("reply_text")
            motivation = last_decision.get("motivation", "")
            reply_willing = last_decision.get("reply_willing", False)
            poke_target = last_decision.get("poke")

            action_desc = "暂时不发言"
            if reply_willing and isinstance(reply_text, list) and reply_text:
                valid_msgs = [msg for msg in reply_text if isinstance(msg, str) and msg.strip().lower() != "null"]
                if len(valid_msgs) == 1:
                    action_desc = f"发言（发言内容为：{valid_msgs[0]}）"
                elif len(valid_msgs) > 1:
                    msgs_str = "，".join(f'"{msg}"' for msg in valid_msgs)
                    action_desc = f"发言，并且发送了{len(valid_msgs)}条消息（内容依次为：{msgs_str}）"
            elif reply_willing:
                action_desc = "决定发言但未提供有效内容"
            elif poke_target:
                uid_map = getattr(session.cycler, "uid_map", {})
                poked_user = uid_map.get(str(poke_target), str(poke_target))
                action_desc = f"戳一戳 {poked_user}"

            parts = [f'刚刚你的心情是："{mood}"\n刚刚你的内心想法是："{think}"']
            if action_desc:
                parts.append(f"出于这个想法，你刚才做了：{action_desc}")
            if motivation:
                parts.append(f"因为：{motivation}")
            return "\n".join(parts)
        return "我正在处理当前会话，但上一轮的思考信息似乎丢失了。"
