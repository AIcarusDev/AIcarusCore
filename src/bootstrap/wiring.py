# 文件路径: src/bootstrap/wiring.py

from src.bootstrap.container import ServiceContainer
from src.common.custom_logging.logging_config import get_logger

logger = get_logger(__name__)


def wire_dependencies(container: ServiceContainer) -> None:
    """将容器中所有服务的静态依赖关系连接起来."""
    container.prompt_builder.container = container


async def wire_dynamic_dependencies(container: ServiceContainer) -> None:
    """此函数只负责处理安检后才能确定的简单信息，如 bot_ids."""
    logger.info("动态依赖连接器：开始等待安检完成...")
    await container.core_comm_layer.wait_for_all_inspections()
    logger.info("动态依赖连接器：所有安检已完成。")

    # 注意：bot_id 的设置现在由每个 app builder 的 run_on_connect_inspection 内部处理
    # ApplicationManager 会自动收集这些信息。
    # 这里我们只是验证一下结果。
    bot_ids_map = container.application_manager.get_self_bot_ids_map()

    logger.info(f"动态依赖连接器：从 ApplicationManager 获取到 Bot ID Map: {bot_ids_map}")
    logger.info("动态依赖连接流程完成。")
