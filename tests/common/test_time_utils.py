# tests/common/test_time_utils.py
from datetime import datetime

from src.common.time_utils import get_formatted_time_for_llm

# pytest 会自动发现并执行以 "test_" 开头的函数


def test_get_formatted_time_for_llm_morning() -> None:
    """测试上午时间的格式化."""
    # 构造一个特定的时间点
    test_time = datetime(2025, 3, 15, 9, 30)
    # 调用被测试的函数
    formatted_str = get_formatted_time_for_llm(test_time)
    # 断言结果是否符合预期
    assert formatted_str == "现在是2025年的春天，3月15日，上午9点30分"


def test_get_formatted_time_for_llm_afternoon_summer() -> None:
    """测试夏天下午时间的格式化."""
    test_time = datetime(2025, 7, 20, 14, 5)
    formatted_str = get_formatted_time_for_llm(test_time)
    assert formatted_str == "现在是2025年的夏天，7月20日，下午14点5分"


def test_get_formatted_time_for_llm_deep_night_winter() -> None:
    """测试冬天深夜时间的格式化."""
    test_time = datetime(2025, 12, 25, 23, 59)
    formatted_str = get_formatted_time_for_llm(test_time)
    assert formatted_str == "现在是2025年的冬天，12月25日，深夜23点59分"
