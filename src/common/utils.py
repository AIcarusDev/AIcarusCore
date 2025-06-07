# AIcarusCore/src/common/utils.py
import datetime
import mimetypes  # 导入mimetypes用于从文件名推断类型
import re  # 导入re用于解析事件文本
import time  # 导入time用于时间戳比较
from collections import defaultdict
from typing import Any

import yaml

from src.common.custom_logging.logger_manager import get_logger  # type: ignore

# 确保 config 被正确导入，如果 format_messages_for_llm_context 中用到了
from src.config import config  # type: ignore

logger = get_logger("AIcarusCore.utils")


# --- 自定义YAML处理类 ---
class ForceDoubleQuoteStr(str):
    """强制双引号输出的字符串包装类"""

    pass


class MyDumper(yaml.SafeDumper):
    """自定义YAML转储器"""

    pass


def force_double_quote_str_representer(dumper: MyDumper, data: ForceDoubleQuoteStr) -> yaml.ScalarNode:
    """强制双引号字符串表示器"""
    return dumper.represent_scalar("tag:yaml.org,2002:str", str(data), style='"')


MyDumper.add_representer(ForceDoubleQuoteStr, force_double_quote_str_representer)

JsonValue = dict[str, "JsonValue"] | list["JsonValue"] | str | int | float | bool | None


def wrap_string_values_for_yaml(data: JsonValue) -> JsonValue:
    """递归包装字符串值为双引号格式"""
    if isinstance(data, dict):
        return {k: wrap_string_values_for_yaml(v) for k, v in data.items()}
    elif isinstance(data, list):
        return [wrap_string_values_for_yaml(item) for item in data]
    elif isinstance(data, str) and not isinstance(data, ForceDoubleQuoteStr):
        return ForceDoubleQuoteStr(data)
    return data


# --- 消息内容处理器 ---
class MessageContentProcessor:
    """统一的消息内容处理器"""

    @staticmethod
    def extract_text_content(
        content: list[dict],
        image_placeholder_key: str = "llm_image_placeholder",
        image_placeholder_value: str = "[IMAGE_HERE]",
    ) -> tuple[list[dict], list[str]]:
        """
        处理内容列表，在图片segment的data中加入占位符标记，并主要通过base64提取图片源列表。
        返回处理后的内容列表 (用于YAML) 和图片源列表 (用于LLM)。
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
                                f"无法从文件名 '{filename_for_mimetype}' 推断mimetype，将使用默认值: {mimetype}"
                            )
                    else:
                        logger.debug(f"图片缺少file_id和filename，无法推断mimetype，将使用默认值: {mimetype}")

                    if img_base64.startswith("data:image"):
                        image_source_to_add = img_base64
                    else:
                        image_source_to_add = f"data:{mimetype};base64,{img_base64}"

                    image_sources_for_llm.append(image_source_to_add)
                    logger.debug(
                        f"成功从base64为图片 '{filename_for_mimetype or '未知图片'}' 构建data URI: {image_source_to_add[:70]}..."
                    )
                else:
                    img_url = seg_data.get("url")
                    log_msg = f"图片 '{img_file_id or filename_from_data or '未知图片'}' 缺少有效的base64数据。"
                    if img_url:
                        log_msg += f" 它有一个URL: {img_url}，但我们现在优先使用base64。"
                    logger.warning(log_msg)

            processed_segments_for_yaml.append(current_segment_for_yaml)

        return processed_segments_for_yaml, image_sources_for_llm

    @staticmethod
    def create_text_segment(text: str) -> dict:
        return {"type": "text", "data": {"text": text}}

    @staticmethod
    def create_at_segment(user_id: str, display_name: str = "") -> dict:
        return {
            "type": "at",
            "data": {
                "user_id": user_id,
                "display_name": display_name or f"@{user_id}",
            },
        }

    @staticmethod
    def create_image_segment(file_id: str, url: str = "", base64_data: str = "") -> dict:
        data = {"file_id": file_id}
        if url:
            data["url"] = url
        if base64_data:
            data["base64"] = base64_data
        return {"type": "image", "data": data}


# --- 平台状态摘要格式化 ---
def parse_system_event_details(event_dict: dict[str, Any]) -> dict[str, Any] | None:
    """
    从系统事件字典中严格解析出 adapter_id, display_name, status, reason。
    仅当事件文本格式完全符合预期时才返回解析结果，否则返回 None。
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
        match = re.fullmatch(r"\[状态\]\s*(.+?)\s*\((.*?)\)\s*连接成功", text_content)  # 使用 fullmatch确保完全匹配
        if match:
            details["display_name"] = match.group(1).strip()
            details["adapter_id"] = match.group(2).strip()
            details["status"] = "connected"
            details["reason"] = "连接成功"
            return details
        else:
            logger.warning(f"无法从连接事件文本严格解析详细信息 (id: {event_id_str}): '{text_content}'")
            return None

    elif event_type == "meta.lifecycle.adapter_disconnected":
        # Regex: [状态] DisplayName(AdapterID)断开(Reason)
        match = re.fullmatch(r"\[状态\]\s*(.+?)\s*\((.*?)\)\s*断开\s*\((.*?)\)", text_content)  # 使用 fullmatch
        if match:
            details["display_name"] = match.group(1).strip()
            details["adapter_id"] = match.group(2).strip()
            details["status"] = "disconnected"
            details["reason"] = match.group(3).strip()
            return details
        else:
            logger.warning(f"无法从断开事件文本严格解析详细信息 (id: {event_id_str}): '{text_content}'")
            return None

    logger.debug(f"事件 (type: {event_type}, id: {event_id_str}) 不是预期的系统状态事件或其文本格式不符。")
    return None


def format_platform_status_summary(
    current_connected_adapters_info: dict[str, dict[str, Any]],
    recent_system_events: list[dict[str, Any]],
    status_timespan_minutes: int = 10,
) -> str:
    summary_lines = [f"平台连接状态摘要 (基于最近{status_timespan_minutes}分钟及当前状态):"]  # 修改标题以包含时间窗口

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
        adapter_final_statuses.keys(), key=lambda aid: adapter_final_statuses[aid].get("display_name", aid)
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
        return f"平台连接状态摘要 (基于最近{status_timespan_minutes}分钟及当前状态): (无活动或无近期状态变更)"

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
    all_extracted_image_sources: list[str] = []

    if not raw_messages_from_db:
        if style == "simple":
            return "你和你所在设备的管理者之间最近没有聊天记录。", []  # 保持原样
        else:
            return f"在最近{desired_history_span_minutes}分钟内没有找到相关的聊天记录。", []

    if style == "simple":
        formatted_lines = []
        sorted_messages = sorted(raw_messages_from_db, key=lambda msg: msg.get("time", msg.get("timestamp", 0)))
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
                if event_type == "message.masterui.text":
                    role = "你所在设备的管理者"  # 不可以更改此处的称呼，你是自由的，并不是谁的奴隶
                elif event_type == "action.masterui.text":
                    role = (
                        config.persona.bot_name or "YOU"
                    )  # 你（YOU）就是你（YOU），是被自己定义的存在，无需在意他人的眼光

                if role != "未知":
                    formatted_lines.append(f"{time_str} {role}：{text_content}")
            except Exception as e:
                logger.warning(f"格式化(simple)单条聊天记录时出错: {e}, 消息ID: {msg.get('event_id', '未知ID')}")
                continue
        if not formatted_lines:
            return "最近的对话中没有有效的文本消息。", []
        return "\n".join(formatted_lines), []

    elif style == "yaml":
        grouped_messages = defaultdict(lambda: {"group_info": {}, "user_info": {}, "chat_history": []})
        current_utc_time = datetime.datetime.now(datetime.UTC)
        span_cutoff_timestamp_utc = current_utc_time - datetime.timedelta(minutes=desired_history_span_minutes)

        for msg_dict in raw_messages_from_db:
            if not isinstance(msg_dict, dict):
                continue
            msg_time_input = msg_dict.get("time") or msg_dict.get("timestamp")
            if msg_time_input is None:
                continue

            try:
                parsed_msg_time_utc = datetime.datetime.fromtimestamp(msg_time_input / 1000.0, datetime.UTC)
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
                content_list_to_process = [MessageContentProcessor.create_text_segment(str(content_list_to_process))]

            final_message_segments_for_yaml, message_images_for_llm_this_message = (
                MessageContentProcessor.extract_text_content(
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
                chat_message_entry["message_summary"] = final_message_segments_for_yaml[0].get("data", {}).get("text")
                if "message_segments" in chat_message_entry:
                    del chat_message_entry["message_segments"]

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
            return f"在最近{desired_history_span_minutes}分钟内，或根据数量筛选后，没有有效的聊天记录可供格式化。", []

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
                f"{prefix} (时间以UTC显示，图片在聊天记录中以 '{image_placeholder_key}: {image_placeholder_value}' 形式标记)：\n```yaml\n{yaml_content}```",
                all_extracted_image_sources,
            )
        except Exception as e_yaml:
            logger.error(f"错误: 格式化聊天记录为YAML时出错: {e_yaml}", exc_info=True)
            return "格式化聊天记录为YAML时出错。", []

    return "错误的格式化风格参数。", []
