# tests/common/test_time_utils.py
from datetime import datetime

from pytest_mock import MockerFixture
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


def test_get_formatted_time_for_llm_midnight() -> None:
    """测试午夜 (00:00) 边界情况."""
    test_time = datetime(2025, 1, 1, 0, 0)
    formatted_str = get_formatted_time_for_llm(test_time)
    assert formatted_str == "现在是2025年的冬天，1月1日，凌晨0点0分"


def test_get_formatted_time_for_llm_noon() -> None:
    """测试中午 (12:00) 边界情况."""
    test_time = datetime(2025, 6, 1, 12, 0)
    formatted_str = get_formatted_time_for_llm(test_time)
    assert formatted_str == "现在是2025年的夏天，6月1日，中午12点0分"


def test_get_formatted_time_for_llm_evening_boundary() -> None:
    """测试傍晚 (17:00) 边界情况."""
    test_time = datetime(2025, 9, 1, 17, 0)
    formatted_str = get_formatted_time_for_llm(test_time)
    assert formatted_str == "现在是2025年的秋天，9月1日，傍晚17点0分"


def test_get_formatted_time_for_llm_no_input_uses_now(mocker: MockerFixture) -> None:
    """测试在没有输入时，函数是否正确调用 datetime.now()."""
    # 1. 创建一个固定的时间点，用于模拟 datetime.now() 的返回值
    mock_now = datetime(2025, 10, 31, 5, 0)

    # 2. 使用 mocker "patch" time_utils 模块中的 datetime 对象
    #    这样当函数内部调用 datetime.now() 时，会得到我们预设的 mock_now
    mocker.patch("src.common.time_utils.datetime.now", return_value=mock_now)

    # 3. 在不传递任何参数的情况下调用函数
    formatted_str = get_formatted_time_for_llm()

    # 4. 断言结果是否与我们模拟的时间点匹配
    assert formatted_str == "现在是2025年的秋天，10月31日，清晨5点0分"
