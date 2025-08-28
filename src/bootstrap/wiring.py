# 文件路径: src/bootstrap/wiring.py

from src.bootstrap.container import ServiceContainer
from src.common.custom_logging.logging_config import get_logger

logger = get_logger(__name__)


def wire_dependencies(container: ServiceContainer) -> None:
    """将容器中所有服务的静态依赖关系连接起来."""
    container.core_logic.container = container


async def wire_dynamic_dependencies(container: ServiceContainer) -> None:
    """此函数只负责处理安检后才能确定的简单信息，如 bot_ids."""
    logger.info("动态依赖连接器：开始等待安检完成...")
    await container.core_comm_layer.wait_for_all_inspections()
    logger.info("动态依赖连接器：所有安检已完成。")

    all_self_entities = await container.entity_graph_service.get_all_self_entities()
    bot_ids_map = {
        details.get("platform"): details.get("platform_id")
        for acc in all_self_entities
        if (details := acc.get("details")) and isinstance(details, dict)
    }

    logger.info(f"动态依赖连接器：从数据库获取到 Bot ID Map: {bot_ids_map}")

    container.application_manager.set_self_bot_ids_map(bot_ids_map)
    logger.info("ApplicationManager 的 Bot ID Map 已设置。")


    container.unread_info_service.update_self_bot_ids(bot_ids_map)
    logger.info("UnreadInfoService 的 Bot ID Map 已更新。")
    logger.info("动态依赖连接流程完成。")
