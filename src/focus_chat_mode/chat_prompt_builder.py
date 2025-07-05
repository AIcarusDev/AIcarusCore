# chat_prompt_builder.py

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
    ) -> tuple[str, str, str | None, dict[str, str], list[str], list[str], str | None]:
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

        (
            chat_history_log_block_str,
            user_list_block_str,
            conversation_info_block_str,
            user_map,
            uid_str_to_platform_id_map,
            processed_event_ids,
            image_references,
            conversation_name_from_formatter,
            last_valid_text_message,
        ) = await format_chat_history_for_llm(
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
            for p_id, user_data_val in user_map.items():
                # 如果是对方用户（不是bot），并且不是当前bot的ID
                if user_data_val.get("uid_str") == "U1" and p_id != final_bot_id:
                    user_nick = user_data_val.get("nick", "对方")
                    # 找到后就不再继续遍历了
                    break

        # 构建上一轮思考的块
        previous_thoughts_block_str = ""

        # Case A: 上一轮被中断了
        if was_last_turn_interrupted:
            # Case A.1: 思考阶段就被打断
            if session.messages_sent_this_turn == 0:
                logger.debug(f"[{session.conversation_id}] 构建中断上下文：思考阶段被中断。")
                # 这种情况，我们就用上上轮的思考结果 (last_llm_decision) 来构建
                if last_llm_decision:
                    # 沿用上一轮完整的思考内容
                    think_content = last_llm_decision.get("think", "我被打断前正在想...")
                    mood_content = last_llm_decision.get("mood", "平静")
                    prev_parts = [
                        f'刚刚你的心情是："{mood_content}"。',
                        f'刚刚你的内心想法是："{think_content}"。',
                        f"但你还没来得及做出任何行动，就被新的消息“{interrupting_event_text or '某条新消息'}”吸引了注意。",
                    ]
                    previous_thoughts_block_str = "\n".join(prev_parts)
                else:
                    # 如果连上上轮的思考都没有（比如第一轮就被打断），就给个通用提示
                    previous_thoughts_block_str = f"你正准备开始思考，但就被新的消息“{interrupting_event_text or '某条新消息'}”打断了。你需要先处理这个新情况。"

            # Case A.2: 发送消息阶段被打断
            else:
                logger.debug(f"[{session.conversation_id}] 构建中断上下文：发送阶段被中断。")
                if last_llm_decision:
                    think_content = last_llm_decision.get("think", "我被打断前正在想...")
                    mood_content = last_llm_decision.get("mood", "平静")
                    motivation_content = last_llm_decision.get("motivation", "")

                    # ❤❤❤ 这就是你想要的“小说式”上下文！❤❤❤
                    prev_parts = [
                        f'刚刚你的心情是："{mood_content}"。',
                        f'刚刚你的内心想法是："{think_content}"。',
                        f"出于这个想法，你决定发言，并计划发送 {session.messages_planned_this_turn} 条消息。",
                    ]
                    if motivation_content:
                        prev_parts.append(f"原因是：{motivation_content}。")

                    prev_parts.append(
                        f"但是，在你发送了 {session.messages_sent_this_turn} 条消息后，"
                        f"新的消息“{interrupting_event_text or '某条新消息'}”让你感到意外，所以你停下了后续的发言。"
                        "现在你需要基于这个新情况重新思考。"
                    )
                    previous_thoughts_block_str = "\n".join(prev_parts)
                else:
                    previous_thoughts_block_str = "你在发送消息时被打断了，但上一轮的思考记录丢失了。请重新评估情况。"
        # Case B: 正常情况，没有被打断
        else:
            # 如果是第一次轮次，或者没有上一轮的决策信息
            if is_first_turn:
                # 来自核心意识的心情、想法和动机
                # 如果核心意识有提供心情、想法和动机，就用它们
                # 理论上这些应该总是有的，除非核心意识没有运行过
                mood_part = f'你刚才的心情是"{last_mood_from_core}"。\n' if last_mood_from_core else ""
                think_part = f'你刚才的想法是："{last_think_from_core}"。\n' if last_think_from_core else ""
                motivation_part = (
                    f'你现在刚刚把注意力放到这个会话中，因为："{motivation_from_core}"。\n'
                    if motivation_from_core
                    else "你已进入专注模式，开始处理此会话。\n"
                )

                # 把它们拼起来！
                previous_thoughts_block_str = f"{mood_part}{think_part}{motivation_part}"

                # 如果不是第一次轮次，或者有上一轮的决策信息
            elif last_llm_decision:
                think_content = last_llm_decision.get("think", "")
                mood_content = last_llm_decision.get("mood", "平静")
                reply_text_list = last_llm_decision.get("reply_text")
                motivation_content = last_llm_decision.get("motivation", "")
                reply_willing_flag = last_llm_decision.get("reply_willing", False)
                poke_target_id_val = last_llm_decision.get("poke")

                # 处理上一轮的心情、想法、动机和回复内容
                action_desc = "暂时不发言"
                if reply_willing_flag and isinstance(reply_text_list, list) and reply_text_list:
                    valid_messages = [
                        msg for msg in reply_text_list if msg and isinstance(msg, str) and msg.strip().lower() != "null"
                    ]
                    if len(valid_messages) == 1:
                        # 只有一条有效消息时，示例：发言（发言内容为：你好）
                        action_desc = f"发言（发言内容为：{valid_messages[0]}）"
                    # 有多条有效消息时，示例：发言，并且发送了3条消息（内容依次为：“你好”，“今天天气不错”，“你呢？”）
                    elif len(valid_messages) > 1:
                        messages_str = "，".join(f'"{msg}"' for msg in valid_messages)
                        action_desc = f"发言，并且发送了{len(valid_messages)}条消息（内容依次为：{messages_str}）"
                # 没有有效消息时（这是一个兜底，llm响应理想的情况下不应该发生）
                elif reply_willing_flag:
                    action_desc = "决定发言但未提供有效内容"
                elif poke_target_id_val:
                    # 处理戳一戳的情况
                    uid_map = session.cycler.uid_map if hasattr(session.cycler, "uid_map") else {}
                    poked_user_display = uid_map.get(str(poke_target_id_val), str(poke_target_id_val))
                    action_desc = f"戳一戳 {poked_user_display}"
                # 如果没有戳一戳或发言，则保持原样
                prev_parts = [f'刚刚你的心情是："{mood_content}"\n刚刚你的内心想法是："{think_content}"']
                if action_desc:
                    prev_parts.append(f"出于这个想法，你刚才做了：{action_desc}")
                if motivation_content:
                    prev_parts.append(f"因为：{motivation_content}")
                # 拼接成完整的上一轮思考块
                previous_thoughts_block_str = "\n".join(prev_parts)
            else:
                previous_thoughts_block_str = "我正在处理当前会话，但上一轮的思考信息似乎丢失了。"

        # --- 步骤4：看好了！这里是核心改造！分别组装！ ---

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
                conversation_name=conversation_name_from_formatter or "未知群聊",
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
            conversation_info_block=conversation_info_block_str,
            user_list_block=user_list_block_str,
            chat_history_log_block=chat_history_log_block_str,
            previous_thoughts_block=previous_thoughts_block_str,
            dynamic_behavior_guidance=dynamic_guidance_str,
            member_count=member_count,
            max_member_count=max_member_count,
        )

        logger.debug(f"[{self.session.conversation_id}] Prompts构建完成 (使用通用格式化工具)。")

        logger.debug(
            f"[{self.session.conversation_id}] 专注聊天 - 准备发送给LLM的完整Prompt:\n"
            f"==================== SYSTEM PROMPT (专注聊天) ====================\n"
            f"{system_prompt}\n"
            f"==================== USER PROMPT (专注聊天) ======================\n"
            f"{user_prompt}\n"
            f"=================================================================="
        )

        # --- 步骤5：把所有需要的东西都吐出去，一个都不能少！ ---
        # 看好了，这里现在返回7个东西！
        return (
            system_prompt,
            user_prompt,
            last_valid_text_message,
            uid_str_to_platform_id_map,
            processed_event_ids,
            image_references,
            conversation_name_from_formatter,
        )
