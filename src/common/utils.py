# AIcarusCore/src/common/utils.py
import datetime
import uuid
from collections import defaultdict
from typing import Any

import yaml


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
    def extract_text_content(content: list[dict]) -> str:
        """从标准格式的内容列表中提取纯文本"""
        if not content or not isinstance(content, list):
            return ""

        text_parts = []
        for segment in content:
            if not isinstance(segment, dict):
                continue

            seg_type = segment.get("type", "")
            seg_data = segment.get("data", {})

            if seg_type == "text":
                text_parts.append(seg_data.get("text", ""))
            elif seg_type == "at":
                text_parts.append(seg_data.get("display_name", "@用户"))
            elif seg_type == "image":
                text_parts.append("[图片]")
            elif seg_type == "voice":
                text_parts.append("[语音]")
            elif seg_type == "file":
                filename = seg_data.get("filename", "文件")
                text_parts.append(f"[{filename}]")
            elif seg_type == "reply":
                text_parts.append("[回复]")
            elif seg_type == "forward":
                text_parts.append("[转发消息]")

        return " ".join(text_parts).strip()

    @staticmethod
    def create_text_segment(text: str) -> dict:
        """创建标准文本段"""
        return {"type": "text", "data": {"text": text}}

    @staticmethod
    def create_at_segment(user_id: str, display_name: str = "") -> dict:
        """创建@用户段"""
        return {
            "type": "at",
            "data": {
                "user_id": user_id,
                "display_name": display_name or f"@{user_id}",
            },
        }

    @staticmethod
    def create_image_segment(file_id: str, url: str = "") -> dict:
        """创建图片段"""
        return {
            "type": "image",
            "data": {"file_id": file_id, "url": url},
        }


# --- 简化的辅助函数 ---
def extract_text_from_segments(content: list[dict]) -> str:
    """提取文本内容的简化接口"""
    return MessageContentProcessor.extract_text_content(content)


def create_simple_text_message(text: str) -> list[dict]:
    """创建简单的文本消息内容"""
    return [MessageContentProcessor.create_text_segment(text)]


# --- 聊天记录格式化（更新为适应新结构）---
def format_chat_history_for_prompt(raw_messages_from_db: list[dict[str, Any]]) -> str:
    """
    将从数据库获取的原始消息列表格式化为YAML字符串
    适应新的消息结构 (v1.4.0 协议)
    """
    if not raw_messages_from_db:
        return "在指定的时间范围内没有找到聊天记录。"

    grouped_messages = defaultdict(lambda: {"group_info": {}, "user_info": {}, "chat_history": []})
    desired_history_span_minutes = 10
    max_messages_per_group = 20

    current_utc_time = datetime.datetime.now(datetime.UTC)
    span_cutoff_timestamp_utc = current_utc_time - datetime.timedelta(minutes=desired_history_span_minutes)
    assumed_local_tz_for_fallback = datetime.timezone(datetime.timedelta(hours=8))

    for msg in raw_messages_from_db:
        if not isinstance(msg, dict):
            print(f"警告: 消息应该是字典类型，但收到了 {type(msg)}: {msg}")
            continue

        # 支持新的时间戳字段结构
        msg_time_input = msg.get("timestamp") or msg.get("time")
        if msg_time_input is None:
            print(f"警告: 消息 {msg.get('event_id', msg.get('message_id', '未知ID'))} 缺少时间戳，已跳过。")
            continue

        # 时间戳处理逻辑（保持原有逻辑）
        try:
            if isinstance(msg_time_input, int | float):
                msg_time_seconds_utc = msg_time_input / 1000.0
                parsed_msg_time_utc = datetime.datetime.fromtimestamp(msg_time_seconds_utc, datetime.UTC)
            elif isinstance(msg_time_input, str):
                print(f"警告: 消息时间戳是字符串，尝试解析: {msg_time_input}")
                temp_time_str = msg_time_input.replace("Z", "+00:00")
                parsed_msg_time_aware_or_naive = datetime.datetime.fromisoformat(temp_time_str)

                if (
                    parsed_msg_time_aware_or_naive.tzinfo is None
                    or parsed_msg_time_aware_or_naive.tzinfo.utcoffset(parsed_msg_time_aware_or_naive) is None
                ):
                    parsed_msg_time_local = parsed_msg_time_aware_or_naive.replace(tzinfo=assumed_local_tz_for_fallback)
                    parsed_msg_time_utc = parsed_msg_time_local.astimezone(datetime.UTC)
                else:
                    parsed_msg_time_utc = parsed_msg_time_aware_or_naive.astimezone(datetime.UTC)
            else:
                raise ValueError(f"时间戳类型不支持: {type(msg_time_input)}")

        except ValueError as e_time:
            print(f"警告: 时间戳处理失败: {e_time}")
            continue

        # 时间筛选
        if parsed_msg_time_utc < span_cutoff_timestamp_utc:
            continue

        # 获取消息分组信息 - 适配新结构
        platform = msg.get("platform", "未知平台")
        conversation_info = msg.get("conversation_info", {})
        group_id_val = (
            conversation_info.get("conversation_id") if isinstance(conversation_info, dict) else msg.get("group_id")
        )
        group_key_part = str(group_id_val) if group_id_val is not None else "direct_or_no_group"
        group_key = (platform, group_key_part)

        # 群组信息处理 - 适配新结构
        if not grouped_messages[group_key]["group_info"]:
            group_name = None
            if isinstance(conversation_info, dict):
                group_name = conversation_info.get("name")

            if group_name is None:
                group_name = msg.get("group_name")

            if group_id_val is None and not group_name:
                # 尝试从用户信息获取昵称
                user_info = msg.get("user_info", {})
                sender_nick_for_group_name = None
                if isinstance(user_info, dict):
                    sender_nick_for_group_name = user_info.get("user_nickname")

                if not sender_nick_for_group_name:
                    sender_nick_for_group_name = msg.get("sender_nickname", "未知用户")

                group_name = (
                    f"与 {sender_nick_for_group_name} 的对话"
                    if sender_nick_for_group_name != "未知用户"
                    else "直接消息"
                )

            grouped_messages[group_key]["group_info"] = {
                "platform_name": platform,
                "group_id": str(group_id_val) if group_id_val is not None else None,
                "group_name": group_name
                if group_name is not None
                else ("未知群名" if group_id_val is not None else "直接消息会话"),
            }

        # 用户信息处理 - 适配新结构
        user_info = msg.get("user_info", {})
        sender_id = None
        if isinstance(user_info, dict):
            sender_id = user_info.get("user_id")

        if not sender_id:
            sender_id = msg.get("sender_id") or msg.get("user_id")

        if sender_id:
            sender_id_str = str(sender_id)
            if sender_id_str not in grouped_messages[group_key]["user_info"]:
                user_details = {}

                # 从新的user_info结构中获取信息
                if isinstance(user_info, dict):
                    user_details = {
                        "sender_nickname": user_info.get("user_nickname") or f"用户_{sender_id_str}",
                        "sender_group_permission": user_info.get("permission_level") or user_info.get("role"),
                    }

                    # 可选字段
                    for new_field, old_field in [
                        ("user_cardname", "sender_group_card"),
                        ("user_titlename", "sender_group_titlename"),
                    ]:
                        value = user_info.get(new_field, "")
                        if value:
                            user_details[old_field] = value
                else:
                    # 兼容旧结构
                    user_details = {
                        "sender_nickname": msg.get("sender_nickname") or f"用户_{sender_id_str}",
                        "sender_group_permission": msg.get("sender_group_permission"),
                    }

                    # 可选字段
                    for optional_field in ["sender_group_card", "sender_group_titlename"]:
                        value = msg.get(optional_field, "")
                        if value:
                            user_details[optional_field] = value

                grouped_messages[group_key]["user_info"][sender_id_str] = user_details

        # 消息内容处理 - 适配新结构
        formatted_time = parsed_msg_time_utc.strftime("%Y-%m-%d %H:%M:%S")

        # 优先从content字段获取消息内容，然后尝试message_content
        message_content_raw = msg.get("content")
        if message_content_raw is None:
            message_content_raw = msg.get("message_content")

        final_message_content = []

        if isinstance(message_content_raw, list):
            # 处理内容列表，排除metadata元素
            for segment in message_content_raw:
                if isinstance(segment, dict):
                    # 跳过metadata类型
                    if segment.get("type") == "message.metadata":
                        continue
                    final_message_content.append(segment.copy())

            # 如果所有元素都被排除，添加一个空文本
            if not final_message_content and message_content_raw:
                print("注意: 所有消息内容段都被过滤，使用空文本替代")
                final_message_content = [{"type": "text", "data": {"text": ""}}]
        elif isinstance(message_content_raw, dict) and "type" in message_content_raw:
            if message_content_raw.get("type") != "message.metadata":
                final_message_content = [message_content_raw.copy()]
        elif isinstance(message_content_raw, str):
            final_message_content = [{"type": "text", "data": {"text": message_content_raw}}]
        elif message_content_raw is None:
            # 处理空内容情况
            print("警告: 消息内容为None")
            final_message_content = [{"type": "text", "data": {"text": "[空消息]"}}]
        else:
            print(f"警告: 消息内容格式未知: {type(message_content_raw)}")
            final_message_content = [{"type": "text", "data": {"text": "[未识别的消息格式]"}}]

        # 构建新的chat_message结构
        chat_message = {
            "time": formatted_time,
            "post_type": msg.get("post_type", "message"),
            "sub_type": msg.get("sub_type"),
            "message_id": str(msg.get("event_id") or msg.get("message_id", uuid.uuid4())),
            "message": final_message_content,
            "sender_id": str(sender_id) if sender_id else "SYSTEM_OR_UNKNOWN",
            "_parsed_timestamp_utc": parsed_msg_time_utc,
        }
        grouped_messages[group_key]["chat_history"].append(chat_message)

    # 输出处理（保持原有逻辑）
    output_list_for_yaml = []
    for (_, _), data in grouped_messages.items():
        current_chat_history = data["chat_history"]

        if current_chat_history:
            # 排序和截断
            current_chat_history.sort(key=lambda x: x["_parsed_timestamp_utc"])
            if len(current_chat_history) > max_messages_per_group:
                final_filtered_history = current_chat_history[-max_messages_per_group:]
            else:
                final_filtered_history = current_chat_history

            # 清理临时字段
            for msg_to_clean in final_filtered_history:
                del msg_to_clean["_parsed_timestamp_utc"]

            data["chat_history"] = final_filtered_history

            if data["chat_history"]:
                group_data_for_yaml = {
                    "group_info": data["group_info"],
                    "user_info": data["user_info"] if data["user_info"] else {},
                    "chat_history": data["chat_history"],
                }
                output_list_for_yaml.append(group_data_for_yaml)

    if not output_list_for_yaml:
        return "在最近10分钟内，或根据数量筛选后，没有有效的聊天记录可供格式化。"

    # YAML输出
    try:
        if len(output_list_for_yaml) == 1:
            if "user_info" not in output_list_for_yaml[0]:
                output_list_for_yaml[0]["user_info"] = {}
            data_to_dump_processed = wrap_string_values_for_yaml(output_list_for_yaml[0])
        else:
            for item in output_list_for_yaml:
                if "user_info" not in item:
                    item["user_info"] = {}
            multi_group_structure = {"chat_sessions": output_list_for_yaml}
            data_to_dump_processed = wrap_string_values_for_yaml(multi_group_structure)

        yaml_content = yaml.dump(
            data_to_dump_processed,
            Dumper=MyDumper,
            allow_unicode=True,
            sort_keys=False,
            indent=2,
            default_flow_style=False,
        )

        prefix = (
            "以下是最近的聊天记录及相关内容"
            if len(output_list_for_yaml) == 1
            else "以下是最近多个会话的聊天记录及相关内容"
        )
        return f"{prefix} (时间以UTC显示)：\n```yaml\n{yaml_content}```"

    except Exception as e_yaml:
        print(f"错误: 格式化聊天记录为YAML时出错: {e_yaml}")
        return "格式化聊天记录为YAML时出错。"
