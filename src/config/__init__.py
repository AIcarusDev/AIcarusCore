from .config_manager import get_typed_settings

# 加载类型化配置
config = get_typed_settings()

# 现在你可以通过 config 对象访问配置项，例如：
# print(config.core_logic.thinking_interval_seconds)
# 数据库相关的配置已经直接从环境变量读取，不再通过config对象访问
