# AIcarusCore/src/common/utils.py
import datetime
import uuid
from collections import defaultdict
from typing import Any, Tuple, List
import mimetypes # 导入mimetypes用于从文件名推断类型

import yaml

from src.common.custom_logging.logger_manager import get_logger # type: ignore

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
    def extract_text_content(content: list[dict], image_placeholder_key: str = "llm_image_placeholder", image_placeholder_value: str = "[IMAGE_HERE]") -> Tuple[List[dict], List[str]]:
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
                    "original_file_id": seg_data.get("file_id")
                }
                if seg_data.get("filename"):
                    current_segment_for_yaml["data"]["original_filename"] = seg_data.get("filename")

                img_base64 = seg_data.get("base64")
                img_file_id = seg_data.get("file_id") # 用于推断mimetype
                filename_from_data = seg_data.get("filename") # 也可用于推断mimetype

                image_source_to_add = None
                if img_base64 and isinstance(img_base64, str) and img_base64.strip(): # 确保base64存在且非空
                    mimetype = "image/jpeg" # 默认
                    filename_for_mimetype = img_file_id or filename_from_data
                    if filename_for_mimetype and isinstance(filename_for_mimetype, str):
                        guessed_mimetype, _ = mimetypes.guess_type(filename_for_mimetype)
                        if guessed_mimetype:
                            mimetype = guessed_mimetype
                        else:
                            logger.debug(f"无法从文件名 '{filename_for_mimetype}' 推断mimetype，将使用默认值: {mimetype}")
                    else:
                        logger.debug(f"图片缺少file_id和filename，无法推断mimetype，将使用默认值: {mimetype}")

                    # 确保base64字符串是纯数据，而不是已经包含 "data:[mimetype];base64," 前缀
                    # 有些情况下，传入的base64可能已经是完整的data URI
                    if img_base64.startswith("data:image"):
                        image_source_to_add = img_base64 # 已经是data URI
                    else:
                        # 确保base64字符串的padding正确，虽然通常不需要手动处理
                        # missing_padding = len(img_base64) % 4
                        # if missing_padding:
                        #    img_base64 += '=' * (4 - missing_padding)
                        image_source_to_add = f"data:{mimetype};base64,{img_base64}"
                    
                    image_sources_for_llm.append(image_source_to_add)
                    logger.debug(f"成功从base64为图片 '{filename_for_mimetype or '未知图片'}' 构建data URI: {image_source_to_add[:70]}...")
                else:
                    # 如果没有base64数据，记录一下，这个图片将不会被发送给LLM
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
        if url: data["url"] = url
        if base64_data: data["base64"] = base64_data
        return {"type": "image", "data": data}


# 文件: src/common/utils.py

def format_messages_for_llm_context(
    raw_messages_from_db: list[dict[str, Any]],
    style: str = 'yaml',  # 新增参数，可以是 'yaml' 或 'simple'
    image_placeholder_key: str = "llm_image_placeholder",
    image_placeholder_value: str = "[IMAGE_HERE]",
    desired_history_span_minutes: int = 10,
    max_messages_per_group: int = 20
) -> Tuple[str, List[str]]:
    """
    一个统一的函数，用于将从数据库获取的原始消息列表，根据指定的风格格式化为LLM的上下文。

    Args:
        raw_messages_from_db: 从数据库获取的原始消息文档列表。
        style: 格式化风格。'yaml' (默认) 会生成详细的YAML格式；'simple' 会生成简洁的 "时间 角色: 内容" 格式。

    Returns:
        一个元组: (格式化后的字符串, 提取出的图片源列表)
    """
    all_extracted_image_sources: List[str] = []

    if not raw_messages_from_db:
        if style == 'simple':
            return "你和电脑主人之间最近没有聊天记录。", []
        else:
            return "在指定的时间范围内没有找到聊天记录。", []

    # --- 简单风格处理 (给 master_chat_context 用) ---
    if style == 'simple':
        formatted_lines = []
        sorted_messages = sorted(raw_messages_from_db, key=lambda msg: msg.get("timestamp", 0))
        for msg in sorted_messages:
            try:
                ts = msg.get("timestamp", 0) / 1000.0
                dt_object = datetime.datetime.fromtimestamp(ts)
                time_str = dt_object.strftime("%Y年%m月%d日 %H点%M分")
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
                    continue
                role = "未知"
                event_type = msg.get("event_type", "")
                if event_type == "message.master.input":
                    role = "主人（电脑主人）"
                elif event_type == "message.master.output":
                    role = "你"
                formatted_lines.append(f"{time_str} {role}：{text_content}")
            except Exception as e:
                logger.warning(f"格式化(simple)单条聊天记录时出错: {e}, 消息ID: {msg.get('event_id', '未知ID')}")
                continue
        if not formatted_lines:
            return "最近的对话中没有有效的文本消息。", []
        return "\n".join(formatted_lines), [] # 简单模式不处理图片，所以返回空列表

    # --- YAML 风格处理 (给 recent_contextual_information 用) ---
    elif style == 'yaml':
        # (这部分代码就是你原来 format_chat_history_for_prompt 的逻辑，我直接搬过来了)
        grouped_messages = defaultdict(lambda: {"group_info": {}, "user_info": {}, "chat_history": []})
        current_utc_time = datetime.datetime.now(datetime.UTC)
        span_cutoff_timestamp_utc = current_utc_time - datetime.timedelta(minutes=desired_history_span_minutes)
        assumed_local_tz_for_fallback = datetime.timezone(datetime.timedelta(hours=8))

        for msg in raw_messages_from_db:
            if not isinstance(msg, dict): continue
            msg_time_input = msg.get("timestamp") or msg.get("time")
            if msg_time_input is None: continue
            
            try:
                if isinstance(msg_time_input, int | float):
                    parsed_msg_time_utc = datetime.datetime.fromtimestamp(msg_time_input / 1000.0, datetime.UTC)
                elif isinstance(msg_time_input, str):
                    temp_time_str = msg_time_input.replace("Z", "+00:00")
                    try:
                        parsed_msg_time_aware_or_naive = datetime.datetime.fromisoformat(temp_time_str)
                    except ValueError:
                        if ' ' in temp_time_str and '.' not in temp_time_str:
                            parsed_msg_time_aware_or_naive = datetime.datetime.strptime(temp_time_str, "%Y-%m-%d %H:%M:%S%z" if "+" in temp_time_str or "-" in temp_time_str[10:] else "%Y-%m-%d %H:%M:%S")
                        else: raise
                    if parsed_msg_time_aware_or_naive.tzinfo is None or parsed_msg_time_aware_or_naive.tzinfo.utcoffset(parsed_msg_time_aware_or_naive) is None:
                        parsed_msg_time_utc = parsed_msg_time_aware_or_naive.replace(tzinfo=assumed_local_tz_for_fallback).astimezone(datetime.UTC)
                    else:
                        parsed_msg_time_utc = parsed_msg_time_aware_or_naive.astimezone(datetime.UTC)
                else: raise ValueError(f"时间戳类型不支持: {type(msg_time_input)}")
            except ValueError: continue

            if parsed_msg_time_utc < span_cutoff_timestamp_utc: continue

            platform = msg.get("platform", "未知平台")
            conversation_info = msg.get("conversation_info", {})
            group_id_val = conversation_info.get("conversation_id") if isinstance(conversation_info, dict) else msg.get("group_id")
            group_key_part = str(group_id_val) if group_id_val is not None else "direct_or_no_group"
            group_key = (platform, group_key_part)

            if not grouped_messages[group_key]["group_info"]:
                group_name = conversation_info.get("name") if isinstance(conversation_info, dict) else None
                if group_name is None: group_name = msg.get("group_name")
                if group_id_val is None and not group_name:
                    user_info_for_group_name = msg.get("user_info", {})
                    sender_nick_for_group_name = (user_info_for_group_name.get("user_nickname") if isinstance(user_info_for_group_name, dict) else None) or msg.get("sender_nickname", "未知用户")
                    group_name = f"与 {sender_nick_for_group_name} 的对话" if sender_nick_for_group_name != "未知用户" else "直接消息"
                grouped_messages[group_key]["group_info"] = {
                    "platform_name": platform, "group_id": str(group_id_val) if group_id_val is not None else None,
                    "group_name": group_name if group_name is not None else ("未知群名" if group_id_val is not None else "直接消息会话"),
                }

            user_info = msg.get("user_info", {})
            sender_id = (user_info.get("user_id") if isinstance(user_info, dict) else None) or (msg.get("sender_id") or msg.get("user_id"))
            if sender_id:
                sender_id_str = str(sender_id)
                if sender_id_str not in grouped_messages[group_key]["user_info"]:
                    user_details = {}
                    if isinstance(user_info, dict):
                        user_details = {"sender_nickname": user_info.get("user_nickname") or f"用户_{sender_id_str}", "sender_group_permission": user_info.get("permission_level") or user_info.get("role")}
                        for new_field, old_field in [("user_cardname", "sender_group_card"), ("user_titlename", "sender_group_titlename")]:
                            value = user_info.get(new_field, "")
                            if value: user_details[old_field] = value
                    else:
                        user_details = {"sender_nickname": msg.get("sender_nickname") or f"用户_{sender_id_str}", "sender_group_permission": msg.get("sender_group_permission")}
                        for optional_field in ["sender_group_card", "sender_group_titlename"]:
                            value = msg.get(optional_field, "")
                            if value: user_details[optional_field] = value
                    grouped_messages[group_key]["user_info"][sender_id_str] = user_details
            
            formatted_time = parsed_msg_time_utc.strftime("%Y-%m-%d %H:%M:%S")
            message_content_raw = msg.get("content")
            if message_content_raw is None: message_content_raw = msg.get("message_content")

            content_list_to_process = []
            if isinstance(message_content_raw, list):
                content_list_to_process = message_content_raw
            elif isinstance(message_content_raw, dict) and "type" in message_content_raw:
                content_list_to_process = [message_content_raw]
            elif isinstance(message_content_raw, str):
                content_list_to_process = [{"type": "text", "data": {"text": message_content_raw}}]
            elif message_content_raw is None:
                content_list_to_process = [{"type": "text", "data": {"text": "[空消息]"}}]
            else:
                logger.warning(f"警告: 消息内容格式未知: {type(message_content_raw)} for msg_id: {msg.get('event_id', msg.get('message_id', '未知ID'))}")
                content_list_to_process = [{"type": "text", "data": {"text": "[未识别的消息格式]"}}]
                
            final_message_segments_for_yaml, message_images_for_llm_this_message = MessageContentProcessor.extract_text_content(
                content_list_to_process,
                image_placeholder_key=image_placeholder_key,
                image_placeholder_value=image_placeholder_value
            )
            all_extracted_image_sources.extend(message_images_for_llm_this_message)

            chat_message = {
                "time": formatted_time, "event_type": msg.get("event_type", "unknown"),
                "message": final_message_segments_for_yaml,
                "sender_id": str(sender_id) if sender_id else "SYSTEM_OR_UNKNOWN",
                "_parsed_timestamp_utc": parsed_msg_time_utc,
            }
            grouped_messages[group_key]["chat_history"].append(chat_message)

        output_list_for_yaml = []
        for (_, _), data in grouped_messages.items():
            current_chat_history = data["chat_history"]
            if current_chat_history:
                current_chat_history.sort(key=lambda x: x["_parsed_timestamp_utc"])
                final_filtered_history = current_chat_history[-max_messages_per_group:] if len(current_chat_history) > max_messages_per_group else current_chat_history
                for msg_to_clean in final_filtered_history: del msg_to_clean["_parsed_timestamp_utc"]
                data["chat_history"] = final_filtered_history
                if data["chat_history"]:
                    output_list_for_yaml.append({
                        "group_info": data["group_info"],
                        "user_info": data["user_info"] or {}, "chat_history": data["chat_history"],
                    })

        if not output_list_for_yaml:
            return "在最近10分钟内，或根据数量筛选后，没有有效的聊天记录可供格式化。", []

        try:
            data_to_dump_processed = wrap_string_values_for_yaml(output_list_for_yaml[0] if len(output_list_for_yaml) == 1 else {"chat_sessions": output_list_for_yaml})
            yaml_content = yaml.dump(
                data_to_dump_processed, Dumper=MyDumper, allow_unicode=True, sort_keys=False, indent=2, default_flow_style=False
            )
            prefix = "以下是最近的聊天记录及相关内容" if len(output_list_for_yaml) == 1 else "以下是最近多个会话的聊天记录及相关内容"
            return f"{prefix} (时间以UTC显示，图片在聊天记录中以 '{image_placeholder_key}: {image_placeholder_value}' 形式标记)：\n```yaml\n{yaml_content}```", all_extracted_image_sources
        except Exception as e_yaml:
            logger.error(f"错误: 格式化聊天记录为YAML时出错: {e_yaml}", exc_info=True)
            return "格式化聊天记录为YAML时出错。", []
    
    # 如果传入了未知的 style
    return "错误的格式化风格。", []