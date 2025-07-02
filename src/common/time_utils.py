# src/common/time_utils.py
from datetime import datetime


# 这是个时间处理的工具模块，整个项目都可以用来获取格式化的时间字符串，并且定义了具体的时间段和季节名称。
def get_formatted_time_for_llm(now: datetime | None = None) -> str:
    """
    就...就是根据你那个麻烦的规则生成时间字符串啦，还给你加上了季节！
    如果不给我时间，我就用现在的时间，哼。

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
    return f"现在是{now.year}年{season}{now.month}月{now.day}日，{period}{now.hour}点{now.minute}分"
