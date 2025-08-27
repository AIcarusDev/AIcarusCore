# 文件路径: src/bootstrap/wiring.py

from src.apps.qq.qq_chat_session_manager import ChatSessionManager
from src.bootstrap.container import ServiceContainer
from src.common.custom_logging.logging_config import get_logger
from src.config import config

logger = get_logger(__name__)


def wire_dependencies(container: ServiceContainer) -> None:
    """将容器中所有服务的静态依赖关系连接起来."""
    # 目前大部分依赖在 builder 中通过构造函数注入，这里可以留空或用于连接非构造函数注入的依赖
    pass


async def wire_dynamic_dependencies(container: ServiceContainer) -> None:
    """处理动态依赖，特指 ChatSessionManager，它需要在安检后创建和注入."""
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

    if config.focus_chat_mode.enabled and container.focused_chat_llm_client:
        chat_session_manager = ChatSessionManager(
            config=config.focus_chat_mode,
            llm_client=container.focused_chat_llm_client,
            event_storage=container.event_storage_service,
            action_handler=container.action_handler,
            self_bot_ids_map=bot_ids_map,
            intelligent_interrupter=container.intelligent_interrupter,
            thought_storage_service=container.thought_storage_service,
            internal_info_builder=container.internal_info_builder,
            entity_graph_service=container.entity_graph_service,
            core_logic=container.core_logic,
            deliberation_service=container.deliberation_service # 注入 deliberation_service
        )
        container.chat_session_manager = chat_session_manager
        logger.info("ChatSessionManager 实例已创建。")

        # 回填所有依赖 ChatSessionManager 的服务
        container.core_logic.chat_session_manager = chat_session_manager
        container.action_handler.set_dynamic_dependencies(
            chat_session_manager=chat_session_manager,
            core_logic=container.core_logic,
            trigger_event=container.core_logic.immediate_thought_trigger
        )
        container.message_processor.qq_chat_session_manager = chat_session_manager
        logger.info(
            "已将 ChatSessionManager 注入到 CoreLogic, ActionHandler, 和 MessageProcessor。"
            )

    container.unread_info_service.update_self_bot_ids(bot_ids_map)
    logger.info("UnreadInfoService 的 Bot ID Map 已更新。")
    logger.info("动态依赖连接流程完成。")
