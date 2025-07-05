# src\common\context_formatters\chat_history_formatter.py

# --- 导入所有需要的工具 ---
import base64
import os
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from aicarus_protocols import ConversationInfo, Event, Seg, UserInfo, extract_text_from_content

from src.common.custom_logging.logging_config import get_logger
from src.config import config

if TYPE_CHECKING:
    from src.database.services.event_storage_service import EventStorageService

logger = get_logger(__name__)


# --- 这就是我们新的、可复用的函数！ ---
async def format_chat_history_for_llm(
    event_storage: "EventStorageService",
    conversation_id: str,
    bot_id: str,
    platform: str,
    bot_profile: dict,
    conversation_type: str,
    conversation_name: str | None,  # 会话名称也传进来
    last_processed_timestamp: float,
    is_first_turn: bool,
    # --- 新增一个参数，让它也能直接处理传入的事件列表 ---
    raw_events_from_caller: list[dict[str, Any]] | None = None,
    # 其他可能需要的参数...
) -> tuple[str, str, str, dict, dict, list[str], list[str], str | None, str | None]:
    """
    一个通用的聊天记录格式化工具，哼，真麻烦。
    它负责从数据库获取事件，处理用户映射，格式化聊天记录，还顺便处理了那些烦人的图片。

    Args:
        event_storage: 事件存储服务的实例。
        conversation_id: 目标会话的ID。
        bot_id: 机器人的ID。
        platform: 平台名称。
        bot_profile: 机器人在该会话的档案信息。
        conversation_type: 会话类型 ("group" 或 "private")。
        conversation_name: 会话名称。
        last_processed_timestamp: 上次处理到的时间戳，用于标记已读/未读。
        is_first_turn: 是否是当前专注会话的第一次思考。
        raw_events_from_caller: (可选) 直接提供事件列表，而不是从数据库获取。

    Returns:
        一个元组，包含了所有你需要的东西，自己看，别问我！
        (
        chat_history_log_block: str,
        user_map: dict,
        uid_str_to_platform_id_map: dict,
        processed_event_ids: list[str],
        image_references: list[str],
        conversation_name_str: str | None,
        last_valid_text_message: str | None
    )
    """
    # 确保临时目录存在
    temp_image_dir = config.runtime_environment.temp_file_directory
    os.makedirs(temp_image_dir, exist_ok=True)

    if raw_events_from_caller is not None:
        # 如果调用者直接提供了事件列表（比如总结服务），就用它
        event_dicts = raw_events_from_caller

    else:
        event_dicts = await event_storage.get_recent_chat_message_documents(
            conversation_id=conversation_id,
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
                # 确保 user_info_dict 是字典类型
                # 如果不是字典类型，可能是 None 或其他类型，这里我们用 None
                protocol_user_info = (
                    UserInfo.from_dict(user_info_dict) if user_info_dict and isinstance(user_info_dict, dict) else None
                )

                conv_info_dict = event_dict.get("conversation_info")
                # 确保 conv_info_dict 是字典类型
                # 如果不是字典类型，可能是 None 或其他类型，这里我们用 None
                protocol_conv_info = (
                    ConversationInfo.from_dict(conv_info_dict)
                    if conv_info_dict and isinstance(conv_info_dict, dict)
                    else None
                )

                motivation = event_dict.pop("motivation", None)  # 从字典中移除，避免重复传递
                event_obj = Event(
                    event_id=str(event_dict.get("event_id", event_dict.get("_key", str(uuid.uuid4())))),
                    event_type=str(event_dict.get("event_type", "unknown")),
                    time=float(event_dict.get("timestamp", event_dict.get("time", 0.0))),
                    bot_id=str(event_dict.get("bot_id", bot_id)),  # 使用已知的机器人ID
                    content=content_segs,
                    user_info=protocol_user_info,
                    conversation_info=protocol_conv_info,
                    raw_data=event_dict.get("raw_data") if isinstance(event_dict.get("raw_data"), dict) else None,
                )
                if motivation:
                    event_obj.motivation = motivation
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
    # ❤❤❤ 先用哥哥你插进来的名字作为默认值，如果哥哥没给，才用“未知会话”这个后备小玩具 ❤❤❤
    conversation_name_str = conversation_name or "未知会话"
    # conversation_type_str = self.conversation_type # 已在 __init__ 中获取

    # 用传入的 bot_profile 来初始化机器人自己的信息
    final_bot_id = str(bot_profile.get("user_id", bot_id))
    final_bot_nickname = bot_profile.get("nickname", config.persona.bot_name or "bot")
    final_bot_card = bot_profile.get("card", final_bot_nickname)

    platform_id_to_uid_str[final_bot_id] = "U0"
    user_map[final_bot_id] = {
        "uid_str": "U0",
        "nick": final_bot_nickname,
        "card": final_bot_card,
        "title": bot_profile.get("title", ""),
        "perm": bot_profile.get("role", "成员"),
    }

    # ❤❤❤ 然后再看看事件里有没有更新、更湿润的名字，有的话就换掉 ❤❤❤
    if raw_events:
        # 从最新的事件开始找，名字通常更准哦~
        for event in reversed(raw_events):
            if event.conversation_info and event.conversation_info.name:
                # 啊~ 找到了一个更新鲜的，那就用这个！
                conversation_name_str = event.conversation_info.name
                break  # 找到一个就够了，不用再深入了，好爽！

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
    user_nick = "对方"

    if conversation_type == "private":
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

    _conversation_info_block_str = (
        f'- conversation_name: "{conversation_name_str}"\n- conversation_type: "{conversation_type}"'
    )

    user_list_lines = []
    # 按 U0, U1, U2... 的顺序排序用户列表
    sorted_user_platform_ids = sorted(user_map.keys(), key=lambda pid_sort: int(user_map[pid_sort]["uid_str"][1:]))
    for p_id_list in sorted_user_platform_ids:
        user_data_item = user_map[p_id_list]
        user_identity_suffix = "（你）" if user_data_item["uid_str"] == "U0" else ""
        if conversation_type == "private":
            user_line = f"{user_data_item['uid_str']}: {p_id_list}{user_identity_suffix} [nick:{user_data_item['nick']}, card:{user_data_item['card']}]"
        else:  # group
            user_line = f"{user_data_item['uid_str']}: {p_id_list}{user_identity_suffix} [nick:{user_data_item['nick']}, card:{user_data_item['card']}, title:{user_data_item['title']}, perm:{user_data_item['perm']}]"
        user_list_lines.append(user_line)
    _user_list_block_str = "\n".join(user_list_lines)

    _conversation_info_block_str = (
        f'- conversation_name: "{conversation_name_str or "未知会话"}"\n- conversation_type: "{conversation_type}"'
    )

    # --- Step 5: Build chat history log ---
    chat_log_lines: list[str] = []
    image_references: list[str] = []
    unread_section_started = False
    current_last_processed_timestamp = last_processed_timestamp  # 从参数获取
    last_valid_text_message: str | None = None

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
                chat_log_lines.append(f"--- 以上消息是你已经思考过的内容，已读 (标记时间: {read_marker_time_str}) ---")
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
                    logger.debug(f"Skipping duplicate message for display, platform_msg_id: {current_platform_msg_id}")
                    continue  # 跳过此事件的处理，不加入chat_log_lines
                added_platform_message_ids_for_log.add(current_platform_msg_id)

            for seg in event_data_log.content:
                if seg.type == "reply":
                    quoted_message_id = seg.data.get("message_id", "unknown_id")
                    quoted_user_id = seg.data.get("user_id")
                    if quoted_user_id:
                        quoted_user_uid = platform_id_to_uid_str.get(quoted_user_id, f"未知用户({quoted_user_id[:4]})")
                        quote_display_str = f"引用/回复 {quoted_user_uid}(id:{quoted_message_id})"
                    else:
                        quote_display_str = f"引用/回复 (id:{quoted_message_id})"
                elif seg.type == "text":
                    main_content_parts.append(seg.data.get("text", ""))
                elif seg.type == "image":
                    # ❤❤❤ 就是这里，看清楚了，笨蛋！❤❤❤
                    # 我现在会检查那张小纸条了！
                    if seg.data.get("summary") == "sticker":
                        main_content_parts.append("[动画表情]")
                    else:
                        # 对于其他所有图片，不管是真图还是没 summary 的，都叫 [图片]
                        main_content_parts.append("[图片]")

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
                            temp_file_path = os.path.join(temp_image_dir, temp_file_name)

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
            text_only_from_content = extract_text_from_content(event_data_log.content)
            # 如果这条消息里真的有文字，就更新我们的变量
            if text_only_from_content:
                last_valid_text_message = text_only_from_content
                logger.trace(f"更新 last_valid_text_message 为: '{last_valid_text_message[:30]}...'")

            display_tag = main_content_type
            if quote_display_str:
                display_tag = f"{main_content_type}, {quote_display_str}"

            log_line = f"[{time_str}] {log_user_id_str} [{display_tag}]: {main_content_str} (id:{msg_id_for_display})"

            event_motivation = getattr(event_data_log, "motivation", None)
            if log_user_id_str == "U0" and event_motivation and event_motivation.strip():
                log_line += f"\n    - [MOTIVE]: {event_motivation}"

        elif event_data_log.event_type.startswith("notice."):
            main_content_type = "NOTICE"
            notice_data = event_data_log.content[0].data if event_data_log.content else {}
            # 从事件类型里把具体的通知类型抠出来，比如 'member_increase'
            notice_subtype = event_data_log.event_type.split('.')[-1]

            # 开始区分不同的通知类型，拼出人话
            if notice_subtype == "member_increase":
                operator_info = notice_data.get("operator_user_info", {})
                operator_id = operator_info.get("user_id") if operator_info else None
                operator_uid = platform_id_to_uid_str.get(operator_id, f"未知用户({str(operator_id)[:4]})") if operator_id else "系统"

                target_id = event_data_log.user_info.user_id if event_data_log.user_info else None
                target_uid = platform_id_to_uid_str.get(target_id, f"未知用户({str(target_id)[:4]})") if target_id else "一位新成员"

                if notice_data.get("join_type") == "approve":
                    main_content_parts.append(f"{target_uid} 加入了群聊。")
                else: # invite
                    main_content_parts.append(f"{operator_uid} 邀请 {target_uid} 加入了群聊。")

            elif notice_subtype == "member_decrease":
                operator_info = notice_data.get("operator_user_info", {})
                operator_id = operator_info.get("user_id") if operator_info else None
                operator_uid = platform_id_to_uid_str.get(operator_id, f"未知用户({str(operator_id)[:4]})") if operator_id else "系统"

                target_id = event_data_log.user_info.user_id if event_data_log.user_info else None
                target_uid = platform_id_to_uid_str.get(target_id, f"未知用户({str(target_id)[:4]})") if target_id else "一位成员"

                if notice_data.get("leave_type") == "kick":
                    main_content_parts.append(f"{operator_uid} 将 {target_uid} 移出了群聊。")
                else: # leave
                    main_content_parts.append(f"{target_uid} 退出了群聊。")

            elif notice_subtype == "member_ban":
                operator_info = notice_data.get("operator_user_info", {})
                operator_id = operator_info.get("user_id") if operator_info else None
                operator_uid = platform_id_to_uid_str.get(operator_id, "管理员") if operator_id else "管理员"

                target_info = notice_data.get("target_user_info", {})
                target_id = target_info.get("user_id") if target_info else None
                target_uid = platform_id_to_uid_str.get(target_id, "一位成员") if target_id else "一位成员"

                duration = notice_data.get("duration_seconds", 0)
                if duration > 0:
                    main_content_parts.append(f"{operator_uid} 将 {target_uid} 禁言了 {duration} 秒。")
                else:
                    main_content_parts.append(f"{operator_uid} 解除了 {target_uid} 的禁言。")

            elif notice_subtype == "message_recalled":
                operator_info = notice_data.get("operator_user_info", {})
                operator_id = operator_info.get("user_id") if operator_info else None
                operator_uid = platform_id_to_uid_str.get(operator_id, "一位用户") if operator_id else "一位用户"
                main_content_parts.append(f"{operator_uid} 撤回了一条消息。")

            elif notice_subtype == "user.poke":
                sender_info = notice_data.get("sender_user_info", {})
                sender_id = sender_info.get("user_id") if sender_info else None
                sender_uid = platform_id_to_uid_str.get(sender_id, "一位用户") if sender_id else "一位用户"

                target_info = notice_data.get("target_user_info", {})
                target_id = target_info.get("user_id") if target_info else None
                target_uid = platform_id_to_uid_str.get(target_id, "一位用户") if target_id else "一位用户"
                main_content_parts.append(f"{sender_uid} 戳了戳 {target_uid}。")

            else:
                # 对于其他不认识的通知，就随便糊弄一下
                main_content_parts.append(f"收到一条 {notice_subtype} 类型的平台通知。")

            main_content_str = "".join(main_content_parts).strip()
            log_line = f"[{time_str}] [{main_content_type}]: {main_content_str}"

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

    # 6. 拼接最终的字符串
    chat_history_log_block_str = "\n".join(chat_log_lines) or "当前没有聊天记录。"

    # 【改造点6】processed_event_ids 的逻辑
    processed_event_ids: list[str] = []
    if raw_events:
        for event_obj_processed in raw_events:
            if (
                event_obj_processed.event_type.startswith("message.")
                and event_obj_processed.time > last_processed_timestamp
            ):
                processed_event_ids.append(event_obj_processed.event_id)

    # 7. 准备返回值
    uid_str_to_platform_id_map = {uid: pid for pid, uid in platform_id_to_uid_str.items()}

    # 返回所有处理好的结果
    return (
        chat_history_log_block_str,
        _user_list_block_str,
        _conversation_info_block_str,
        user_map,
        uid_str_to_platform_id_map,
        processed_event_ids,
        image_references,
        conversation_name_str,  # 返回正确的群名
        last_valid_text_message,  # 返回我们找到的最后一条文本消息
    )
