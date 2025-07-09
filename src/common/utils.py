# AIcarusCore/src/common/utils.py
import datetime
import mimetypes  # 导入mimetypes用于从文件名推断类型
import re  # 导入re用于解析事件文本
import time  # 导入time用于时间戳比较
from collections import defaultdict
from typing import Any

import yaml
from aicarus_protocols import SegBuilder
from src.common.custom_logging.logging_config import get_logger

logger = get_logger(__name__)


# --- 自定义YAML处理类 ---
class ForceDoubleQuoteStr(str):
    """强制双引号输出的字符串包装类.

    该类用于确保在YAML转储时，字符串总是使用双引号样式表示.
    """

    pass


class MyDumper(yaml.SafeDumper):
    """自定义YAML转储器.

    该类用于处理 ForceDoubleQuoteStr 类型的字符串，使其在YAML中总是使用双引号表示.
    """

    pass


def force_double_quote_str_representer(
    dumper: MyDumper, data: ForceDoubleQuoteStr
) -> yaml.ScalarNode:
    """强制双引号字符串表示器.

    Args:
        dumper: 自定义YAML转储器.
        data: 强制双引号字符串.

    Returns:
        yaml.ScalarNode: 包装后的YAML标量节点，使用双引号样式表示字符串.
    """
    return dumper.represent_scalar("tag:yaml.org,2002:str", str(data), style='"')


MyDumper.add_representer(ForceDoubleQuoteStr, force_double_quote_str_representer)

JsonValue = dict[str, "JsonValue"] | list["JsonValue"] | str | int | float | bool | None


def wrap_string_values_for_yaml(data: JsonValue) -> JsonValue:
    """递归包装字符串值为双引号格式.

    Args:
        data: 输入数据，可以是字典、列表或基本类型.

    Returns:
        JsonValue: 处理后的数据，所有字符串都被包装为双引号格式.
    """
    if isinstance(data, dict):
        return {k: wrap_string_values_for_yaml(v) for k, v in data.items()}
    elif isinstance(data, list):
        return [wrap_string_values_for_yaml(item) for item in data]
    elif isinstance(data, str) and not isinstance(data, ForceDoubleQuoteStr):
        return ForceDoubleQuoteStr(data)
    return data


def is_valid_message(msg: str) -> bool:
    """检查消息是否有效，过滤掉 null 和占位符。真麻烦."""
    if not msg or not isinstance(msg, str) or msg.strip().lower() == "null":
        return False
    # // 正则表达式，用来匹配 "text_数字" 这种无聊的占位符
    return not re.fullmatch(r"text_\d+", msg.strip())


# --- 消息内容解析器 ---
class MessageParser:
    """统一的消息内容解析器.

    Attributes:
        image_placeholder_key: 用于图片占位符的键名.
        image_placeholder_value: 用于图片占位符的值.
    """

    @staticmethod
    def extract_text_content(
        content: list[dict],
        image_placeholder_key: str = "llm_image_placeholder",
        image_placeholder_value: str = "[IMAGE_HERE]",
    ) -> tuple[list[dict], list[str]]:
        """处理内容列表，在图片segment的data中加入占位符标记，并主要通过base64提取图片源列表.

        返回处理后的内容列表 (用于YAML) 和图片源列表 (用于LLM).

        Args:
            content: 包含消息段的列表，每个段是一个字典，可能包含文本、图片等信息.
            image_placeholder_key: 用于图片占位符的键名.
            image_placeholder_value: 用于图片占位符的键值.

        Returns:
            tuple: 包含两个元素的元组: 处理后的内容列表和图片源列表.
        """
        if not content or not isinstance(content, list):
            return [], []

        processed_segments_for_yaml = []
        image_sources_for_llm = []

        for segment in content:
            if not isinstance(segment, dict):
                processed_segments_for_yaml.append(segment)
                continue

            current_segment_for_yaml = segment.copy()
            seg_type = segment.get("type", "")
            seg_data = segment.get("data", {})

            if seg_type == "message_metadata":
                continue

            if seg_type == "image":
                current_segment_for_yaml["data"] = {
                    image_placeholder_key: image_placeholder_value,
                    "original_file_id": seg_data.get("file_id"),
                }
                if seg_data.get("filename"):
                    current_segment_for_yaml["data"]["original_filename"] = seg_data.get("filename")

                img_base64 = seg_data.get("base64")
                img_file_id = seg_data.get("file_id")
                filename_from_data = seg_data.get("filename")

                image_source_to_add = None
                if img_base64 and isinstance(img_base64, str) and img_base64.strip():
                    mimetype = "image/jpeg"
                    filename_for_mimetype = img_file_id or filename_from_data
                    if filename_for_mimetype and isinstance(filename_for_mimetype, str):
                        guessed_mimetype, _ = mimetypes.guess_type(filename_for_mimetype)
                        if guessed_mimetype:
                            mimetype = guessed_mimetype
                        else:
                            logger.debug(
                                f"无法从文件名 '{filename_for_mimetype}' 推断mimetype，"
                                f"将使用默认值: {mimetype}"
                            )
                    else:
                        logger.debug(
                            f"图片缺少file_id和filename，无法推断mimetype，将使用默认值: {mimetype}"
                        )

                    if img_base64.startswith("data:image"):
                        image_source_to_add = img_base64
                    else:
                        image_source_to_add = f"data:{mimetype};base64,{img_base64}"

                    image_sources_for_llm.append(image_source_to_add)
                    logger.debug(
                        f"成功从base64为图片 '{filename_for_mimetype or '未知图片'}' "
                        f"构建data URI: {image_source_to_add[:70]}..."
                    )
                else:
                    img_url = seg_data.get("url")
                    log_msg = (
                        f"图片 '{img_file_id or filename_from_data or '未知图片'}' "
                        f"缺少有效的base64数据。"
                    )
                    if img_url:
                        log_msg += f" 它有一个URL: {img_url}，但我们现在优先使用base64。"
                    logger.warning(log_msg)

            processed_segments_for_yaml.append(current_segment_for_yaml)

        return processed_segments_for_yaml, image_sources_for_llm


# --- 平台状态摘要格式化 ---
def parse_system_event_details(event_dict: dict[str, Any]) -> dict[str, Any] | None:
    """从系统事件字典中严格解析出 adapter_id, display_name, status, reason.

    仅当事件文本格式完全符合预期时才返回解析结果，否则返回 None.

    Args:
        event_dict: 包含事件信息的字典，必须包含 'event_type' 和 'content' 字段.
        event_dict: dict[str, Any]
            - 'event_type': str, 事件类型标识.
            - 'content': list, 包含事件内容的列表，通常第一个元素是文本段落.

    Returns:
        dict[str, Any] | None: 包含解析后的详细信息的字典，或 None.
    """
    details: dict[str, Any] = {}
    event_type = event_dict.get("event_type")
    event_id_str = event_dict.get("event_id", "unknown_event_id")  # 用于日志

    text_content = ""
    content_list = event_dict.get("content")
    if isinstance(content_list, list) and len(content_list) > 0:
        first_seg = content_list[0]
        if isinstance(first_seg, dict) and first_seg.get("type") == "text":
            text_content = first_seg.get("data", {}).get("text", "")

    if not text_content:
        logger.debug(f"系统事件 (type: {event_type}, id: {event_id_str}) 无文本内容可供解析。")
        return None

    if event_type == "meta.lifecycle.adapter_connected":
        # Regex: [状态] DisplayName(AdapterID)连接成功
        match = re.fullmatch(
            r"\[状态\]\s*(.+?)\s*\((.*?)\)\s*连接成功", text_content
        )  # 使用 fullmatch确保完全匹配
        if match:
            details["display_name"] = match.group(1).strip()
            details["adapter_id"] = match.group(2).strip()
            details["status"] = "connected"
            details["reason"] = "连接成功"
            return details
        else:
            logger.warning(
                f"无法从连接事件文本严格解析详细信息 (id: {event_id_str}): '{text_content}'"
            )
            return None

    elif event_type == "meta.lifecycle.adapter_disconnected":
        # Regex: [状态] DisplayName(AdapterID)断开(Reason)
        match = re.fullmatch(
            r"\[状态\]\s*(.+?)\s*\((.*?)\)\s*断开\s*\((.*?)\)", text_content
        )  # 使用 fullmatch
        if match:
            details["display_name"] = match.group(1).strip()
            details["adapter_id"] = match.group(2).strip()
            details["status"] = "disconnected"
            details["reason"] = match.group(3).strip()
            return details
        else:
            logger.warning(
                f"无法从断开事件文本严格解析详细信息 (id: {event_id_str}): '{text_content}'"
            )
            return None

    logger.debug(
        f"事件 (type: {event_type}, id: {event_id_str}) 不是预期的系统状态事件或其文本格式不符。"
    )
    return None


def format_platform_status_summary(
    current_connected_adapters_info: dict[str, dict[str, Any]],
    recent_system_events: list[dict[str, Any]],
    status_timespan_minutes: int = 10,
) -> str:
    """格式化平台连接状态摘要，基于最近的系统事件和当前连接的适配器信息.

    Args:
        current_connected_adapters_info: 当前在线适配器的信息字典，键为适配器ID，
            值为包含显示名称和最后心跳时间的字典.
        recent_system_events: 最近的系统事件列表，每个事件是一个字典，包含时间戳和内容等信息.
        status_timespan_minutes: 用于确定最近状态变更的时间窗口（分钟），
            默认为10分钟.

    Returns:
        str: 格式化后的连接状态摘要字符串.
    """
    summary_lines = [
        f"平台连接状态摘要 (基于最近{status_timespan_minutes}分钟及当前状态):"
    ]  # 修改标题以包含时间窗口

    adapter_final_statuses: dict[str, dict[str, Any]] = {}

    # 1. 初始化当前在线的适配器状态
    for adapter_id, info in current_connected_adapters_info.items():
        display_name = info.get("display_name", adapter_id)
        adapter_final_statuses[adapter_id] = {
            "display_name": display_name,
            "status": "connected",
            "timestamp": info.get("last_heartbeat", time.time()),
            "reason": "当前在线",
        }

    # 2. 处理最近10分钟的系统事件，以获取更精确的状态和时间
    cutoff_timestamp_sec = time.time() - (status_timespan_minutes * 60)
    sorted_recent_events = sorted(recent_system_events, key=lambda e: e.get("time", 0))

    for event_dict in sorted_recent_events:
        event_timestamp_ms = event_dict.get("time")
        if not event_timestamp_ms:
            continue
        event_timestamp_sec = event_timestamp_ms / 1000.0

        parsed_details = parse_system_event_details(event_dict)
        if not parsed_details:
            continue

        event_adapter_id = parsed_details.get("adapter_id")
        if not event_adapter_id:
            continue

        current_display_name = parsed_details.get("display_name", event_adapter_id)
        current_status = parsed_details.get("status")
        current_reason = parsed_details.get("reason", "")

        if (
            event_timestamp_sec >= cutoff_timestamp_sec
            or event_adapter_id not in adapter_final_statuses
            or event_timestamp_sec > adapter_final_statuses[event_adapter_id].get("timestamp", 0)
        ):
            adapter_final_statuses[event_adapter_id] = {
                "display_name": current_display_name,
                "status": current_status,
                "timestamp": event_timestamp_sec,
                "reason": current_reason,
            }

    # 3. 构建输出字符串
    online_lines = []
    recent_changes_lines = []

    sorted_adapter_ids = sorted(
        adapter_final_statuses.keys(),
        key=lambda aid: adapter_final_statuses[aid].get("display_name", aid),
    )

    for adapter_id in sorted_adapter_ids:
        info = adapter_final_statuses[adapter_id]
        display_name = info["display_name"]
        status = info["status"]
        timestamp_sec = info["timestamp"]
        reason = info.get("reason", "")

        dt_object = datetime.datetime.fromtimestamp(timestamp_sec, datetime.UTC)
        time_str = dt_object.strftime("%H:%M UTC")

        is_within_timespan = timestamp_sec >= cutoff_timestamp_sec

        line_text = f"    - {display_name}({adapter_id}): "

        if adapter_id in current_connected_adapters_info:
            line_text += "已连接"
            # 检查连接事件是否在10分钟内，而不是用 last_heartbeat
            # 需要从 adapter_final_statuses 中找到该 adapter_id 的 "connected" 事件时间
            if is_within_timespan and status == "connected" and reason == "连接成功":
                line_text += f" (于 {time_str})"
            else:
                line_text += " (状态稳定)"
            online_lines.append(line_text)
        elif status == "disconnected" and is_within_timespan:
            recent_changes_lines.append(f"{line_text}已断开 (于 {time_str}, 原因: {reason})")

    if online_lines:
        summary_lines.append("  当前已连接:")
        summary_lines.extend(online_lines)
    else:
        summary_lines.append("  当前已连接: (无)")

    if recent_changes_lines:
        summary_lines.append(f"  最近{status_timespan_minutes}分钟内状态变更:")
        summary_lines.extend(recent_changes_lines)

    if len(summary_lines) == 1:
        return (
            f"平台连接状态摘要 (基于最近{status_timespan_minutes}分钟及当前状态): "
            f"(无活动或无近期状态变更)"
        )

    return "\n".join(summary_lines)


# --- 聊天记录格式化 ---
def format_messages_for_llm_context(
    raw_messages_from_db: list[dict[str, Any]],
    style: str = "yaml",
    image_placeholder_key: str = "llm_image_placeholder",
    image_placeholder_value: str = "[IMAGE_HERE]",
    desired_history_span_minutes: int = 10,
    max_messages_per_group: int = 20,
) -> tuple[str, list[str]]:
    """格式化聊天记录以供 LLM 使用.

    支持两种风格：简单文本和 YAML 格式.

    Args:
        raw_messages_from_db: 从数据库获取的原始消息列表.
        style: 输出格式，支持 "simple" 或 "yaml".
        image_placeholder_key: 用于图片占位符的键名.
        image_placeholder_value: 用于图片占位符的值.
        desired_history_span_minutes: 希望获取的聊天记录时间跨度（分钟）.
        max_messages_per_group: 每个会话组中保留的最大消息数.

    Returns:
        tuple: 包含格式化后的聊天记录字符串和提取的图片源列表.
    """
    all_extracted_image_sources: list[str] = []

    if not raw_messages_from_db:
        if style == "simple":
            return "你和你所在设备的管理者之间最近没有聊天记录。", []  # 保持原样
        else:
            return f"在最近{desired_history_span_minutes}分钟内没有找到相关的聊天记录。", []

    if style == "simple":
        formatted_lines = []
        sorted_messages = sorted(
            raw_messages_from_db, key=lambda msg: msg.get("time", msg.get("timestamp", 0))
        )
        for msg in sorted_messages:
            try:
                ts_ms = msg.get("time", msg.get("timestamp", 0))
                dt_object = datetime.datetime.fromtimestamp(
                    ts_ms / 1000.0, datetime.timezone(datetime.timedelta(hours=8))
                )
                time_str = dt_object.strftime("%H:%M UTC")

                text_content = ""
                content_list = msg.get("content", [])
                if isinstance(content_list, list):
                    text_parts = [
                        seg.get("data", {}).get("text", "")
                        for seg in content_list
                        if isinstance(seg, dict) and seg.get("type") == "text"
                    ]
                    text_content = "".join(text_parts).strip()

                if not text_content:
                    if msg.get("event_type", "").startswith("meta.lifecycle.adapter_"):
                        pass
                    continue

                role = "未知"
                event_type = msg.get("event_type", "")
                if event_type == "system.notice":
                    role = "系统通知"

                if role != "未知":
                    formatted_lines.append(f"{time_str} {role}：{text_content}")
            except Exception as e:
                logger.warning(
                    f"格式化(simple)单条聊天记录时出错: {e}, "
                    f"消息ID: {msg.get('event_id', '未知ID')}"
                )
                continue
        if not formatted_lines:
            return "最近的对话中没有有效的文本消息。", []
        return "\n".join(formatted_lines), []

    elif style == "yaml":
        grouped_messages = defaultdict(
            lambda: {"group_info": {}, "user_info": {}, "chat_history": []}
        )
        current_utc_time = datetime.datetime.now(datetime.UTC)
        span_cutoff_timestamp_utc = current_utc_time - datetime.timedelta(
            minutes=desired_history_span_minutes
        )

        for msg_dict in raw_messages_from_db:
            if not isinstance(msg_dict, dict):
                continue
            msg_time_input = msg_dict.get("time") or msg_dict.get("timestamp")
            if msg_time_input is None:
                continue

            try:
                parsed_msg_time_utc = datetime.datetime.fromtimestamp(
                    msg_time_input / 1000.0, datetime.UTC
                )
            except Exception:
                continue

            if parsed_msg_time_utc < span_cutoff_timestamp_utc:
                continue

            platform = msg_dict.get("platform", "未知平台")
            conversation_info = msg_dict.get("conversation_info", {})

            if not isinstance(conversation_info, dict):
                conversation_info = {}

            group_id_val = conversation_info.get("conversation_id", "unknown_conversation")
            group_key = (platform, str(group_id_val))

            if not grouped_messages[group_key]["group_info"]:
                group_name = conversation_info.get("name")
                if not group_name and group_id_val == "system_events":
                    group_name = "系统事件频道"
                elif not group_name:
                    user_info_for_group = msg_dict.get("user_info", {})
                    if not isinstance(user_info_for_group, dict):
                        user_info_for_group = {}
                    sender_nick_for_group = user_info_for_group.get("user_nickname", "未知用户")
                    group_name = (
                        f"与 {sender_nick_for_group} 的对话"
                        if group_id_val == "unknown_conversation" or not group_id_val
                        else f"会话_{group_id_val}"
                    )

                grouped_messages[group_key]["group_info"] = {
                    "platform_name": platform,
                    "conversation_id": str(group_id_val),
                    "conversation_name": group_name,
                    "conversation_type": conversation_info.get("type", "unknown"),
                }

            user_info = msg_dict.get("user_info", {})
            if not isinstance(user_info, dict):
                user_info = {}
            sender_id = user_info.get("user_id", "SYSTEM_OR_UNKNOWN")

            if sender_id and sender_id != "SYSTEM_OR_UNKNOWN":
                sender_id_str = str(sender_id)
                if sender_id_str not in grouped_messages[group_key]["user_info"]:
                    grouped_messages[group_key]["user_info"][sender_id_str] = {
                        "nickname": user_info.get("user_nickname", f"用户_{sender_id_str}"),
                        "cardname": user_info.get("user_cardname"),
                        "permission": user_info.get("permission_level") or user_info.get("role"),
                    }

            formatted_time = parsed_msg_time_utc.strftime("%Y-%m-%d %H:%M:%S UTC")

            content_list_to_process = msg_dict.get("content", [])
            if not isinstance(content_list_to_process, list):
                content_list_to_process = [SegBuilder.text(str(content_list_to_process))]

            final_message_segments_for_yaml, message_images_for_llm_this_message = (
                MessageParser.extract_text_content(
                    content_list_to_process, image_placeholder_key, image_placeholder_value
                )
            )
            all_extracted_image_sources.extend(message_images_for_llm_this_message)

            chat_message_entry = {
                "time": formatted_time,
                "event_type": msg_dict.get("event_type", "unknown"),
                "sender_id": str(sender_id),
                "message_segments": final_message_segments_for_yaml,
                "_parsed_timestamp_utc": parsed_msg_time_utc,
            }
            if (
                msg_dict.get("event_type", "").startswith("meta.lifecycle.adapter_")
                and final_message_segments_for_yaml
                and final_message_segments_for_yaml[0].get("type") == "text"
            ):
                chat_message_entry["message_summary"] = (
                    final_message_segments_for_yaml[0].get("data", {}).get("text")
                )
                chat_message_entry.pop("message_segments", None)

            grouped_messages[group_key]["chat_history"].append(chat_message_entry)

        output_list_for_yaml = []
        for _, data in grouped_messages.items():
            current_chat_history = data["chat_history"]
            if current_chat_history:
                current_chat_history.sort(key=lambda x: x["_parsed_timestamp_utc"])
                final_filtered_history = current_chat_history[-max_messages_per_group:]
                for msg_to_clean in final_filtered_history:
                    del msg_to_clean["_parsed_timestamp_utc"]
                data["chat_history"] = final_filtered_history
                if data["chat_history"]:
                    output_list_for_yaml.append(
                        {
                            "session_context": data["group_info"],
                            "known_users_in_session": data["user_info"] or {},
                            "recent_messages": data["chat_history"],
                        }
                    )

        if not output_list_for_yaml:
            return (
                f"在最近{desired_history_span_minutes}分钟内，或根据数量筛选后，没有有效的聊天记录可供格式化。",
                [],
            )

        try:
            data_to_dump = (
                output_list_for_yaml[0]
                if len(output_list_for_yaml) == 1
                else {"multiple_chat_sessions": output_list_for_yaml}
            )
            data_to_dump_processed = wrap_string_values_for_yaml(data_to_dump)

            yaml_content = yaml.dump(
                data_to_dump_processed,
                Dumper=MyDumper,
                allow_unicode=True,
                sort_keys=False,
                indent=2,
                default_flow_style=False,
            )
            prefix = "以下是最近的上下文信息"
            return (
                f"{prefix} (时间以UTC显示，图片在聊天记录中以 '{image_placeholder_key}: "
                f"{image_placeholder_value}' 形式标记)：\n```yaml\n{yaml_content}```",
                all_extracted_image_sources,
            )
        except Exception as e_yaml:
            logger.error(f"错误: 格式化聊天记录为YAML时出错: {e_yaml}", exc_info=True)
            return "格式化聊天记录为YAML时出错。", []

    return "错误的格式化风格参数。", []
