# 中介所，负责管所有的翻译官
import importlib
import inspect
import pkgutil
from typing import Any

from src.common.custom_logging.logging_config import get_logger
from src.platform_builders.base_builder import BasePlatformBuilder

logger = get_logger(__name__)


class PlatformBuilderRegistry:
    def __init__(self) -> None:
        self._builders: dict[str, BasePlatformBuilder] = {}

    def discover_and_register_builders(self, package: pkgutil.ModuleInfo) -> None:
        """
        自动扫描指定包（就是那个 platform_builders 文件夹），把所有翻译官都找出来登记。
        """
        logger.info("中介所开门了，正在寻找所有持证上岗的翻译官...")
        for _, name, _ in pkgutil.iter_modules(package.__path__, package.__name__ + "."):
            if name.endswith("_builder"):
                module = importlib.import_module(name)
                for item_name, item in inspect.getmembers(module, inspect.isclass):
                    if issubclass(item, BasePlatformBuilder) and item is not BasePlatformBuilder:
                        try:
                            instance = item()
                            platform_id = instance.platform_id
                            if platform_id in self._builders:
                                logger.warning(
                                    f"发现重复的翻译官！平台'{platform_id}'的翻译官被'{item.__name__}'覆盖了！"
                                )
                            self._builders[platform_id] = instance
                            logger.info(f"翻译官'{item.__name__}'已登记，负责平台：'{platform_id}'")
                        except Exception as e:
                            logger.error(f"实例化或注册翻译官'{item_name}'失败: {e}", exc_info=True)
        logger.info(f"中介所登记完毕，目前共有 {len(self._builders)} 位翻译官在岗。")

    def get_builder(self, platform_id: str) -> BasePlatformBuilder | None:
        """根据平台ID，找一个翻译官出来干活。"""
        return self._builders.get(platform_id)

    def get_all_schemas_for_llm(self) -> list[dict[str, Any]]:
        """把所有在岗翻译官的功能说明书都收上来，打包给LLM。"""
        all_schemas = []
        for builder in self._builders.values():
            all_schemas.extend(builder.get_action_schema_for_llm())
        return all_schemas


# 创建一个全局的单例，大家共用这一个中介所
platform_builder_registry = PlatformBuilderRegistry()
