# 文件路径: src/prompting/orchestrator.py

from typing import TYPE_CHECKING, Any, Optional

from src.os.apps.interfaces import IApp, ISession
from src.os.models import WindowStatus
from src.prompting.components import PromptComponents
from src.prompting.schema_builder import SchemaBuilder
from src.prompting.system_prompt_parts_builder import SystemPromptPartsBuilder
from src.prompting.templates import prompt_templates
from src.prompting.user_prompt_parts_builder import UserPromptPartsBuilder

if TYPE_CHECKING:
    from src.bootstrap.container import ServiceContainer
    from src.mind.abilities.deliberation_service import DeliberationService
    from src.mind.abilities.information_retrieval_service import InformationRetrievalService
    from src.mind.goal_manager import GoalManager
    from src.mind.state_manager import AIStateManager
    from src.os.application_manager import ApplicationManager
    from src.os.services.filesystem_service import FileSystemService
    from src.os.state_generator import AICOSStateGenerator
    from src.os.window_manager import WindowManager
    from src.services.database.services.entity_graph_service import EntityGraphService
    from src.services.database.services.thought_storage_service import ThoughtStorageService


class ThoughtPromptBuilder:
    """[最终版] 构建思维提示的总编排器和 Schema 构建总指挥."""

    def __init__(
        self,
        aicos_state_generator: "AICOSStateGenerator",
        window_manager: "WindowManager",
        application_manager: "ApplicationManager",
        state_manager: "AIStateManager",
        thought_storage_service: "ThoughtStorageService",
        entity_graph_service: "EntityGraphService",
        filesystem_service: "FileSystemService",
        info_retrieval_service: "InformationRetrievalService",
        goal_manager: "GoalManager",
        deliberation_service: "DeliberationService",
    ) -> None:
        self.is_context_switch_flag: bool = False
        self.aicos_state_generator = aicos_state_generator
        self.window_manager = window_manager
        self.application_manager = application_manager
        # self.chat_session_manager 已移除

        self.filesystem_service = filesystem_service
        self.info_retrieval_service = info_retrieval_service
        self.goal_manager = goal_manager
        self.deliberation_service = deliberation_service

        self.schema_builder = SchemaBuilder()

        self.system_prompt_parts_builder = SystemPromptPartsBuilder(
            state_manager,
            window_manager,
            application_manager,
            entity_graph_service,
        )
        self.user_prompt_parts_builder = UserPromptPartsBuilder(
            thought_storage_service,
            state_manager,
        )
        self.container: ServiceContainer | None = None # 用于接收容器引用

    def _build_response_schema(self, ui_mapping: dict[str, Any]) -> dict[str, Any]:
        """Schema 构建的总指挥方法."""
        schema_parts = {}

        schema_parts["internal_state"] = {
            "type": "object",
            "description": "你的内心状态。",
            "properties": {
                "mood": {"type": "string", "description": "你当前的情绪，是下意识的第一反应。"},
                "think": {"type": "string", "description": "你当前的内心想法，应该真实自然丰富。"},
                "intent": {"type": "string", "description": "你当前最直接的、短期的意图或打算。"},
            },
            "required": ["mood", "think", "intent"],
        }

        internal_action_properties = {}
        internal_action_properties.update(self.goal_manager.get_actions_schema())
        internal_action_properties.update(self.deliberation_service.get_actions_schema())

        external_action_properties = self._build_external_action_schema(ui_mapping)

        action_properties = {}
        if internal_action_properties:
            action_properties["internal"] = {
                "type": "object",
                "description": "内部的心理动作。",
                "properties": internal_action_properties,
                "maxProperties": 1, # 确保内部动作只选一个
            }
        if external_action_properties:
            action_properties["external"] = {
                "type": "object",
                "description": "与外部世界交互的动作。",
                "properties": external_action_properties,
                # 外部动作的约束由具体实现决定
            }

        if action_properties:
            schema_parts["action"] = {
                "type": "object",
                "description": "你决定执行的动作。",
                "properties": action_properties,
            }

        return self.schema_builder.assemble(schema_parts)

    def _build_external_action_schema(self, ui_mapping: dict) -> dict:
        """聚合所有外部动作提供者的 Schema."""
        external_actions = {}

        innate_actions = {}
        innate_actions.update(self.info_retrieval_service.get_actions_schema())
        innate_actions.update(self.filesystem_service.get_actions_schema())
        if not self.aicos_state_generator.is_connected:
            innate_actions["connect"] = {
                "type": "object", "description": "连接属于你的设备，可以进行工作或社交。",
                "properties": {
                    "device_name": {"type": "string", "enum": ["AIC-OS"]},
                    "motivation": {"type": "string"},
                }, "required": ["device_name", "motivation"],
            }
        if innate_actions:
            external_actions["innate"] = {
                "type": "object", "description": "你自带的能力。",
                "properties": innate_actions, "maxProperties": 1,
            }

        if self.aicos_state_generator.is_connected:
            aicos_interactions = self._build_aicos_interaction_schema(ui_mapping)
            if aicos_interactions:
                external_actions["AIC-OS"] = {
                    "type": "object", "description": "与 AIC-OS 系统的交互动作。",
                    "properties": aicos_interactions, "maxProperties": 1,
                }
        return external_actions

    def _build_aicos_interaction_schema(self, ui_mapping: dict) -> dict:
        """聚合所有 AIC-OS 交互的 Schema."""
        aicos_properties = {}

        # 调用 ApplicationManager 构建基础交互
        # BUG:build_base_interaction_schema未定义
        base_interactions = self.application_manager.build_base_interaction_schema(ui_mapping)
        if base_interactions:
            aicos_properties["base"] = {
                "type": "object", "description": "通用的 AIC-OS 界面操作。",
                "properties": base_interactions,
            }

        for platform_id, builder in self.application_manager._builders.items():
            app_schema = builder.get_action_definitions(self.window_manager)
            if app_schema:
                aicos_properties[platform_id] = {
                    "type": "object",
                    "description": f"与 {builder.platform_id.upper()} 应用的交互。",
                    "properties": app_schema,
                }
        return aicos_properties

    async def build_prompts_components(
        self,
        last_external_info_snapshot: str | None,
    ) -> tuple[PromptComponents, ISession | None, dict, str]:
        """构建所有 Prompt 组件，并返回 UI 映射表."""
        # 1. 生成当前轮次的 external_info
        image_collector = []
        current_external_info_block, ui_mapping = (
            await self.aicos_state_generator.build_current_state(
                image_collector=image_collector
            )
        )

        _, _, _, session = await self._extract_context_from_ui()  # <--- [修改] await a call

        # 2. 将上一轮的快照传递给 SystemPromptPartsBuilder
        system_prompt_blocks = await self.system_prompt_parts_builder.build(
            session=session,
            is_context_switch_flag=self.is_context_switch_flag,
            last_external_info_snapshot=last_external_info_snapshot
        )

        # 3. 将当前轮次的 external_info 传递给 UserPromptPartsBuilder
        user_prompt_blocks = await self.user_prompt_parts_builder.build(
            external_info_block=current_external_info_block
        )

        response_schema = self._build_response_schema(ui_mapping=ui_mapping)

        prompt_components_obj = PromptComponents(
            system_prompt_blocks=system_prompt_blocks,
            user_prompt_blocks=user_prompt_blocks,
            response_schema=response_schema,
            image_references=image_collector
        )

        return prompt_components_obj, session, ui_mapping, current_external_info_block

    async def _extract_context_from_ui(
        self,
    ) -> tuple[str, str | None, str | None, Optional["ISession"]]: # 返回通用接口
        all_windows = self.window_manager.get_all_windows_sorted()
        active_window = next(
            (w for w in reversed(all_windows) if w.status != WindowStatus.MINIMIZE), None
        )

        if not active_window:
            return "core", "core", None, None

        window_class_parts = active_window.window_class.split("/")
        app_list = self.application_manager.get_all_apps()
        parent_app = next((a for a in app_list if a.id == active_window.parent_app_id), None)
        platform_id = parent_app.name if parent_app else "unknown"

        if len(window_class_parts) > 1 and window_class_parts[0] == "main":
            return "platform", platform_id, None, None

        elif window_class_parts[0] == "conversation":
            conversation_uid = active_window.content_state.get("conversation_uid")
            if not conversation_uid:
                return "platform", platform_id, None, None

            # 通过接口动态获取会话实例
            session: ISession | None = None
            builder = self.application_manager.get_builder_by_name(platform_id)
            if builder and isinstance(builder, IApp) and self.container:
                session = await builder.get_session(conversation_uid, self.container)

            return "cellular", platform_id, conversation_uid, session

        return "core", "core", None, None

    def finalize_prompts(self, components: PromptComponents) -> tuple[str, str, dict[str, Any]]:
        """将所有 Prompt 组件格式化为最终的字符串."""
        system_prompt = prompt_templates.CORE_CYCLE_SYSTEM_PROMPT.format(
            **components.system_prompt_blocks
        )
        user_prompt = prompt_templates.CORE_CYCLE_USER_PROMPT.format(
            **components.user_prompt_blocks
        )
        return system_prompt, user_prompt, components.response_schema
