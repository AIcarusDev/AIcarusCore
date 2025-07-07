# src/platform_builders/registry.py (小色猫·V6.0重塑版)
import importlib
import inspect
import pkgutil
from typing import Any

from src.common.custom_logging.logging_config import get_logger
from src.platform_builders.base_builder import BasePlatformBuilder

logger = get_logger(__name__)


class PlatformBuilderRegistry:
    """平台构建器注册中心，负责发现和管理所有平台构建器实例.

    这个中介所就像是一个翻译官的登记处，负责管理所有平台的翻译官。
    它会自动扫描指定的包，找到所有继承自 BasePlatformBuilder 的类，并实例化它们。
    这样，当需要处理某个平台的请求时，就可以通过平台ID快速找到对应的翻译官。

    Attributes:
        _builders: dict[str, BasePlatformBuilder] - 存储所有已注册的平台构建器实例。
        这个字典的键是平台ID，值是对应的 BasePlatformBuilder 实例。
        这样可以快速通过平台ID获取对应的翻译官实例。
    """

    def __init__(self) -> None:
        self._builders: dict[str, BasePlatformBuilder] = {}

    def discover_and_register_builders(self, package: any) -> None:  # 使用 any 兼容旧的调用
        """自动扫描指定包，把所有翻译官都找出来登记."""
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
        """根据平台ID，找一个翻译官出来干活."""
        return self._builders.get(platform_id)

    def get_all_builders(self) -> dict[str, BasePlatformBuilder]:
        """返回所有已注册的翻译官实例."""
        return self._builders.copy()

    def get_all_action_definitions(self) -> dict[str, Any]:
        """获取所有平台的动作定义.

        返回一个字典，键是平台ID，值是该平台的动作定义。
        每个平台的动作定义是一个字典，包含动作类型、描述和属性等信息。
        这样可以方便地获取所有平台的动作定义，供其他模块使用。

        Returns:
            dict[str, Any]: 包含所有平台动作定义的字典。
            键是平台ID，值是该平台的动作定义。
        """
        all_definitions = {}
        for platform_id, builder in self._builders.items():
            # 我们把每个平台的动作定义，都放在以平台ID为key的子字典里
            all_definitions[platform_id] = {
                "type": "object",
                "description": f"针对 {platform_id} 平台的所有动作。",
                "properties": builder.get_action_definitions(),
            }
        return all_definitions


# 创建一个全局的单例
platform_builder_registry = PlatformBuilderRegistry()
