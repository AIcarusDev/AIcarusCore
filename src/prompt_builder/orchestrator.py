# src/prompt_builder/orchestrator.py
from typing import TYPE_CHECKING, Any, Optional

from src.common.utils import parse_focus_path
from src.domain.models import Stimulus
from src.focus_chat_mode.components import PromptComponents
from src.prompt_templates import prompt_templates
from src.prompt_templates.deliberation_prompts import (
    DELIBERATION_RESPONSE_SCHEMA,
    DELIBERATION_SYSTEM_PROMPT,
    DELIBERATION_USER_PROMPT,
)

from .external_info_builder import ExternalInfoBuilder
from .schema_builder import SchemaBuilder
from .system_prompt_parts_builder import SystemPromptPartsBuilder
from .user_prompt_parts_builder import UserPromptPartsBuilder

if TYPE_CHECKING:
    from src.action.action_handler import ActionHandler
    from src.common.unread_info_service.unread_info_service import UnreadInfoService
    from src.core_communication.core_ws_server import CoreWebsocketServer
    from src.core_logic.internal_info_builder import InternalInfoBuilder
    from src.core_logic.state_manager import AIStateManager
    from src.database.services.entity_graph_service import EntityGraphService
    from src.database.services.event_storage_service import EventStorageService
    from src.database.services.thought_storage_service import ThoughtStorageService
    from src.focus_chat_mode.chat_session import ChatSession
    from src.focus_chat_mode.chat_session_manager import ChatSessionManager


class ThoughtPromptBuilder:
    """构建思维提示的类."""

    def __init__(
        self,
        unread_info_service: "UnreadInfoService",
        internal_info_builder: "InternalInfoBuilder",
        event_storage_service: "EventStorageService",
        thought_storage_service: "ThoughtStorageService",
        entity_graph_service: "EntityGraphService",
        action_handler: "ActionHandler",
        state_manager: "AIStateManager",
        chat_session_manager: Optional["ChatSessionManager"] = None,
        core_ws_server: Optional["CoreWebsocketServer"] = None,
    ) -> None:
        self.is_context_switch_flag: bool = False

        # --- [核心修复 1/4] ---
        # 将实例变量改为私有，表示它们由 property 控制
        self._chat_session_manager: ChatSessionManager | None = None
        self._core_ws_server: CoreWebsocketServer | None = None
        # --- [修复结束] ---

        # 在初始化时，就创建好所有子构建器
        # 此时，它们接收到的 chat_session_manager 和 core_ws_server 可能是 None，这是符合预期的
        self.schema_builder = SchemaBuilder(chat_session_manager, core_ws_server)
        self.external_info_builder = ExternalInfoBuilder(
            unread_info_service, event_storage_service, chat_session_manager
        )
        self.system_prompt_parts_builder = SystemPromptPartsBuilder(
            internal_info_builder,
            state_manager,
            chat_session_manager,
            core_ws_server,
            action_handler,
            entity_graph_service,
        )
        self.user_prompt_parts_builder = UserPromptPartsBuilder(
            thought_storage_service, entity_graph_service, chat_session_manager, state_manager
        )

        # 通过调用 property setter 来完成初始的依赖注入
        # 这确保了即使在初始化时传入了有效的值，它也能被正确地传递下去
        self.chat_session_manager = chat_session_manager
        self.core_ws_server = core_ws_server

    # --- [核心修复 2/4] ---
    # 将 chat_session_manager 定义为一个 property，保持现有逻辑
    @property
    def chat_session_manager(self) -> Optional["ChatSessionManager"]:
        """获取 chat_session_manager 实例."""
        return self._chat_session_manager

    @chat_session_manager.setter
    def chat_session_manager(self, value: Optional["ChatSessionManager"]) -> None:
        """设置 chat_session_manager 实例，并将其自动传播到所有需要它的子构建器中."""
        self._chat_session_manager = value
        # 将新的值（无论是实例还是 None）同步给所有子组件
        self.schema_builder.chat_session_manager = value
        self.external_info_builder.chat_session_manager = value
        self.system_prompt_parts_builder.chat_session_manager = value
        self.user_prompt_parts_builder.chat_session_manager = value

    # --- [修复结束] ---

    # --- [核心修复 3/4] ---
    # 为 core_ws_server 添加同样的 property 和 setter 逻辑
    @property
    def core_ws_server(self) -> Optional["CoreWebsocketServer"]:
        """获取 core_ws_server 实例."""
        return self._core_ws_server

    @core_ws_server.setter
    def core_ws_server(self, value: Optional["CoreWebsocketServer"]) -> None:
        """设置 core_ws_server 实例，并将其自动传播到所有需要它的子构建器中."""
        self._core_ws_server = value
        # 将新的值传播给需要它的子模块
        self.schema_builder.core_ws_server = value
        self.system_prompt_parts_builder.core_ws_server = value

    # --- [修复结束] ---

    async def build_prompts_components(
        self,
        level: str,
        focus_path: str | None,
        session: Optional["ChatSession"] = None,
        handover_result: dict | None = None,
    ) -> tuple[PromptComponents, list[Stimulus] | None]:
        """构建提示组件."""
        current_level, current_platform_id, current_conv_id = parse_focus_path(focus_path)

        # --- [核心修复 4/4] ---
        # 现在可以直接安全地访问 self.chat_session_manager
        can_go_back = (
            self.chat_session_manager
            and self.chat_session_manager.focus_manager
            and len(self.chat_session_manager.focus_manager.focus_history) > 1
        )
        # --- [修复结束] ---

        (
            external_info_block,
            meta_info_block,
            history_components,
            processed_stimuli,
        ) = await self.external_info_builder.build(
            current_level, current_platform_id, current_conv_id, session
        )

        system_prompt_blocks = await self.system_prompt_parts_builder.build(
            level=current_level,
            platform_id=current_platform_id,
            conv_id=current_conv_id,
            session=session,
            user_map=history_components.user_map if history_components else None,
            can_go_back=can_go_back,
            is_context_switch_flag=self.is_context_switch_flag,
        )

        user_prompt_blocks = await self.user_prompt_parts_builder.build(
            handover_result=handover_result,
            meta_info_block=meta_info_block,
            external_info_block=external_info_block,
            platform_id=current_platform_id,
            level=current_level,
            session=session,
        )

        response_schema = self.schema_builder.build_response_schema(
            current_level, current_platform_id, current_conv_id, can_go_back=can_go_back
        )

        prompt_components_obj = PromptComponents(
            system_prompt_blocks=system_prompt_blocks,
            user_prompt_blocks=user_prompt_blocks,
            response_schema=response_schema,
            last_valid_text_message=history_components.last_valid_text_message
            if history_components
            else None,
            image_references=history_components.image_references if history_components else [],
            # Pass through context needed by other modules
            user_map=history_components.user_map if history_components else {},
            uid_str_to_platform_id_map=history_components.uid_str_to_platform_id_map
            if history_components
            else {},
        )

        return prompt_components_obj, processed_stimuli

    def finalize_prompts(self, components: PromptComponents) -> tuple[str, str, dict[str, Any]]:
        """最终化提示组件，生成系统和用户提示的字符串表示."""
        system_prompt = prompt_templates.CORE_CYCLE_SYSTEM_PROMPT.format(
            **components.system_prompt_blocks
        )
        user_prompt = prompt_templates.CORE_CYCLE_USER_PROMPT.format(
            **components.user_prompt_blocks
        )
        return system_prompt, user_prompt, components.response_schema

    def build_deliberation_prompts(
        self, pipeline_params: dict, current_internal_state: dict
    ) -> tuple[str, str, dict[str, Any]]:
        """构建深思熟虑的提示."""
        opinions_block = "\n".join(
            [
                f'            <pipeline tag="{p.get("tag", f"观点 {i + 1}")}">\n'
                f"                <initial_thought>{p.get('initial_thought', '无具体想法。')}</initial_thought>\n"  # noqa: E501
                f"            </pipeline>"
                for i, p in enumerate(pipeline_params.get("opinions", []))
            ]
        )

        from src.common.time_utils import get_formatted_time_for_llm
        from src.config import config

        system_prompt = DELIBERATION_SYSTEM_PROMPT.format(
            current_time=get_formatted_time_for_llm(),
            bot_name=config.persona.bot_name,
            slow_thought_persona=config.persona.slow_thought_persona,
        )

        user_prompt = DELIBERATION_USER_PROMPT.format(
            fast_think_person_block=f'你是"{config.persona.bot_name}"；\n{config.persona.description}\n{config.persona.profile}',
            mood=current_internal_state.get("mood", "未知"),
            think=current_internal_state.get("think", "未知"),
            intent=current_internal_state.get("intent", "未知"),
            motivation=pipeline_params.get("motivation", "无明确动机"),
            opinions_block=opinions_block,
        )

        return system_prompt, user_prompt, DELIBERATION_RESPONSE_SCHEMA
