# chat_prompt_builder.py

import os
from datetime import datetime
from typing import TYPE_CHECKING, Any

from src.action.action_handler import ActionHandler
from src.common.custom_logging.logging_config import get_logger
from src.common.focus_chat_history_builder.chat_history_formatter import format_chat_history_for_llm

# 导入你的顶层config对象
from src.config import config  # 假设你的顶层配置对象叫 config
from src.database.services.event_storage_service import EventStorageService

from . import prompt_templates  # 假设你的 prompt_templates 在同级目录

if TYPE_CHECKING:
    from .chat_session import ChatSession  # 假设 ChatSession 在同级目录

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
    ) -> tuple[str, str, str | None, dict[str, str], list[str], list[str]]:
        """
        构建专注聊天模式下给LLM的System Prompt和User Prompt。
        返回值是一个元组，包含了所有需要的东西：
        (system_prompt, user_prompt, last_valid_text_message, uid_to_pid_map, processed_event_ids, image_references)
        """
        # --- 步骤1：准备模板和一些通用信息 ---
        logger.debug(f"[{self.session.conversation_id}] 开始构建Prompt...")

        user_nick = ""  # 私聊时对方的昵称，后面会填充
        if self.session.conversation_type == "private":
            system_prompt_template = prompt_templates.PRIVATE_SYSTEM_PROMPT
            user_prompt_template = prompt_templates.PRIVATE_USER_PROMPT
        else:  # group
            system_prompt_template = prompt_templates.GROUP_SYSTEM_PROMPT
            user_prompt_template = prompt_templates.GROUP_USER_PROMPT

        current_time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        persona_config = config.persona
        bot_name_str = persona_config.bot_name or "AI"
        bot_description_str = f"\n{persona_config.description}" if persona_config.description else ""
        bot_profile_str = f"\n{persona_config.profile}" if persona_config.profile else ""

        no_action_guidance_str = ""
        if session.no_action_count >= 3:
            if self.session.conversation_type == "private":
                no_action_guidance_str = f"\n你已经决定连续不发言/没有互动 {session.no_action_count} 次了，观察一下目前与对方的话题是不是已经告一段落了，如果是，可以考虑暂时先不专注于与对方的聊天了。"
            else:
                no_action_guidance_str = f"\n你已经决定连续不发言/没有互动 {session.no_action_count} 次了，观察一下目前群内话题是不是已经告一段落了，如果是，可以考虑暂时先不专注于群聊的消息了。"
            logger.info(f"[{self.session.conversation_id}] 添加无互动提示, count: {session.no_action_count}")

        # --- 步骤2：调用新玩具，获取所有格式化好的聊天记录相关信息 ---
        logger.debug(f"[{self.session.conversation_id}] 调用通用聊天记录格式化工具...")
        bot_profile = await session.get_bot_profile()

        # 喊一声新玩具的名字，它就把所有脏活累活都干完了！
        (
            chat_history_log_block_str,
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

        # 更新会话的名称，这样下次就不用再费劲了
        if conversation_name_from_formatter and self.session.conversation_name != conversation_name_from_formatter:
            self.session.conversation_name = conversation_name_from_formatter
            logger.info(f"[{self.session.conversation_id}] 会话名称已更新为: '{self.session.conversation_name}'")
        logger.debug(
            f"[{self.session.conversation_id}] 通用格式化工具执行完毕，获取到 {len(processed_event_ids)} 个新事件ID。"
        )

        # --- 步骤3：使用新玩具返回的结果，准备剩下的Prompt零件 ---

        # 如果是私聊，我们还需要从返回的 user_map 里找到对方的昵称
        if self.session.conversation_type == "private":
            final_bot_id = str(bot_profile.get("user_id", self.bot_id))
            for p_id, user_data_val in user_map.items():
                if user_data_val.get("uid_str") == "U1" and p_id != final_bot_id:
                    user_nick = user_data_val.get("nick", "对方")
                    break

        # 构建会话信息和用户列表块
        conversation_info_block_str = f'- conversation_name: "{self.session.conversation_name or "未知会话"}"\n- conversation_type: "{self.session.conversation_type}"'

        user_list_lines = []
        sorted_user_platform_ids = sorted(user_map.keys(), key=lambda pid_sort: int(user_map[pid_sort]["uid_str"][1:]))
        for p_id_list in sorted_user_platform_ids:
            user_data_item = user_map[p_id_list]
            user_identity_suffix = "（你）" if user_data_item["uid_str"] == "U0" else ""
            if self.session.conversation_type == "private":
                user_line = f"{user_data_item['uid_str']}: {p_id_list}{user_identity_suffix} [nick:{user_data_item['nick']}, card:{user_data_item['card']}]"
            else:
                user_line = f"{user_data_item['uid_str']}: {p_id_list}{user_identity_suffix} [nick:{user_data_item['nick']}, card:{user_data_item['card']}, title:{user_data_item['title']}, perm:{user_data_item['perm']}]"
            user_list_lines.append(user_line)
        user_list_block_str = "\n".join(user_list_lines)

        # 构建上一轮思考的块 (这部分逻辑不变，因为它特定于专注聊天模式)
        previous_thoughts_block_str = ""
        if is_first_turn:
            mood_part = f'你刚才的心情是"{session.initial_core_mood}"。\n' if session.initial_core_mood else ""
            think_part = (
                f"你刚才的想法是：{last_think_from_core}\n\n现在你刚刚把注意力放到这个会话中；\n\n原因是：你对当前聊天内容有点兴趣\n"
                if last_think_from_core
                else "你已进入专注模式，开始处理此会话。\n"
            )
            previous_thoughts_block_str = (
                f"<previous_thoughts_and_actions>\n{mood_part}{think_part}</previous_thoughts_and_actions>"
            )
        elif last_llm_decision:
            think_content = last_llm_decision.get("think", "")
            mood_content = last_llm_decision.get("mood", "平静")
            reply_text = last_llm_decision.get("reply_text")
            motivation_content = last_llm_decision.get("motivation", "")
            reply_willing_flag = last_llm_decision.get("reply_willing", False)
            poke_target_id_val = last_llm_decision.get("poke")

            action_desc = "暂时不发言"
            if reply_willing_flag and reply_text:
                action_desc = f"发言（发言内容为：{reply_text}）"
            elif reply_willing_flag and not reply_text:
                action_desc = "决定发言但未提供内容"
            elif poke_target_id_val:
                poked_user_display = uid_str_to_platform_id_map.get(str(poke_target_id_val), str(poke_target_id_val))
                action_desc = f"戳一戳 {poked_user_display}"

            prev_parts = [
                f'<previous_thoughts_and_actions>\n刚刚你的心情是："{mood_content}"\n刚刚你的内心想法是："{think_content}"'
            ]
            if action_desc:
                prev_parts.append(f"出于这个想法，你刚才做了：{action_desc}")
            if motivation_content:
                prev_parts.append(f"因为：{motivation_content}")

            prev_parts.append("</previous_thoughts_and_actions>")
            previous_thoughts_block_str = "\n".join(prev_parts)
        else:
            previous_thoughts_block_str = "<previous_thoughts_and_actions>\n我正在处理当前会话，但上一轮的思考信息似乎丢失了。\n</previous_thoughts_and_actions>"

        # --- 步骤4：组装最终的Prompt ---
        final_bot_id = str(bot_profile.get("user_id", self.bot_id))
        final_bot_nickname = bot_profile.get("nickname", persona_config.bot_name or "bot")
        final_bot_card = bot_profile.get("card", final_bot_nickname)

        if self.session.conversation_type == "group":
            system_prompt = system_prompt_template.format(
                current_time=current_time_str,
                bot_name=bot_name_str,
                optional_description=bot_description_str,
                optional_profile=bot_profile_str,
                bot_id=final_bot_id,
                bot_nickname=final_bot_nickname,
                conversation_name=self.session.conversation_name or "未知群聊",
                bot_card=final_bot_card,
                no_action_guidance=no_action_guidance_str,
            )
        else:  # private
            system_prompt = system_prompt_template.format(
                current_time=current_time_str,
                bot_name=bot_name_str,
                bot_id=final_bot_id,
                optional_description=bot_description_str,
                optional_profile=bot_profile_str,
                no_action_guidance=no_action_guidance_str,
                user_nick=user_nick,
            )

        user_prompt = user_prompt_template.format(
            conversation_info_block=conversation_info_block_str,
            user_list_block=user_list_block_str,
            chat_history_log_block=chat_history_log_block_str,
            previous_thoughts_block=previous_thoughts_block_str,
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
        return (
            system_prompt,
            user_prompt,
            last_valid_text_message,
            uid_str_to_platform_id_map,
            processed_event_ids,
            image_references,
        )
