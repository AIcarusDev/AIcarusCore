# src/core_logic/context_builder.py
from typing import TYPE_CHECKING, Any

from src.common.custom_logging.logging_config import get_logger
from src.common.utils import format_messages_for_llm_context, format_platform_status_summary
from src.config import config

if TYPE_CHECKING:
    from src.core_communication.core_ws_server import CoreWebsocketServer
    from src.core_logic.state_manager import AIStateManager
    from src.database.services.event_storage_service import EventStorageService

logger = get_logger(__name__)


class ContextBuilder:
    """负责从各种来源收集上下文信息，并格式化以供核心思考循环使用.

    这个类主要用于收集和格式化上下文信息，以便在核心思考循环中使用.

    Attributes:
        event_storage (EventStorageService): 用于存储和获取事件的服务实例.
        core_comm (CoreWebsocketServer): 用于获取实时连接状态的核心通信服务器实例.
        state_manager (AIStateManager): 用于获取当前状态的状态管理器实例.
    """

    def __init__(
        self,
        event_storage: "EventStorageService",
        core_comm: "CoreWebsocketServer",
        state_manager: "AIStateManager",
    ) -> None:
        self.event_storage = event_storage
        self.core_comm = core_comm
        self.state_manager = state_manager
        logger.info("ContextBuilder 已初始化。")

    # --- 小色猫的净化仪式！---
    # 我把返回值改回去了，它现在只吐文字，不吐图片！
    async def gather_context_for_core_thought(self) -> str:
        """收集并格式化上下文信息，以供核心思考循环使用.

        这个方法会从事件存储中获取最近的聊天记录和系统事件，
        并将它们格式化为适合 LLM 上下文的字符串.

        Returns:
            str: 格式化后的上下文信息字符串，包含平台状态摘要和其他聊天记录的 YAML 格式化内容.
                如果没有找到相关信息，则返回一个默认的空字符串或初始状态信息.
        """
        initial_empty_context_info: str = self.state_manager.INITIAL_STATE.get(
            "recent_contextual_information", "无最近信息。"
        )
        # 主意识不处理图片，所以这个列表一直是空的
        # image_list_for_llm_from_history: list[str] = []

        chat_history_duration_minutes: int = getattr(
            config.core_logic_settings, "chat_history_context_duration_minutes", 10
        )

        formatted_recent_contextual_info = initial_empty_context_info
        try:
            system_lifecycle_events_raw: list[dict[str, Any]] = (
                await self.event_storage.get_recent_chat_message_documents(
                    duration_minutes=chat_history_duration_minutes,
                    conversation_id="system_events",
                    fetch_all_event_types=True,
                )
                or []
            )

            all_other_events: list[dict[str, Any]] = (
                await self.event_storage.get_recent_chat_message_documents(
                    duration_minutes=chat_history_duration_minutes,
                    fetch_all_event_types=False,
                )
                or []
            )

            other_chat_events_for_yaml_raw: list[dict[str, Any]] = []
            if all_other_events:
                for event_dict in all_other_events:
                    conv_info = event_dict.get("conversation_info")
                    if not (
                        isinstance(conv_info, dict)
                        and conv_info.get("conversation_id") == "system_events"
                    ):
                        other_chat_events_for_yaml_raw.append(event_dict)

            current_connections_info: dict[str, dict[str, Any]] = {}
            if hasattr(self.core_comm, "adapter_clients_info") and isinstance(
                self.core_comm.adapter_clients_info, dict
            ):
                current_connections_info = self.core_comm.adapter_clients_info
            else:
                logger.warning(
                    "CoreWebsocketServer 实例没有 adapter_clients_info 属性或其类型不正确，"
                    "无法获取实时连接状态。"
                )

            platform_status_summary_str = format_platform_status_summary(
                current_connections_info,
                system_lifecycle_events_raw,
                status_timespan_minutes=chat_history_duration_minutes,
            )

            other_chats_yaml_str = ""
            # 虽然 format_messages_for_llm_context 会返回图片，但我们在这里把它丢掉
            if other_chat_events_for_yaml_raw:
                other_chats_yaml_str, _ = format_messages_for_llm_context(
                    other_chat_events_for_yaml_raw,
                    style="yaml",
                    image_placeholder_key=getattr(
                        config.core_logic_settings,
                        "llm_image_placeholder_key",
                        "llm_image_placeholder",
                    ),
                    image_placeholder_value=getattr(
                        config.core_logic_settings, "llm_image_placeholder_value", "[IMAGE_HERE]"
                    ),
                    desired_history_span_minutes=chat_history_duration_minutes,
                    max_messages_per_group=getattr(
                        config.core_logic_settings, "max_messages_per_group_in_yaml", 20
                    ),
                )
                # image_list_for_llm_from_history.extend(temp_image_list)

            final_context_parts = []
            if (
                platform_status_summary_str
                and platform_status_summary_str.strip()
                and "无活动" not in platform_status_summary_str
            ):
                final_context_parts.append(platform_status_summary_str)

            if (
                other_chats_yaml_str
                and other_chats_yaml_str.strip()
                and "没有找到相关的聊天记录" not in other_chats_yaml_str
            ):
                final_context_parts.append(other_chats_yaml_str)

            if final_context_parts:
                formatted_recent_contextual_info = "\n\n".join(final_context_parts)

        except Exception as e:
            logger.error(f"收集上下文信息时出错: {e}", exc_info=True)
            formatted_recent_contextual_info = initial_empty_context_info

        return formatted_recent_contextual_info
