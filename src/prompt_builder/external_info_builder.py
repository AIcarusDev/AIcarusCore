# src/prompt_builder/external_info_builder.py
from typing import TYPE_CHECKING, Optional

from src.common.focus_chat_history_builder.chat_history_formatter import format_chat_history_for_llm
from src.common.utils import build_conversation_entity_uid
from src.focus_chat_mode.behavioral_guidance_generator import BehavioralGuidanceGenerator
from src.focus_chat_mode.components import PromptComponents

from .error import PromptBuilderError

if TYPE_CHECKING:
    from src.common.unread_info_service.unread_info_service import UnreadInfoService
    from src.database.services.event_storage_service import EventStorageService
    from src.domain.models import Stimulus
    from src.focus_chat_mode.chat_session import ChatSession
    from src.focus_chat_mode.chat_session_manager import ChatSessionManager


class ExternalInfoBuilder:
    """负责构建关于“外部世界”的信息块，如聊天记录、未读消息摘要等."""

    def __init__(
        self,
        unread_info_service: "UnreadInfoService",
        event_storage_service: "EventStorageService",
        chat_session_manager: "ChatSessionManager",
    ) -> None:
        self.unread_info_service = unread_info_service
        self.event_storage = event_storage_service
        self.chat_session_manager = chat_session_manager
        self._last_shown_unread_summary: str | None = None

    async def build(
        self,
        level: str,
        platform_id: str,
        conv_id: str | None,
        session: Optional["ChatSession"] = None,
    ) -> tuple[str, str, PromptComponents | None, list["Stimulus"] | None]:
        """获取外部信息和元信息块."""
        external_info, meta_info, history_components, processed_stimuli = "", "", None, None

        if not self.chat_session_manager:
            raise PromptBuilderError("会话管理器尚未准备就绪，无法构建外部信息块。")

        if level == "core":
            current_unread_summary = await self.unread_info_service.get_platform_summary()
            if current_unread_summary and current_unread_summary != self._last_shown_unread_summary:
                external_info = current_unread_summary
            else:
                external_info = "所有平台均无新的未读消息。"
            self._last_shown_unread_summary = current_unread_summary

        elif level == "platform":
            scroll_offset = self.chat_session_manager.platform_view_states.get(platform_id, {}).get(
                "scroll_offset", 0
            )
            external_info = await self.unread_info_service.get_conversation_list_summary(
                platform_id, scroll_offset=scroll_offset
            )
        elif level == "cellular" and conv_id:
            try:
                if "." not in conv_id:
                    raise PromptBuilderError(
                        f"无效的会话ID格式 '{conv_id}'。它必须是 'type.id' 格式。"
                    )
                conv_type, actual_id = conv_id.split(".", 1)
                session_key = build_conversation_entity_uid(platform_id, conv_type, actual_id)
            except (ValueError, IndexError):
                raise PromptBuilderError(f"无法从会话部分 '{conv_id}' 解析出类型和ID。") from None

            if not session:
                session = self.chat_session_manager.sessions.get(session_key)

            if not session:
                raise PromptBuilderError(f"找不到会话实体UID为 '{session_key}' 的活跃会话档案。")

            bot_profile = await session.get_bot_profile()
            history_components, processed_stimuli = await format_chat_history_for_llm(
                event_storage=self.event_storage,
                conversation_id=session.conversation_info.conversation_id,
                bot_profile=bot_profile,
                conversation_type=session.conversation_type,
                conversation_name=session.conversation_name,
                last_processed_timestamp=session.last_processed_timestamp,
                is_first_turn=(
                    self.chat_session_manager.core_logic.prompt_builder.is_context_switch_flag
                    if self.chat_session_manager.core_logic
                    else False
                ),
            )

            if history_components.conversation_name:
                session.conversation_name = history_components.conversation_name

            unread_summary_str = await self.unread_info_service.generate_unread_summary_text(
                exclude_conversation_id=session.conversation_id
            )

            event_types_block_str = (
                "## Event Types\n"
                "[MSG]: 普通消息，在消息后的（id:xxx）为消息的id\n"
                "[SYS]: 系统通知\n"
                '[MOTIVE]: 对应你的"motivation"，帮助你更好的了解自己的心路历程，代表你发出该条消息的“背后动机”或“原因”\n'  # noqa: E501
                "[FILE]: 文件分享\n"
                "[表情包: xxx] 或 [图片: xxx]: 这代表早些时候的图片，你已经不能直接看到了，只能通过文字来理解它的“印象”。\n"  # noqa: E501
                "[NOTICE]: 来自平台的通知\n"
            )

            external_info = (
                f"<Conversation_Info>\n{history_components.conversation_info_block}\n</Conversation_Info>\n\n"
                f"<user_logs>\n{history_components.user_list_block}\n</user_logs>\n\n"
                f"<event_types>\n{event_types_block_str}\n</event_types>\n\n"
                f"<chat_history>\n{history_components.chat_history_log_block}\n</chat_history>\n\n"
                f"<unread_summary>\n{unread_summary_str or '所有其他会话均无未读消息。'}\n</unread_summary>"  # noqa: E501
            )

            guidance_generator = BehavioralGuidanceGenerator(session)
            meta_info = guidance_generator.generate_guidance()

        return external_info, meta_info, history_components, processed_stimuli
