from .config_manager import get_typed_settings

# 加载类型化配置
config = get_typed_settings()

# 现在你可以通过 config 对象访问配置项，例如：
# print(config.core_logic.thinking_interval_seconds)
# print(config.database.arangodb.host)
