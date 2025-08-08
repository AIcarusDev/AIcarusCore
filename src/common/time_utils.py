# src/common/time_utils.py
from datetime import UTC, datetime


def get_formatted_time_for_llm(now: datetime | None = None) -> str:
    """获取格式化的时间字符串，包含季节信息.

    Args:
        now (datetime | None): 一个datetime对象，如果为None，就用当前时间。

    Returns:
        str: 格式化好的、给LLM看的时间字符串，包含了季节信息。
    """
    if now is None:
        now = datetime.now()

    hour = now.hour
    month = now.month

    # 按照你那个啰嗦的规矩来判断时间段
    if 0 <= hour < 5:
        period = "凌晨"
    elif 5 <= hour < 8:
        period = "清晨"
    elif 8 <= hour < 11:
        period = "上午"
    elif 11 <= hour < 13:
        period = "中午"
    elif 13 <= hour < 17:
        period = "下午"
    elif 17 <= hour < 19:
        period = "傍晚"
    elif 19 <= hour < 22:
        period = "晚上"
    else:  # 22 <= hour < 24
        period = "深夜"

    # 好吧，看在你这么要求的份上，季节也给你加上好了。
    # (这里是按照北半球的常规划分)
    if 3 <= month <= 5:
        season = "春天"
    elif 6 <= month <= 8:
        season = "夏天"
    elif 9 <= month <= 11:
        season = "秋天"
    else:  # 12, 1, 2
        season = "冬天"

    # 喏，你想要的更详细的格式，满意了吧？
    return (
        f"现在是{now.year}年的{season}，{now.month}月{now.day}日，"
        f"{period}{now.hour}点{now.minute}分"
    )


def format_relative_time(past_timestamp_ms: int) -> str:
    """将过去的毫秒时间戳转换为易于理解的相对时间字符串.

    Args:
        past_timestamp_ms: 过去的毫秒级时间戳 (UTC)。

    Returns:
        一个描述相对时间的字符串，例如 "刚刚", "5分钟前", "3小时前", "昨天"。
    """
    if past_timestamp_ms <= 0:
        return "很久以前"

    now_utc = datetime.now(UTC)
    past_time_utc = datetime.fromtimestamp(past_timestamp_ms / 1000.0, tz=UTC)

    delta = now_utc - past_time_utc

    seconds = delta.total_seconds()

    if seconds < 10:
        return "刚刚"
    if seconds < 60:
        return f"{int(seconds)}秒前"

    minutes = seconds / 60
    if minutes < 60:
        return f"{int(minutes)}分钟前"

    hours = minutes / 60
    if hours < 24:
        return f"{int(hours)}小时前"

    days = hours / 24
    if days < 2:
        return "昨天"
    if days < 30:
        return f"{int(days)}天前"

    months = days / 30
    if months < 12:
        return f"{int(months)}个月前"

    years = days / 365
    return f"{int(years)}年前"

def format_relative_time_for_attention_log(
        past_timestamp_ms: int,
        current_timestamp_ms: int
    ) -> str:
    """为注意力日志专门设计的、人性化的相对时间格式化函数.

    Args:
        past_timestamp_ms: 历史事件的毫秒时间戳。
        current_timestamp_ms: 当前时间的毫秒时间戳，作为比较基准。

    Returns:
        一个模糊的、人性化的时间描述字符串。
    """
    if past_timestamp_ms <= 0:
        return "很久以前"

    delta_seconds = (current_timestamp_ms - past_timestamp_ms) / 1000.0
    delta_minutes = delta_seconds / 60

    if delta_minutes < 1:
        return "刚才"
    if 30 <= delta_minutes <= 40:
        return "约半小时前"
    if 50 <= delta_minutes < 60:
        return "约1小时前"

    if delta_minutes >= 60:
        hours = round(delta_minutes / 60)
        return f"约{hours}小时前"

    # 默认情况
    return f"{round(delta_minutes)}分钟前"




def format_relative_time_for_memory(past_timestamp_ms: int) -> str:
    """针对记忆操作，将过去的毫秒时间戳转换为易于理解的相对时间字符串.

    Args:
        past_timestamp_ms: 过去的毫秒级时间戳 (UTC)。

    Returns:
        一个描述相对时间的字符串，例如 "刚刚", "5分钟前", "3小时前", "昨天"。
    """
    # TODO: 此函数旨在为“记忆”提供更模糊或更人性化的时间描述，
    # 例如“片刻之前”、“不久前”、“几天前”。
    # 目前尚未实现，先明确抛出异常。
    raise NotImplementedError("format_relative_time_for_memory 函数尚未实现。")
