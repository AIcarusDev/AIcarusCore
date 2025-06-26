# chat_prompt_builder.py

import base64
import os
import uuid
from collections import OrderedDict
from datetime import datetime
from typing import TYPE_CHECKING, Any

from aicarus_protocols.common import extract_text_from_content
from aicarus_protocols.conversation_info import ConversationInfo
from aicarus_protocols.event import Event
from aicarus_protocols.seg import Seg
from aicarus_protocols.user_info import UserInfo

from src.action.action_handler import ActionHandler
from src.common.custom_logging.logging_config import get_logger

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

    async def build_prompts(
        self,
        session: "ChatSession",
        last_processed_timestamp: float,
        last_llm_decision: dict[str, Any] | None,
        sent_actions_context: OrderedDict[str, dict[str, Any]],  # 虽然未使用，但保持签名一致
        is_first_turn: bool,
        last_think_from_core: str | None = None,
    ) -> tuple[str, str, dict[str, str], list[str], list[str]]:
        # --- Step 1: Decide which templates to use ---
        user_nick = ""  # 私聊时对方的昵称
        if self.conversation_type == "private":
            system_prompt_template = prompt_templates.PRIVATE_SYSTEM_PROMPT
            user_prompt_template = prompt_templates.PRIVATE_USER_PROMPT
        else:  # group
            system_prompt_template = prompt_templates.GROUP_SYSTEM_PROMPT
            user_prompt_template = prompt_templates.GROUP_USER_PROMPT

        # --- Step 2: Prepare common data ---
        current_time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        persona_config = config.persona
        bot_name_str = persona_config.bot_name or "AI"
        bot_description_str = f"\n{persona_config.description}" if persona_config.description else ""
        bot_profile_str = f"\n{persona_config.profile}" if persona_config.profile else ""

        no_action_guidance_str = ""
        if session.no_action_count >= 3:
            if self.conversation_type == "private":
                no_action_guidance_str = f"\n你已经决定连续不发言/没有互动 {session.no_action_count} 次了，观察一下目前与对方的话题是不是已经告一段落了，如果是，可以考虑暂时先不专注于与对方的聊天了。"
            else:
                no_action_guidance_str = f"\n你已经决定连续不发言/没有互动 {session.no_action_count} 次了，观察一下目前群内话题是不是已经告一段落了，如果是，可以考虑暂时先不专注于群聊的消息了。"
            logger.info(f"[{self.conversation_id}] 添加无互动提示, count: {session.no_action_count}")

        # --- Step 3: Fetch and process events ---
        event_dicts = await self.event_storage.get_recent_chat_message_documents(
            conversation_id=self.conversation_id,
            limit=50,  # 你可以按需调整这个限制
            fetch_all_event_types=True,
        )

        raw_events: list[Event] = []
        if event_dicts:
            for event_dict in event_dicts:
                try:
                    content_segs_data = event_dict.get("content", [])
                    content_segs = [
                        Seg(type=s_data.get("type", "unknown"), data=s_data.get("data", {}))
                        for s_data in content_segs_data
                        if isinstance(s_data, dict)
                    ]
                    user_info_dict = event_dict.get("user_info")
                    protocol_user_info = (
                        UserInfo(**user_info_dict) if user_info_dict and isinstance(user_info_dict, dict) else None
                    )
                    conv_info_dict = event_dict.get("conversation_info")
                    protocol_conv_info = (
                        ConversationInfo(**conv_info_dict)
                        if conv_info_dict and isinstance(conv_info_dict, dict)
                        else None
                    )
                    motivation = event_dict.pop("motivation", None)  # 从字典中移除，避免重复传递
                    event_obj = Event(
                        event_id=str(event_dict.get("event_id", event_dict.get("_key", str(uuid.uuid4())))),
                        event_type=str(event_dict.get("event_type", "unknown")),
                        time=float(event_dict.get("timestamp", event_dict.get("time", 0.0))),
                        platform=str(event_dict.get("platform", self.platform)),  # 使用已知的平台
                        bot_id=str(event_dict.get("bot_id", self.bot_id)),  # 使用已知的机器人ID
                        content=content_segs,
                        user_info=protocol_user_info,
                        conversation_info=protocol_conv_info,
                        raw_data=event_dict.get("raw_data") if isinstance(event_dict.get("raw_data"), dict) else None,
                    )
                    if motivation:
                        event_obj.motivation = motivation  # 将 motivation 赋值给 Event 对象
                    raw_events.append(event_obj)
                except Exception as e_conv:
                    logger.bind(event_dict=event_dict).error(
                        f"将数据库事件字典转换为Event对象时出错: {e_conv}", exc_info=True
                    )

        # --- Deduplicate raw_events (based on your existing logic) ---
        if raw_events:
            unique_events_dict: dict[str, Event] = {}
            for event_obj in sorted(raw_events, key=lambda e: e.time, reverse=True):  # 从新到旧处理
                dedup_key: str | None = None
                is_message_event = event_obj.event_type.startswith("message.")
                if is_message_event:
                    platform_msg_id = event_obj.get_message_id()
                    if platform_msg_id:
                        dedup_key = f"msg_{platform_msg_id}"
                if not dedup_key:  # 对于非消息事件或无平台ID的消息事件，用核心事件ID去重
                    dedup_key = f"core_{event_obj.event_id}"

                if dedup_key not in unique_events_dict:
                    unique_events_dict[dedup_key] = event_obj
            raw_events = sorted(unique_events_dict.values(), key=lambda e: e.time)  # 按时间顺序排好

        # --- Step 4: Prepare user map and conversation info ---
        user_map: dict[str, dict[str, Any]] = {}  # platform_id -> user_data_dict
        platform_id_to_uid_str: dict[str, str] = {}  # platform_id -> "U0", "U1", ...
        uid_counter = 0
        conversation_name_str = "未知会话"
        # conversation_type_str = self.conversation_type # 已在 __init__ 中获取

        bot_profile = await session.get_bot_profile()  # 动态获取机器人信息
        final_bot_id = str(bot_profile.get("user_id", self.bot_id))
        final_bot_nickname = bot_profile.get("nickname", persona_config.bot_name or "bot")
        final_bot_card = bot_profile.get("card", final_bot_nickname)

        platform_id_to_uid_str[final_bot_id] = "U0"
        user_map[final_bot_id] = {
            "uid_str": "U0",
            "nick": final_bot_nickname,
            "card": final_bot_card,
            "title": bot_profile.get("title", "") if bot_profile else getattr(persona_config, "title", None) or "",
            "perm": bot_profile.get("role", "成员") if bot_profile else "成员",
        }

        # 更新 conversation_name 和 session
        if raw_events and raw_events[0].conversation_info:  # 取最早的事件（排序后）或最新的（如果之前是反向排序）
            conv_info = raw_events[0].conversation_info  # 假设raw_events已按时间正序排列
            # conversation_type_str = conv_info.type # 已有 self.conversation_type
            if conv_info.name:
                conversation_name_str = conv_info.name
        self.session.conversation_name = conversation_name_str  # 更新会话对象的名称

        for event_data in raw_events:
            if event_data.user_info and event_data.user_info.user_id:
                p_user_id = event_data.user_info.user_id
                if p_user_id not in platform_id_to_uid_str:
                    uid_counter += 1
                    uid_str = f"U{uid_counter}"
                    platform_id_to_uid_str[p_user_id] = uid_str
                    user_map[p_user_id] = {
                        "uid_str": uid_str,
                        "nick": event_data.user_info.user_nickname or f"用户{p_user_id[:4]}",
                        "card": event_data.user_info.user_cardname
                        or (event_data.user_info.user_nickname or f"用户{p_user_id[:4]}"),
                        "title": event_data.user_info.user_titlename or "",
                        "perm": event_data.user_info.permission_level or "成员",
                    }

        if self.conversation_type == "private":
            # 尝试找到 "U1" 作为对方
            for p_id, user_data_val in user_map.items():
                if user_data_val.get("uid_str") == "U1" and p_id != final_bot_id:
                    user_nick = user_data_val.get("nick", "对方")
                    break
            if not user_nick:  # 如果没有U1，或者U1是机器人自己（理论上不应发生）
                # 找第一个非U0的用户
                for _p_id, user_data_val in user_map.items():
                    if user_data_val.get("uid_str") != "U0":
                        user_nick = user_data_val.get("nick", "对方")
                        break
                if not user_nick:
                    user_nick = "对方"  # 最终后备

        conversation_info_block_str = (
            f'- conversation_name: "{conversation_name_str}"\n- conversation_type: "{self.conversation_type}"'
        )

        user_list_lines = []
        # 按 U0, U1, U2... 的顺序排序用户列表
        sorted_user_platform_ids = sorted(user_map.keys(), key=lambda pid_sort: int(user_map[pid_sort]["uid_str"][1:]))
        for p_id_list in sorted_user_platform_ids:
            user_data_item = user_map[p_id_list]
            user_identity_suffix = "（你）" if user_data_item["uid_str"] == "U0" else ""
            if self.conversation_type == "private":
                user_line = f"{user_data_item['uid_str']}: {p_id_list}{user_identity_suffix} [nick:{user_data_item['nick']}, card:{user_data_item['card']}]"
            else:  # group
                user_line = f"{user_data_item['uid_str']}: {p_id_list}{user_identity_suffix} [nick:{user_data_item['nick']}, card:{user_data_item['card']}, title:{user_data_item['title']}, perm:{user_data_item['perm']}]"
            user_list_lines.append(user_line)
        user_list_block_str = "\n".join(user_list_lines)

        # --- Step 5: Build chat history log ---
        chat_log_lines: list[str] = []
        image_references: list[str] = []
        unread_section_started = False
        current_last_processed_timestamp = last_processed_timestamp  # 从参数获取

        # 用于消息显示去重的集合，确保每次调用 build_prompts 时都是新的
        added_platform_message_ids_for_log: set[str] = set()

        # raw_events 此时应已按时间正序排列
        for event_data_log in raw_events:
            log_line = ""  # 确保每次循环开始时log_line是空的
            msg_id_for_display = event_data_log.get_message_id() or event_data_log.event_id
            quote_display_str = ""
            main_content_parts: list[str] = []
            main_content_type = "MSG"

            if (
                not is_first_turn  # 非首次运行时
                and event_data_log.time > current_last_processed_timestamp  # 且事件时间晚于上次处理时间
                and not unread_section_started  # 且未读标记还未开始
            ):
                if chat_log_lines:  # 如果前面有已读内容
                    read_marker_time_obj = datetime.fromtimestamp(current_last_processed_timestamp / 1000.0)
                    read_marker_time_str = read_marker_time_obj.strftime("%H:%M:%S")
                    chat_log_lines.append(
                        f"--- 以上消息是你已经思考过的内容，已读 (标记时间: {read_marker_time_str}) ---"
                    )
                chat_log_lines.append("--- 请关注以下未读的新消息---")
                unread_section_started = True

            dt_obj = datetime.fromtimestamp(event_data_log.time / 1000.0)
            time_str = dt_obj.strftime("%H:%M:%S")

            log_user_id_str = "SYS"  # 默认为系统消息
            if event_data_log.user_info and event_data_log.user_info.user_id:
                event_sender_platform_id = event_data_log.user_info.user_id
                log_user_id_str = platform_id_to_uid_str.get(
                    event_sender_platform_id, f"UnknownUser({event_sender_platform_id[:4]})"
                )

            is_robot_message_to_display_as_msg = (
                log_user_id_str == "U0" and event_data_log.event_type.startswith("message.")
            ) or (log_user_id_str == "U0" and event_data_log.event_type == "action.message.send")

            # 消息事件或机器人代发的消息事件
            if event_data_log.event_type.startswith("message.") or is_robot_message_to_display_as_msg:
                current_platform_msg_id = event_data_log.get_message_id()
                if event_data_log.event_type.startswith("message.") and current_platform_msg_id:
                    if current_platform_msg_id in added_platform_message_ids_for_log:
                        logger.debug(
                            f"Skipping duplicate message for display, platform_msg_id: {current_platform_msg_id}"
                        )
                        continue  # 跳过此事件的处理，不加入chat_log_lines
                    added_platform_message_ids_for_log.add(current_platform_msg_id)

                for seg in event_data_log.content:
                    if seg.type == "reply":
                        quoted_message_id = seg.data.get("message_id", "unknown_id")
                        quoted_user_id = seg.data.get("user_id")
                        if quoted_user_id:
                            quoted_user_uid = platform_id_to_uid_str.get(
                                quoted_user_id, f"未知用户({quoted_user_id[:4]})"
                            )
                            quote_display_str = f"引用/回复 {quoted_user_uid}(id:{quoted_message_id})"
                        else:
                            quote_display_str = f"引用/回复 (id:{quoted_message_id})"
                    elif seg.type == "text":
                        main_content_parts.append(seg.data.get("text", ""))
                    elif seg.type == "image":
                        placeholder = config.llm_client_settings.image_placeholder_tag
                        main_content_parts.append(f" {placeholder} ")

                        base64_data = seg.data.get("base64")
                        image_url_from_seg = seg.data.get("url")

                        if base64_data:
                            try:
                                image_bytes = base64.b64decode(base64_data)
                                file_id = seg.data.get("file_id", "unknown.tmp")
                                original_extension = file_id.split(".")[-1].lower()
                                if (
                                    not original_extension
                                    or len(original_extension) > 5
                                    or not original_extension.isalnum()
                                ):
                                    original_extension = "jpg"  # 更安全的默认

                                temp_file_name = f"{uuid.uuid4().hex}.{original_extension}"
                                temp_file_path = os.path.join(self._temp_image_dir, temp_file_name)

                                with open(temp_file_path, "wb") as tmp_f:
                                    tmp_f.write(image_bytes)

                                image_references.append(temp_file_path)
                                logger.info(f"图片 (ext: {original_extension}) base64已存至临时文件: {temp_file_path}")
                            except Exception as e_temp_save:
                                logger.error(f"保存base64到临时文件时出错: {e_temp_save}", exc_info=True)
                                if image_url_from_seg:
                                    logger.warning(f"临时文件保存失败，回退使用图片段中URL: {image_url_from_seg}")
                                    image_references.append(image_url_from_seg)
                                else:
                                    logger.error(f"事件 {event_data_log.event_id} 图片无法处理。")
                        elif image_url_from_seg:
                            image_references.append(image_url_from_seg)
                        else:
                            logger.error(f"事件 {event_data_log.event_id} 图片无base64也无url！")
                    elif seg.type == "at":
                        at_user_id = seg.data.get("user_id")
                        at_display_name = seg.data.get("display_name")
                        if at_user_id and at_user_id in platform_id_to_uid_str:
                            at_display_name = platform_id_to_uid_str[at_user_id]
                        elif not at_display_name and at_user_id:
                            at_display_name = f"@{at_user_id}"
                        elif not at_display_name:
                            at_display_name = "@未知用户"
                        main_content_parts.append(f"@{at_display_name} ")
                    elif seg.type == "face":
                        face_id = seg.data.get("id", "未知表情")
                        main_content_parts.append(f"[表情:{face_id}]")
                    elif seg.type == "file":
                        main_content_type = "FILE"
                        file_name = seg.data.get("name", "未知文件")
                        file_size = seg.data.get("size", 0)
                        main_content_parts.append(f"[FILE:{file_name} ({file_size} bytes)]")

                main_content_str = "".join(main_content_parts).strip()
                display_tag = main_content_type
                if quote_display_str:
                    display_tag = f"{main_content_type}, {quote_display_str}"

                log_line = (
                    f"[{time_str}] {log_user_id_str} [{display_tag}]: {main_content_str} (id:{msg_id_for_display})"
                )

                event_motivation = getattr(event_data_log, "motivation", None)
                if log_user_id_str == "U0" and event_motivation and event_motivation.strip():
                    log_line += f"\n    - [MOTIVE]: {event_motivation}"

            elif event_data_log.event_type == "notice.group.increase":
                op_id = event_data_log.content[0].data.get("operator_id") if event_data_log.content else None
                tar_id = event_data_log.content[0].data.get("target_id") if event_data_log.content else None
                op_uid = platform_id_to_uid_str.get(op_id, op_id or "UnknownOperator") if op_id else "UnknownOperator"
                tar_uid = platform_id_to_uid_str.get(tar_id, tar_id or "UnknownTarget") if tar_id else "UnknownTarget"
                log_line = f"[{time_str}] [SYS]: {op_uid}邀请{tar_uid}加入了群聊。"
            elif event_data_log.event_type == "internal.focus_chat_mode.thought_log":
                motivation_text = extract_text_from_content(event_data_log.content)
                log_line = f"[{time_str}] {log_user_id_str} [MOTIVE]: {motivation_text}"  # log_user_id_str 可能是 U0
            else:  # 其他类型的事件
                content_preview = extract_text_from_content(event_data_log.content)
                event_type_display = event_data_log.event_type.split(".")[-1].upper()
                log_line = f"[{time_str}] {log_user_id_str} [{event_type_display}]: {content_preview[:30]}{'...' if len(content_preview) > 30 else ''} (id:{event_data_log.event_id})"

            if log_line:  # 只有当 log_line 被赋值后才添加
                chat_log_lines.append(log_line)

        # 如果所有消息都已读，但未读标记未触发 (比如新消息时间戳 <= last_processed_timestamp)
        if not is_first_turn and not unread_section_started and chat_log_lines:
            marker_ts_for_all_read = raw_events[-1].time if raw_events else current_last_processed_timestamp
            read_marker_time_obj = datetime.fromtimestamp(marker_ts_for_all_read / 1000.0)
            read_marker_time_str = read_marker_time_obj.strftime("%H:%M:%S")
            chat_log_lines.append(f"--- 以上消息是你已经思考过的内容，已读 (标记时间: {read_marker_time_str}) ---")

        chat_history_log_block_str = "\n".join(chat_log_lines)
        if not chat_history_log_block_str:
            chat_history_log_block_str = "当前没有聊天记录。"

        # --- Step 6: Build previous thoughts block ---
        previous_thoughts_block_str = ""
        if is_first_turn:
            mood_part = ""
            if session.initial_core_mood:
                mood_part = f'你刚才的心情是"{session.initial_core_mood}"。\n'
            think_part = ""
            if last_think_from_core:  # 这是从核心同步过来的初始想法
                think_part = f"你刚才的想法是：{last_think_from_core}\n\n现在你刚刚把注意力放到这个群聊中；\n\n原因是：你对当前聊天内容有点兴趣\n"
            else:
                think_part = "你已进入专注模式，开始处理此会话。\n"
            previous_thoughts_block_str = (
                f"<previous_thoughts_and_actions>\n{mood_part}{think_part}</previous_thoughts_and_actions>"
            )
        elif last_llm_decision:  # 如果不是第一次，使用上次LLM的决策
            think_content = last_llm_decision.get("think", "")
            mood_content = last_llm_decision.get("mood", "平静")
            reply_text = last_llm_decision.get("reply_text")  # 假设这是上次LLM决定回复的内容
            motivation_content = last_llm_decision.get("motivation", "")
            reply_willing_flag = last_llm_decision.get("reply_willing", False)
            poke_target_id_val = last_llm_decision.get("poke")  # 假设这是上次LLM决定戳的人的platform_id

            action_desc = ""
            if reply_willing_flag and reply_text:
                action_desc = f"发言（发言内容为：{reply_text}）"
            elif reply_willing_flag and not reply_text:  # 决定要说，但没具体内容（可能只是思考要说）
                action_desc = "决定发言但未提供内容"  # 或者可以理解为“准备发言”
            else:  # 不发言
                if poke_target_id_val:
                    poked_user_display = platform_id_to_uid_str.get(poke_target_id_val, poke_target_id_val)
                    action_desc = f"戳一戳 {poked_user_display}"
                else:
                    action_desc = "暂时不发言"

            prev_parts = [
                f'<previous_thoughts_and_actions>\n刚刚你的心情是："{mood_content}"\n刚刚你的内心想法是："{think_content}"'
            ]
            if action_desc:  # 只有当有明确的action描述时才加入
                prev_parts.append(f"出于这个想法，你刚才做了：{action_desc}")

            # 动机的显示逻辑
            if motivation_content and (
                action_desc == "暂时不发言"
                or action_desc.startswith("戳一戳")
                or not motivation_content.startswith(action_desc)
                or (not reply_willing_flag and not poke_target_id_val)
            ):
                prev_parts.append(f"因为：{motivation_content}")

            prev_parts.append("</previous_thoughts_and_actions>")
            previous_thoughts_block_str = "\n".join(prev_parts)
        elif not last_llm_decision:  # 不是首次，但没有上次决策信息
            previous_thoughts_block_str = "<previous_thoughts_and_actions>\n我正在处理当前会话，但上一轮的思考信息似乎丢失了。\n</previous_thoughts_and_actions>"

        # --- Step 7: Assemble final prompts ---
        if self.conversation_type == "group":
            system_prompt = system_prompt_template.format(
                current_time=current_time_str,
                bot_name=bot_name_str,
                optional_description=bot_description_str,
                optional_profile=bot_profile_str,
                bot_id=final_bot_id,
                bot_nickname=final_bot_nickname,
                conversation_name=conversation_name_str,
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
                user_nick=user_nick,  # 私聊时对方的昵称
            )

        user_prompt = user_prompt_template.format(
            conversation_info_block=conversation_info_block_str,
            user_list_block=user_list_block_str,
            chat_history_log_block=chat_history_log_block_str,
            previous_thoughts_block=previous_thoughts_block_str,
        )

        # --- Step 8: Prepare return values ---
        uid_str_to_platform_id_map: dict[str, str] = {
            uid_str_val: p_id_val for p_id_val, uid_str_val in platform_id_to_uid_str.items()
        }

        processed_event_ids: list[str] = []
        if raw_events:  # 确保 raw_events 不是空的
            for event_obj_processed in raw_events:  # raw_events 已经是按时间排序的
                # 我们只关心在 last_processed_timestamp 之后收到的需要标记为已读的消息事件
                if (
                    event_obj_processed.event_type.startswith("message.")
                    and event_obj_processed.time > last_processed_timestamp
                ):
                    processed_event_ids.append(event_obj_processed.event_id)

        logger.debug(f"[{self.conversation_id}] Prompts 构建完成. 图片引用数量: {len(image_references)}")
        return system_prompt, user_prompt, uid_str_to_platform_id_map, processed_event_ids, image_references
