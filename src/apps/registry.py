# 文件路径: src/apps/registry.py

import importlib
import inspect
from pathlib import Path
from typing import Any

from src.common.custom_logging.logging_config import get_logger

# 导入路径已修正
from src.services.action.components.base_builder import BasePlatformBuilder

logger = get_logger(__name__)


class PlatformBuilderRegistry:
    """平台构建器注册中心，负责发现和管理所有平台构建器实例."""

    def __init__(self) -> None:
        self._builders: dict[str, BasePlatformBuilder] = {}

    def discover_and_register_builders(self, package: Any) -> None:
        """自动扫描 apps 目录下的所有子目录，寻找 builder.py 并注册."""
        logger.info("应用注册中心：正在扫描 apps 目录以发现所有应用...")
        apps_dir = Path(package.__path__[0])

        for app_path in apps_dir.iterdir():
            if not app_path.is_dir():
                continue

            builder_file = app_path / "builder.py"
            if not builder_file.exists():
                continue

            # 构建模块的完整路径，例如: src.apps.qq.builder
            module_name = f"{package.__name__}.{app_path.name}.builder"
            try:
                module = importlib.import_module(module_name)
                for _item_name, item in inspect.getmembers(module, inspect.isclass):
                    if issubclass(item, BasePlatformBuilder) and item is not BasePlatformBuilder:
                        instance = item()
                        platform_id = instance.platform_id
                        if platform_id in self._builders:
                            logger.warning(
                                f"发现重复的应用！平台 '{platform_id}' 被 '{item.__name__}' 覆盖！"
                            )
                        self._builders[platform_id] = instance
                        logger.info(f"应用 '{item.__name__}' 已注册，负责平台：'{platform_id}'")
            except Exception as e:
                logger.error(f"加载或注册应用模块 '{module_name}' 失败: {e}", exc_info=True)

        logger.info(f"应用注册完成，目前共有 {len(self._builders)} 个应用在岗。")

    def get_builder(self, platform_id: str) -> BasePlatformBuilder | None:
        """根据平台ID，获取一个应用构建器."""
        return self._builders.get(platform_id)

    def get_all_builders(self) -> dict[str, BasePlatformBuilder]:
        """返回所有已注册的应用构建器实例."""
        return self._builders.copy()

    def get_all_action_definitions(self) -> dict[str, Any]:
        """获取所有已注册应用提供的动作定义."""
        all_definitions = {}
        for platform_id, builder in self._builders.items():
            all_definitions[platform_id] = {
                "type": "object",
                "description": f"针对 {platform_id} 平台的所有动作。",
                "properties": builder.get_action_definitions(),
            }
        return all_definitions


# 创建一个全局的单例
platform_builder_registry = PlatformBuilderRegistry()
