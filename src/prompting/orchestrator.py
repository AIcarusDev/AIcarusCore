# 文件路径: src/prompting/orchestrator.py

from typing import TYPE_CHECKING, Any, Optional

from src.apps.qq.components import PromptComponents
from src.domain.models import Stimulus
from src.os.models import WindowStatus
from src.os.state_generator import AICOSStateGenerator
from src.prompting.schema_builder import SchemaBuilder
from src.prompting.system_prompt_parts_builder import SystemPromptPartsBuilder
from src.prompting.templates import prompt_templates
from src.prompting.user_prompt_parts_builder import UserPromptPartsBuilder

if TYPE_CHECKING:
    from src.apps.qq.qq_chat_session import ChatSession
    from src.apps.qq.qq_chat_session_manager import ChatSessionManager
    from src.mind.abilities.deliberation_service import DeliberationService
    from src.mind.abilities.information_retrieval_service import InformationRetrievalService
    from src.mind.goal_manager import GoalManager
    from src.mind.internal_info_builder import InternalInfoBuilder
    from src.mind.state_manager import AIStateManager
    from src.os.application_manager import ApplicationManager
    from src.os.services.filesystem_service import FileSystemService
    from src.os.window_manager import WindowManager
    from src.services.core_communication.core_ws_server import CoreWebsocketServer
    from src.services.database.services.entity_graph_service import EntityGraphService
    from src.services.database.services.thought_storage_service import ThoughtStorageService


class ThoughtPromptBuilder:
    """[AIC-OS]构建思维提示的总编排器."""

    def __init__(
        self,
        aicos_state_generator: "AICOSStateGenerator",
        window_manager: "WindowManager",
        application_manager: "ApplicationManager",
        internal_info_builder: "InternalInfoBuilder",
        state_manager: "AIStateManager",
        thought_storage_service: "ThoughtStorageService",
        entity_graph_service: "EntityGraphService",
        filesystem_service: "FileSystemService",
        info_retrieval_service: "InformationRetrievalService",
        goal_manager: "GoalManager",
        deliberation_service: "DeliberationService",
        chat_session_manager: Optional["ChatSessionManager"] = None,
        core_ws_server: Optional["CoreWebsocketServer"] = None,
    ) -> None:
        self.is_context_switch_flag: bool = False
        self.aicos_state_generator = aicos_state_generator
        self.window_manager = window_manager
        self.application_manager = application_manager
        self.chat_session_manager = chat_session_manager  # Still needed for session access
        self.core_ws_server = core_ws_server

        self.schema_builder = SchemaBuilder(
            window_manager,
            filesystem_service,
            info_retrieval_service,
            goal_manager,
            deliberation_service,
        )
        self.system_prompt_parts_builder = SystemPromptPartsBuilder(
            internal_info_builder,
            state_manager,
            window_manager,
            application_manager,
            entity_graph_service,
        )
        self.user_prompt_parts_builder = UserPromptPartsBuilder(
            thought_storage_service,
            state_manager,
        )

    async def build_prompts_components(
        self,
        handover_result: dict | None = None,
    ) -> tuple[PromptComponents, list[Stimulus] | None, dict]:
        """构建所有 Prompt 组件，并返回 UI 映射表."""
        external_info_block, ui_mapping = await self.aicos_state_generator.build_current_state()

        _, _, _, session = self._extract_context_from_ui()

        system_prompt_blocks = await self.system_prompt_parts_builder.build(
            session=session,
            is_context_switch_flag=self.is_context_switch_flag,
        )

        user_prompt_blocks = await self.user_prompt_parts_builder.build(
            handover_result=handover_result,
            external_info_block=external_info_block,
            session=session,
        )

        response_schema = self.schema_builder.build_response_schema(ui_mapping=ui_mapping)

        prompt_components_obj = PromptComponents(
            system_prompt_blocks=system_prompt_blocks,
            user_prompt_blocks=user_prompt_blocks,
            response_schema=response_schema,
        )

        return prompt_components_obj, None, ui_mapping

    def _extract_context_from_ui(
        self,
    ) -> tuple[str, str | None, str | None, Optional["ChatSession"]]:
        """通过分析 WindowManager 中的窗口状态，来确定 AI 当前的上下文."""
        all_windows = self.window_manager.get_all_windows_sorted()
        active_window = next(
            (w for w in reversed(all_windows) if w.status != WindowStatus.MINIMIZE), None
        )

        if not active_window:
            return "core", "core", None, None

        window_class_parts = active_window.window_class.split("/")

        # [修复] 应用的 ID 应该从 parent_app_id 获取
        app = self.application_manager.get_all_apps()
        parent_app = next((a for a in app if a.id == active_window.parent_app_id), None)
        platform_id = parent_app.name if parent_app else "unknown"

        if len(window_class_parts) > 1 and window_class_parts[0] == "main":
            return "platform", platform_id, None, None

        elif window_class_parts[0] == "conversation":
            conversation_uid = active_window.content_state.get("conversation_uid")
            if not conversation_uid:
                return "platform", platform_id, None, None

            session = None
            if self.chat_session_manager:
                # [修复] 应该使用 await get_or_create_session
                # 但为了避免阻塞，这里我们只尝试获取已存在的 session
                session = self.chat_session_manager.sessions.get(conversation_uid)

            return "cellular", platform_id, conversation_uid, session

        return "core", "core", None, None

    def finalize_prompts(self, components: PromptComponents) -> tuple[str, str, dict[str, Any]]:
        """最终化提示组件，生成系统和用户提示的字符串表示."""
        system_prompt = prompt_templates.CORE_CYCLE_SYSTEM_PROMPT.format(
            **components.system_prompt_blocks
        )
        user_prompt = prompt_templates.CORE_CYCLE_USER_PROMPT.format(
            **components.user_prompt_blocks
        )
        return system_prompt, user_prompt, components.response_schema
