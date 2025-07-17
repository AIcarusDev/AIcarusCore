# src/bootstrap/wiring.py
from src.bootstrap.container import ServiceContainer
from src.focus_chat_mode.chat_session_manager import ChatSessionManager
from src.config import config

def wire_dependencies(container: ServiceContainer):
    """将容器中所有服务的依赖关系连接起来."""

    # 获取 ActionSender 实例，它在 CoreWebsocketServer 内部
    action_sender = container.core_comm_layer.action_sender

    # 连接 ActionHandler 的依赖
    container.action_handler.set_dependencies(
        thought_service=container.thought_storage_service,
        event_service=container.event_storage_service,
        action_log_service=container.action_log_service,
        conversation_service=container.conversation_storage_service,
        action_sender=action_sender,
        chat_session_manager=container.chat_session_manager, # 此时还是 None
        core_logic=container.core_logic,
        person_service=container.person_storage_service,
    )
    container.action_handler.set_thought_trigger(container.core_logic.immediate_thought_trigger)

    # 连接 CoreLogic 的依赖 (ChatSessionManager 稍后连接)
    # core_logic 已经在 __init__ 中获取了大部分依赖

    # 连接 MessageProcessor 的依赖 (ChatSessionManager 稍后连接)
    container.message_processor.core_comm_layer = container.core_comm_layer
    container.message_processor.core_logic = container.core_logic

    # ... 其他需要后期注入的简单依赖

async def wire_dynamic_dependencies(container: ServiceContainer):
    """
    处理动态依赖，特指 ChatSessionManager，它需要在安检后创建。
    这个函数会在系统启动后被调用。
    """
    # 1. 等待安检完成
    await container.core_comm_layer.wait_for_all_inspections()

    # 2. 获取安检后的 bot_ids
    all_self_accounts = await container.person_storage_service.get_all_self_accounts()
    bot_ids_map = {acc["platform"]: acc["platform_id"] for acc in all_self_accounts} if all_self_accounts else {}

    # 3. 创建并注入 ChatSessionManager
    if config.focus_chat_mode.enabled:
        chat_session_manager = ChatSessionManager(
            config=config.focus_chat_mode,
            llm_client=container.focused_chat_llm_client,
            event_storage=container.event_storage_service,
            action_handler=container.action_handler,
            self_bot_ids_map=bot_ids_map,
            conversation_service=container.conversation_storage_service,
            summarization_service=container.summarization_service,
            summary_storage_service=container.summary_storage_service,
            intelligent_interrupter=container.intelligent_interrupter,
            thought_storage_service=container.thought_storage_service,
            internal_info_builder=container.internal_info_builder,
            core_logic=container.core_logic
        )
        container.chat_session_manager = chat_session_manager

        # 4. 回填所有依赖 ChatSessionManager 的服务
        container.core_logic.chat_session_manager = chat_session_manager
        container.action_handler.chat_session_manager = chat_session_manager
        container.prompt_builder.chat_session_manager = chat_session_manager
        container.message_processor.qq_chat_session_manager = chat_session_manager

    # 5. 更新 UnreadInfoService
    container.unread_info_service.update_self_bot_ids(bot_ids_map)