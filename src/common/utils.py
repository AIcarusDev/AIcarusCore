# AIcarusCore/src/common/utils.py
import datetime
import uuid
from collections import defaultdict
from typing import Any

import yaml  # 确保 yaml 已安装 (PyYAML)

# import asyncio # 在这个文件中 asyncio 未被直接使用


# --- 新增：自定义包装类和辅助函数，用于强制字符串值使用双引号 ---
class ForceDoubleQuoteStr(str):
    """一个简单的包装类，用于标记那些需要被强制双引号输出的字符串值。"""

    pass


class MyDumper(yaml.SafeDumper):
    """自定义Dumper，用于注册特定类型的representer。"""

    pass


def force_double_quote_str_representer(dumper: MyDumper, data: ForceDoubleQuoteStr) -> yaml.ScalarNode:
    """ForceDoubleQuoteStr 类型对象的 representer，强制使用双引号风格。"""
    return dumper.represent_scalar("tag:yaml.org,2002:str", str(data), style='"')


MyDumper.add_representer(ForceDoubleQuoteStr, force_double_quote_str_representer)


JsonValue = dict[str, "JsonValue"] | list["JsonValue"] | str | int | float | bool | None


def wrap_string_values_for_yaml(data: JsonValue) -> JsonValue:
    """
    递归地遍历数据结构，将所有作为“值”的字符串包装在 ForceDoubleQuoteStr 中。
    字典的键、None、数字、布尔值等保持原样。
    """
    if isinstance(data, dict):
        return {k: wrap_string_values_for_yaml(v) for k, v in data.items()}
    elif isinstance(data, list):
        return [wrap_string_values_for_yaml(item) for item in data]
    elif isinstance(data, str) and not isinstance(data, ForceDoubleQuoteStr):
        # 只包装普通的 str 对象，避免重复包装
        return ForceDoubleQuoteStr(data)
    return data


# --- 自定义部分结束 ---


def format_chat_history_for_prompt(raw_messages_from_db: list[dict[str, Any]]) -> str:
    """
    将从 ArangoDBHandler 获取的原始消息列表格式化为类似 input_test.md 的 YAML 字符串。
    限制：每个会话窗口最多20条消息，且消息在调用此函数时的最近10分钟内。
    时间戳处理：
    - 期望从数据库接收的 msg["timestamp"] 是 UTC 毫秒级数值。
    - 如果接收到的是字符串 (兼容旧数据)，会尝试按ISO格式解析。
    - 当前时间以UTC为准进行筛选。
    - 输出到YAML的时间是 'YYYY-MM-DD HH:MM:SS' 格式的UTC时间字符串。
    """
    if not raw_messages_from_db:
        return "在指定的时间范围内没有找到聊天记录。"

    grouped_messages = defaultdict(lambda: {"group_info": {}, "user_info": {}, "chat_history": []})

    desired_history_span_minutes = 10
    max_messages_per_group = 20

    # 获取当前的UTC时间，作为筛选基准
    current_utc_time = datetime.datetime.now(datetime.UTC)
    # 计算10分钟前的时间点 (UTC)
    span_cutoff_timestamp_utc = current_utc_time - datetime.timedelta(minutes=desired_history_span_minutes)

    # 当数据库中的时间戳是naive字符串（无时区信息）时，我们假定它所属的时区。
    # 主要用于兼容可能存在的旧数据或意外的字符串输入。
    assumed_local_tz_for_fallback = datetime.timezone(datetime.timedelta(hours=8))  # 例如 CST UTC+8

    for msg in raw_messages_from_db:
        msg_time_input = msg.get("timestamp")  # 期望是数值型UTC毫秒

        if msg_time_input is None:
            print(f"警告: 消息 {msg.get('message_id', '未知ID')} 缺少时间戳，已跳过。")
            continue

        parsed_msg_time_utc: datetime.datetime
        try:
            if isinstance(msg_time_input, int | float):
                # 是数值型时间戳 (int or float), 假设是UTC毫秒
                msg_time_seconds_utc = msg_time_input / 1000.0
                parsed_msg_time_utc = datetime.datetime.fromtimestamp(msg_time_seconds_utc, datetime.UTC)
            elif isinstance(msg_time_input, str):
                # 为了兼容可能存在的旧数据或意外的字符串输入，尝试按ISO字符串解析
                print(
                    f"警告: 消息 {msg.get('message_id', '未知ID')} 的时间戳是字符串 '{msg_time_input}' "
                    f"而非预期的数字。尝试按ISO字符串解析。"
                )
                temp_time_str = msg_time_input.replace("Z", "+00:00")  # 确保 fromisoformat 能处理 'Z'
                parsed_msg_time_aware_or_naive = datetime.datetime.fromisoformat(temp_time_str)

                if (
                    parsed_msg_time_aware_or_naive.tzinfo is None
                    or parsed_msg_time_aware_or_naive.tzinfo.utcoffset(parsed_msg_time_aware_or_naive) is None
                ):
                    # 时间戳是 naive (无时区信息)
                    # 假定它是上述定义的 assumed_local_tz_for_fallback
                    print(
                        f"  消息 {msg.get('message_id', '未知ID')} 的 naive 时间戳 '{temp_time_str}' 被假定为时区 {assumed_local_tz_for_fallback}。"
                    )
                    parsed_msg_time_local = parsed_msg_time_aware_or_naive.replace(tzinfo=assumed_local_tz_for_fallback)
                    # 转换为 UTC
                    parsed_msg_time_utc = parsed_msg_time_local.astimezone(datetime.UTC)
                else:
                    # 时间戳已经是 timezone-aware，直接转换为 UTC
                    parsed_msg_time_utc = parsed_msg_time_aware_or_naive.astimezone(datetime.UTC)
            else:
                raise ValueError(f"时间戳类型不支持: {type(msg_time_input)}")

        except ValueError as e_time:
            print(
                f"警告: 处理或解析数据库中的时间戳 '{msg_time_input}' (类型: {type(msg_time_input)}) 失败: {e_time}。"
                f"消息ID: {msg.get('message_id', '未知ID')}，已跳过。"
            )
            continue

        # 进行时间筛选 (所有时间都已转换为UTC进行比较)
        if parsed_msg_time_utc < span_cutoff_timestamp_utc:
            continue  # 跳过早于10分钟窗口的消息 (UTC比较)

        platform = msg.get("platform", "未知平台")
        group_id_val = msg.get("group_id")
        group_key_part = str(group_id_val) if group_id_val is not None else "direct_or_no_group"
        group_key = (platform, group_key_part)

        if not grouped_messages[group_key]["group_info"]:
            group_name = msg.get("group_name")
            if group_id_val is None and not group_name:  # 对于没有 group_id 和 group_name 的情况 (可能是私聊)
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

        sender_id = msg.get("sender_id")
        if sender_id:
            sender_id_str = str(sender_id)
            if sender_id_str not in grouped_messages[group_key]["user_info"]:
                user_details = {
                    "sender_nickname": msg.get("sender_nickname")
                    if msg.get("sender_nickname") is not None
                    else f"用户_{sender_id_str}",
                    "sender_group_permission": msg.get("sender_group_permission"),
                }
                sender_group_card = msg.get("sender_group_card", "")
                if sender_group_card:  # 仅当有值时添加
                    user_details["sender_group_card"] = sender_group_card

                sender_group_titlename = msg.get("sender_group_titlename", "")
                if sender_group_titlename:  # 仅当有值时添加
                    user_details["sender_group_titlename"] = sender_group_titlename

                grouped_messages[group_key]["user_info"][sender_id_str] = user_details

        # 输出到YAML的时间字符串格式，这里我们输出UTC时间。
        formatted_time = parsed_msg_time_utc.strftime("%Y-%m-%d %H:%M:%S")  # 输出UTC时间

        message_content_raw = msg.get("message_content")
        final_message_content = []

        if isinstance(message_content_raw, list):
            final_message_content = [
                segment.copy() if isinstance(segment, dict) else segment  # 确保字典是副本
                for segment in message_content_raw
            ]
        elif isinstance(message_content_raw, dict) and "type" in message_content_raw and "data" in message_content_raw:
            # 单个消息段也包装在列表中
            final_message_content = [message_content_raw.copy()]
        elif isinstance(message_content_raw, str):
            # 如果 message_content 是一个原始字符串，包装为text段
            final_message_content = [{"type": "text", "data": {"text": message_content_raw}}]  # 确保 data 是一个字典
            # print(f"警告: 消息 {msg.get('message_id')} 的 message_content 是一个原始字符串，已包装为text段。") # 可选打印
        else:
            print(
                f"警告: 消息 {msg.get('message_id', '未知ID')} 的 message_content 格式未知 "
                f"({type(message_content_raw)})，将使用默认空文本内容。"
            )
            final_message_content = [{"type": "text", "data": {"text": ""}}]  # 确保 data 是一个字典

        chat_message = {
            "time": formatted_time,  # YAML中的时间将是UTC字符串
            "post_type": msg.get("post_type", "message"),
            "sub_type": msg.get("sub_type"),  # 可能为 None
            "message_id": str(msg.get("message_id", uuid.uuid4())),  # 确保 message_id 是字符串
            "message": final_message_content,
            "sender_id": str(sender_id) if sender_id else "SYSTEM_OR_UNKNOWN",  # 确保 sender_id 是字符串
            "_parsed_timestamp_utc": parsed_msg_time_utc,  # 保留UTC时间戳对象用于排序和截断
        }
        grouped_messages[group_key]["chat_history"].append(chat_message)

    output_list_for_yaml = []
    for (_, _), data in grouped_messages.items():
        current_chat_history = data["chat_history"]

        if current_chat_history:
            # 按UTC时间戳排序 (最旧的在前)
            current_chat_history.sort(key=lambda x: x["_parsed_timestamp_utc"])

            # 应用消息数量限制，取最新的 max_messages_per_group 条消息
            if len(current_chat_history) > max_messages_per_group:
                final_filtered_history = current_chat_history[-max_messages_per_group:]
            else:
                final_filtered_history = current_chat_history

            # 清理临时的UTC时间戳字段
            for msg_to_clean in final_filtered_history:
                del msg_to_clean["_parsed_timestamp_utc"]

            data["chat_history"] = final_filtered_history

            if data["chat_history"]:  # 确保在截断后仍有聊天记录
                group_data_for_yaml = {
                    "group_info": data["group_info"],
                    "user_info": data["user_info"] if data["user_info"] else {},  # 确保 user_info 存在
                    "chat_history": data["chat_history"],
                }
                output_list_for_yaml.append(group_data_for_yaml)

    if not output_list_for_yaml:
        return "在最近10分钟内，或根据数量筛选后，没有有效的聊天记录可供格式化。"

    # 使用自定义的 wrap_string_values_for_yaml 来处理字符串值的双引号问题
    data_to_dump_processed: Any
    if len(output_list_for_yaml) == 1:
        # 单个会话直接输出其内容
        if "user_info" not in output_list_for_yaml[0]:  # 再次确保 user_info 存在
            output_list_for_yaml[0]["user_info"] = {}
        data_to_dump_processed = wrap_string_values_for_yaml(output_list_for_yaml[0])
    else:
        # 多个会话，使用 "chat_sessions" 列表包装
        for item in output_list_for_yaml:  # 再次确保 user_info 存在
            if "user_info" not in item:
                item["user_info"] = {}
        multi_group_structure = {"chat_sessions": output_list_for_yaml}
        data_to_dump_processed = wrap_string_values_for_yaml(multi_group_structure)

    try:
        # 使用自定义的 Dumper
        yaml_content = yaml.dump(
            data_to_dump_processed,
            Dumper=MyDumper,
            allow_unicode=True,
            sort_keys=False,
            indent=2,
            default_flow_style=False,
        )

        if len(output_list_for_yaml) == 1:
            return f"以下是最近的聊天记录及相关内容 (时间以UTC显示)：\n```yaml\n{yaml_content}```"
        else:
            return f"以下是最近多个会话的聊天记录及相关内容 (时间以UTC显示)：\n```yaml\n{yaml_content}```"

    except Exception as e_yaml:
        print(f"错误: 格式化聊天记录为YAML时出错: {e_yaml}")
        return "格式化聊天记录为YAML时出错。"
