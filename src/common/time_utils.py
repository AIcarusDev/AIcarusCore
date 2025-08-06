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

def format_relative_time_for_memory(past_timestamp_ms: int) -> str:
    """针对记忆操作，将过去的毫秒时间戳转换为易于理解的相对时间字符串.

    Args:
        past_timestamp_ms: 过去的毫秒级时间戳 (UTC)。

    Returns:
        一个描述相对时间的字符串，例如 "刚刚", "5分钟前", "3小时前", "昨天"。
    """
    pass
