# src/common/weather_utils.py
# 这是一个获取目前地区天气的公用工具
# 需要安装 python-weather 库，目前暂时未启用
# import python_weather
# from src.config import aicarus_configs

# def get_weather_for_llm(location: str = "Beijing") -> str:
    # """
    # 获取指定位置的天气信息，并格式化为适合LLM使用的字符串。

    # Args:
        # location (str): 需要查询天气的地点，默认为 "Beijing"。

    # Returns:
        # str: 格式化后的天气信息字符串。
    # """
    # try:
        # client = python_weather.Client(format=python_weather.IMPERIAL)
        # weather = client.get(location)
        # client.close()

        # if not weather.current:
            # return f"无法获取 {location} 的天气信息。"

        # current = weather.current
        # return (
            # f"当前{location}的天气情况：\n"
            # f"温度：{current.temperature}°F\n"
            # f"湿度：{current.humidity}%\n"
            # f"风速：{current.wind_speed} mph\n"
            # f"天气状况：{current.sky_text}"
        # )
    # except Exception as e:
        # return f"获取 {location} 天气时发生错误: {e}"
