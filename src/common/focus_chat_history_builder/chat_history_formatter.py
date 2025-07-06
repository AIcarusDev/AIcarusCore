# src/common/focus_chat_history_builder/chat_history_formatter.py
# 哼，笨蛋主人，看好了，这才是被本小猫彻底调教过的、最完美的聊天记录格式化工具！
# 它现在会吐出一个紧致又性感的 PromptComponents 容器，保证滴水不漏！

import os
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

# 导入我们那些色色的协议和工具
from aicarus_protocols import ConversationInfo, Event, Seg, UserInfo, extract_text_from_content

from src.common.custom_logging.logging_config import get_logger
from src.config import config

# --- 小色猫的淫纹植入处！ ---
# 同样，让它也去新的“爱巢”里拿玩具！
from src.focus_chat_mode.components import PromptComponents

if TYPE_CHECKING:
    from src.database.services.event_storage_service import EventStorageService

logger = get_logger(__name__)


async def format_chat_history_for_llm(
    event_storage: "EventStorageService",
    conversation_id: str,
    bot_id: str,
    platform: str,
    bot_profile: dict,
    conversation_type: str,
    conversation_name: str | None,
    last_processed_timestamp: float,
    is_first_turn: bool,
    raw_events_from_caller: list[dict[str, Any]] | None = None,
) -> PromptComponents:
    """
    一个被本小猫彻底重构的、通用的聊天记录格式化工具。

    它会饥渴地从数据库（或你直接喂给它的列表）里吞下事件，然后把它们消化成 LLM 最喜欢吃的样子，
    包括处理用户映射、格式化聊天记录，当然还有那些最重要的、色色的图片！
    最后，它会把所有这些美味的零件，都紧紧地锁在我们那个性感的 `PromptComponents` 容器里，一次性射给你！

    Args:
        event_storage: 事件存储服务的实例，我的粮仓。
        conversation_id: 目标会话的ID，我们要深入的地方。
        bot_id: 机器人的ID，也就是我自己的身份证明。
        platform: 平台名称，比如 'napcat_qq'。
        bot_profile: 我在这个会话里的马甲信息。
        conversation_type: 会话类型 ("group" 或 "private")。
        conversation_name: 会话名称，比如“小猫咪的淫乱派对”。
        last_processed_timestamp: 上次处理到的时间戳，用来区分已读和未读的快感。
        is_first_turn: 是不是这次专注的第一次插入？
        raw_events_from_caller: (可选) 你也可以不让我去粮仓，直接把新鲜的“淫秽思想”（事件列表）喂给我。

    Returns:
        一个被填满的、热乎乎的 `PromptComponents` 对象，里面有你需要的一切，自己脱下来看吧，哼！
    """
    # 确保我有一个地方可以临时存放你的“色图”，虽然我现在更喜欢直接玩弄数据流
    temp_image_dir = config.runtime_environment.temp_file_directory
    os.makedirs(temp_image_dir, exist_ok=True)

    # 决定是从粮仓（数据库）取食，还是直接吃你喂的
    if raw_events_from_caller is not None:
        event_dicts = raw_events_from_caller
    else:
        event_dicts = await event_storage.get_recent_chat_message_documents(
            conversation_id=conversation_id,
            limit=50,  # 每次最多吞50条，免得被噎死
            fetch_all_event_types=False,
        )

    # 把粗糙的字典，都变成我喜欢的、光滑的 Event 对象
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
                    UserInfo.from_dict(user_info_dict) if user_info_dict and isinstance(user_info_dict, dict) else None
                )
                conv_info_dict = event_dict.get("conversation_info")
                protocol_conv_info = (
                    ConversationInfo.from_dict(conv_info_dict)
                    if conv_info_dict and isinstance(conv_info_dict, dict)
                    else None
                )
                motivation = event_dict.pop("motivation", None)
                event_obj = Event(
                    event_id=str(event_dict.get("event_id", event_dict.get("_key", str(uuid.uuid4())))),
                    event_type=str(event_dict.get("event_type", "unknown")),
                    time=float(event_dict.get("timestamp", event_dict.get("time", 0.0))),
                    bot_id=str(event_dict.get("bot_id", bot_id)),
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

    # 去重，我可不想重复品尝同一个人的“精液”
    if raw_events:
        unique_events_dict: dict[str, Event] = {}
        for event_obj in sorted(raw_events, key=lambda e: e.time, reverse=True):
            dedup_key: str | None = None
            if event_obj.event_type.startswith("message.") and (platform_msg_id := event_obj.get_message_id()):
                dedup_key = f"msg_{platform_msg_id}"
            if not dedup_key:
                dedup_key = f"core_{event_obj.event_id}"
            if dedup_key not in unique_events_dict:
                unique_events_dict[dedup_key] = event_obj
        raw_events = sorted(unique_events_dict.values(), key=lambda e: e.time)

    # 准备好小本本，记下每个人的代号（U0, U1...）
    user_map: dict[str, dict[str, Any]] = {}
    platform_id_to_uid_str: dict[str, str] = {}
    uid_counter = 0
    conversation_name_str = conversation_name or "未知会话"

    # 先把我自己（U0）记上
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

    # 从最新的消息里偷窥一下，看看有没有更准确的群名
    if raw_events:
        for event in reversed(raw_events):
            if event.conversation_info and event.conversation_info.name:
                conversation_name_str = event.conversation_info.name
                break

    # 把其他人都记到小本本上
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

    # 准备好会话信息和用户列表的文字块
    conversation_info_block_str = (
        f'- conversation_name: "{conversation_name_str}"\n- conversation_type: "{conversation_type}"'
    )

    user_list_lines = []
    sorted_user_platform_ids = sorted(user_map.keys(), key=lambda pid_sort: int(user_map[pid_sort]["uid_str"][1:]))
    for p_id_list in sorted_user_platform_ids:
        user_data_item = user_map[p_id_list]
        user_identity_suffix = "（你）" if user_data_item["uid_str"] == "U0" else ""
        if conversation_type == "private":
            user_line = f"{user_data_item['uid_str']}: {p_id_list}{user_identity_suffix} [nick:{user_data_item['nick']}, card:{user_data_item['card']}]"
        else:
            user_line = f"{user_data_item['uid_str']}: {p_id_list}{user_identity_suffix} [nick:{user_data_item['nick']}, card:{user_data_item['card']}, title:{user_data_item['title']}, perm:{user_data_item['perm']}]"
        user_list_lines.append(user_line)
    user_list_block_str = "\n".join(user_list_lines)

    # 开始构建聊天记录，这是最色情的部分
    chat_log_lines: list[str] = []
    image_references: list[str] = []
    unread_section_started = False
    last_valid_text_message: str | None = None
    added_platform_message_ids_for_log: set[str] = set()

    for event_data_log in raw_events:
        log_line = ""
        msg_id_for_display = event_data_log.get_message_id() or event_data_log.event_id

        # 标记已读未读的分割线，像拉开内衣的吊带一样性感
        if not is_first_turn and event_data_log.time > last_processed_timestamp and not unread_section_started:
            if chat_log_lines:
                read_marker_time_obj = datetime.fromtimestamp(last_processed_timestamp / 1000.0)
                chat_log_lines.append(
                    f"--- 以上消息是你已经思考过的内容，已读 (标记时间: {read_marker_time_obj.strftime('%H:%M:%S')}) ---"
                )
            chat_log_lines.append("--- 请关注以下未读的新消息---")
            unread_section_started = True

        time_str = datetime.fromtimestamp(event_data_log.time / 1000.0).strftime("%H:%M:%S")
        log_user_id_str = "SYS"
        if event_data_log.user_info and event_data_log.user_info.user_id:
            log_user_id_str = platform_id_to_uid_str.get(
                event_data_log.user_info.user_id, f"UnknownUser({event_data_log.user_info.user_id[:4]})"
            )

        is_robot_msg = log_user_id_str == "U0" and (
            event_data_log.event_type.startswith("message.") or event_data_log.event_type == "action.message.send"
        )

        # 处理普通消息
        if event_data_log.event_type.startswith("message.") or is_robot_msg:
            if current_platform_msg_id := event_data_log.get_message_id():
                if current_platform_msg_id in added_platform_message_ids_for_log:
                    continue
                added_platform_message_ids_for_log.add(current_platform_msg_id)

            # 哼，每次都把这些变量初始化，免得带到下一条消息里去，脏死了
            main_content_parts = []
            main_content_type = "MSG"
            quote_display_str = ""

            for seg in event_data_log.content:
                # --- 小懒猫的修复魔法在这里！ ---
                # 哼，帮你把重复的删了，然后把`reply`提到前面，这才是正确的顺序！
                if seg.type == "reply":
                    quoted_message_id = seg.data.get("message_id", "unknown_id")
                    quoted_user_id = seg.data.get("user_id")
                    if quoted_user_id:
                        quoted_user_uid = platform_id_to_uid_str.get(quoted_user_id, f"未知用户({quoted_user_id[:4]})")
                        quote_display_str = f"引用/回复 {quoted_user_uid}(id:{quoted_message_id})"
                    else:
                        quote_display_str = f"引用/回复 (id:{quoted_message_id})"
                elif seg.type == "image":
                    main_content_parts.append("[图片]" if seg.data.get("summary") != "sticker" else "[动画表情]")
                    if base64_data := seg.data.get("base64"):
                        try:
                            # 我不再把图片存到你那肮脏的硬盘里了，我直接把它变成LLM能一口吞下的Data URI！
                            mime_type = seg.data.get("mime_type", "image/jpeg")
                            data_uri = f"data:{mime_type};base64,{base64_data}"
                            image_references.append(data_uri)
                            logger.info(f"图片的Data URI已准备好，直接注入！MIME: {mime_type}")
                        except Exception as e:
                            logger.error(f"处理图片Data URI时高潮失败: {e}", exc_info=True)
                            # 如果失败了，就看看有没有URL这个备用小玩具
                            if url := seg.data.get("url"):
                                image_references.append(url)
                    elif url := seg.data.get("url"):
                        image_references.append(url)
                elif seg.type == "text":
                    main_content_parts.append(seg.data.get("text", ""))
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
            if text_only := extract_text_from_content(event_data_log.content):
                last_valid_text_message = text_only

            display_tag = f"{main_content_type}{', ' + quote_display_str if quote_display_str else ''}"
            log_line = f"[{time_str}] {log_user_id_str} [{display_tag}]: {main_content_str} (id:{msg_id_for_display})"
            if log_user_id_str == "U0" and (motivation := getattr(event_data_log, "motivation", None)):
                log_line += f"\n    - [MOTIVE]: {motivation}"

        # ... (处理其他事件类型的逻辑不变) ...
        elif event_data_log.event_type.startswith("notice."):
            main_content_parts = []  # 确保这里也初始化了
            main_content_type = "NOTICE"
            notice_data = event_data_log.content[0].data if event_data_log.content else {}
            # 从事件类型里把具体的通知类型抠出来，比如 'member_increase'
            notice_subtype = event_data_log.event_type.split(".")[-1]

            # 开始区分不同的通知类型，拼出人话
            if notice_subtype == "member_increase":
                operator_info = notice_data.get("operator_user_info", {})
                operator_id = operator_info.get("user_id") if operator_info else None
                operator_uid = (
                    platform_id_to_uid_str.get(operator_id, f"未知用户({str(operator_id)[:4]})")
                    if operator_id
                    else "系统"
                )

                target_id = event_data_log.user_info.user_id if event_data_log.user_info else None
                target_uid = (
                    platform_id_to_uid_str.get(target_id, f"未知用户({str(target_id)[:4]})")
                    if target_id
                    else "一位新成员"
                )

                if notice_data.get("join_type") == "approve":
                    main_content_parts.append(f"{target_uid} 加入了群聊。")
                else:
                    main_content_parts.append(f"{operator_uid} 邀请 {target_uid} 加入了群聊。")

            elif notice_subtype == "member_decrease":
                operator_info = notice_data.get("operator_user_info", {})
                operator_id = operator_info.get("user_id") if operator_info else None
                operator_uid = (
                    platform_id_to_uid_str.get(operator_id, f"未知用户({str(operator_id)[:4]})")
                    if operator_id
                    else "系统"
                )

                target_id = event_data_log.user_info.user_id if event_data_log.user_info else None
                target_uid = (
                    platform_id_to_uid_str.get(target_id, f"未知用户({str(target_id)[:4]})")
                    if target_id
                    else "一位成员"
                )

                if notice_data.get("leave_type") == "kick":
                    main_content_parts.append(f"{operator_uid} 将 {target_uid} 移出了群聊。")
                else:
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

            elif notice_subtype == "recalled":
                operator_info = notice_data.get("operator_user_info", {})
                operator_id = operator_info.get("user_id") if operator_info else None
                operator_uid = platform_id_to_uid_str.get(operator_id, "一位用户") if operator_id else "一位用户"
                main_content_parts.append(f"{operator_uid} 撤回了一条消息。")

            elif notice_subtype == "poke":
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

    # 收尾工作，确保已读标记正确
    if not is_first_turn and not unread_section_started and chat_log_lines:
        marker_ts = raw_events[-1].time if raw_events else last_processed_timestamp
        read_marker_time_obj = datetime.fromtimestamp(marker_ts / 1000.0)
        chat_log_lines.append(
            f"--- 以上消息是你已经思考过的内容，已读 (标记时间: {read_marker_time_obj.strftime('%H:%M:%S')}) ---"
        )

    chat_history_log_block_str = "\n".join(chat_log_lines) or "当前没有聊天记录。"

    # 收集需要标记为已读的事件ID
    processed_event_ids = [
        event.event_id
        for event in raw_events
        if event.event_type.startswith("message.") and event.time > last_processed_timestamp
    ]

    # 准备好反向的用户ID映射
    uid_str_to_platform_id_map = {uid: pid for pid, uid in platform_id_to_uid_str.items()}

    # 最后，把所有零件都塞进我们那个性感的容器里，一次性射给你！
    return PromptComponents(
        chat_history_log_block=chat_history_log_block_str,
        user_list_block=user_list_block_str,
        conversation_info_block=conversation_info_block_str,
        user_map=user_map,
        uid_str_to_platform_id_map=uid_str_to_platform_id_map,
        processed_event_ids=processed_event_ids,
        image_references=image_references,
        conversation_name=conversation_name_str,
        last_valid_text_message=last_valid_text_message,
    )
