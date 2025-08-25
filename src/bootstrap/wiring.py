# src/bootstrap/wiring.py
from src.bootstrap.container import ServiceContainer
from src.config import config
from src.focus_chat_mode.chat_session_manager import ChatSessionManager


def wire_dependencies(container: ServiceContainer) -> None:
    """将容器中所有服务的依赖关系连接起来."""
    action_sender = container.core_comm_layer.action_sender

    # 连接 ActionHandler 的依赖
    container.action_handler.set_dependencies(
        thought_service=container.thought_storage_service,
        event_service=container.event_storage_service,
        action_log_service=container.action_log_service,
        action_sender=action_sender,
        chat_session_manager=container.chat_session_manager,
        core_logic=container.core_logic,
        entity_service=container.entity_graph_service,
        sticker_service=container.sticker_service,  # <-- 确保只传入新的 service，没有旧的
        narrative_vectorizer=container.narrative_vectorizer,
    )
    container.action_handler.set_thought_trigger(container.core_logic.immediate_thought_trigger)

    # 连接 MessageProcessor 的依赖
    container.message_processor.core_comm_layer = container.core_comm_layer
    container.message_processor.core_logic = container.core_logic


async def wire_dynamic_dependencies(container: ServiceContainer) -> None:
    """处理动态依赖，特指 ChatSessionManager，它需要在安检后创建和注入."""
    # 1. 等待安检完成
    await container.core_comm_layer.wait_for_all_inspections()

    # 2. 获取安检后的 bot_ids
    all_self_entities = await container.entity_graph_service.get_all_self_entities()
    # self_bot_ids_map 的构建逻辑需要适配新的实体结构
    bot_ids_map = (
        {
            acc.get("details", {}).get("platform"): acc.get("details", {}).get("platform_id")
            for acc in all_self_entities
            if isinstance(acc, dict)
            and acc.get("details", {}).get("platform")
            and acc.get("details", {}).get("platform_id")
        }
        if all_self_entities
        else {}
    )

    # 将 bot_ids_map 注入 ApplicationManager
    container.application_manager.set_self_bot_ids_map(bot_ids_map)

    # 3. 创建并注入 ChatSessionManager
    if config.focus_chat_mode.enabled and container.focused_chat_llm_client:
        # 创建 ChatSessionManager
        chat_session_manager = ChatSessionManager(
            config=config.focus_chat_mode,
            llm_client=container.focused_chat_llm_client,
            deliberation_llm_client=container.deliberation_llm_client,
            event_storage=container.event_storage_service,
            action_handler=container.action_handler,
            self_bot_ids_map=bot_ids_map,
            intelligent_interrupter=container.intelligent_interrupter,
            thought_storage_service=container.thought_storage_service,
            internal_info_builder=container.internal_info_builder,
            core_logic=container.core_logic,
            entity_graph_service=container.entity_graph_service,
        )
        container.chat_session_manager = chat_session_manager

        # 4. 回填所有依赖 ChatSessionManager 的服务
        container.core_logic.chat_session_manager = chat_session_manager
        container.action_handler.chat_session_manager = chat_session_manager
        container.prompt_builder.chat_session_manager = chat_session_manager
        container.message_processor.qq_chat_session_manager = chat_session_manager

    # 5. 更新 UnreadInfoService
    container.unread_info_service.update_self_bot_ids(bot_ids_map)
