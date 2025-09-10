from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Optional
from src.common.custom_logging.logging_config import get_logger

from src.common.time_utils import get_formatted_time_for_llm
from src.config import config
from src.mind.memory_system.working_memory.builder import WorkingMemoryBuilder
from src.prompting.components import PromptComponents
from src.prompting.schema_builder import SchemaBuilder
from src.prompting.templates.aicarus_rule import AICARUS_RULE
from src.prompting.templates.core_prompts import CORE_BEHAVIOR_GUIDELINES
from src.os.apps.interfaces import IApp

if TYPE_CHECKING:
    from src.bootstrap.container import ServiceContainer
    from src.os.apps.interfaces import ISession
    from src.mind.abilities.deliberation_service import DeliberationService
    from src.mind.abilities.information_retrieval_service import InformationRetrievalService
    from src.mind.goal_manager import GoalManager
    from src.mind.state_manager import AIStateManager
    from src.os.application_manager import ApplicationManager
    from src.os.services.filesystem_service import FileSystemService
    from src.os.state_generator import AICOSStateGenerator
    from src.os.window_manager import WindowManager
    from src.prompting.system_prompt_parts_builder import SystemPromptPartsBuilder
    from src.prompting.user_prompt_parts_builder import UserPromptPartsBuilder
    from src.services.database.services.entity_graph_service import EntityGraphService
    from src.services.database.services.thought_storage_service import ThoughtStorageService

logger = get_logger(__name__)


class IPromptBuildingStrategy(ABC):
    """定义了构建Prompt组件的策略接口。"""

    @abstractmethod
    async def build_prompts_components(
        self, last_external_info_snapshot: Optional[str], ui_message: Optional[str] = None
    ) -> tuple[PromptComponents, Optional["ISession"], dict, str]:
        """
        构建并返回所有Prompt的核心组件。

        Args:
            last_external_info_snapshot: 上一轮外部信息的快照。

        Returns:
            一个元组，包含Prompt组件对象、当前会话、UI映射和当前外部信息块。
        """
        pass


class QQPromptStrategy(IPromptBuildingStrategy):
    """为QQ/AIC-OS模式构建Prompt的策略。"""

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
        system_prompt_parts_builder: "SystemPromptPartsBuilder",
        user_prompt_parts_builder: "UserPromptPartsBuilder",
    ):
        self.aicos_state_generator = aicos_state_generator
        self.window_manager = window_manager
        self.application_manager = application_manager
        self.state_manager = state_manager
        self.thought_storage_service = thought_storage_service
        self.entity_graph_service = entity_graph_service
        self.filesystem_service = filesystem_service
        self.info_retrieval_service = info_retrieval_service
        self.goal_manager = goal_manager
        self.deliberation_service = deliberation_service
        self.system_prompt_parts_builder = system_prompt_parts_builder
        self.user_prompt_parts_builder = user_prompt_parts_builder
        self.schema_builder = SchemaBuilder()
        self.is_context_switch_flag: bool = False
        self.container: Optional["ServiceContainer"] = None

    async def build_prompts_components(
        self, last_external_info_snapshot: Optional[str], ui_message: Optional[str] = None
    ) -> tuple[PromptComponents, Optional["ISession"], dict, str]:
        """构建所有 Prompt 组件，并返回 UI 映射表."""
        # 1. 生成当前轮次的 external_info
        image_collector = []
        (
            current_external_info_block,
            ui_mapping,
        ) = await self.aicos_state_generator.build_current_state(image_collector=image_collector)

        _, _, _, session = await self._extract_context_from_ui()

        # 2. 将上一轮的快照传递给 SystemPromptPartsBuilder
        system_prompt_blocks = await self.system_prompt_parts_builder.build(
            session=session,
            is_context_switch_flag=self.is_context_switch_flag,
            last_external_info_snapshot=last_external_info_snapshot,
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
            image_references=image_collector,
        )

        logger.debug(
            f"QQPromptStrategy: Prompt components built. "
            f"System blocks: {len(system_prompt_blocks)}, "
            f"User blocks: {len(user_prompt_blocks)}, "
            f"Images: {len(image_collector)}"
        )

        return prompt_components_obj, session, ui_mapping, current_external_info_block

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
                "maxProperties": 1,
            }
        if external_action_properties:
            action_properties["external"] = {
                "type": "object",
                "description": "与外部世界交互的动作。",
                "properties": external_action_properties,
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
                "type": "object",
                "description": "连接属于你的设备，可以进行工作或社交。",
                "properties": {
                    "device_name": {"type": "string", "enum": ["AIC-OS"]},
                    "motivation": {"type": "string"},
                },
                "required": ["device_name", "motivation"],
            }
        if innate_actions:
            external_actions["innate"] = {
                "type": "object",
                "description": "你自带的能力。",
                "properties": innate_actions,
                "maxProperties": 1,
            }

        if self.aicos_state_generator.is_connected:
            aicos_interactions = self._build_aicos_interaction_schema(ui_mapping)
            if aicos_interactions:
                external_actions["AIC-OS"] = {
                    "type": "object",
                    "description": "与 AIC-OS 系统的交互动作。",
                    "properties": aicos_interactions,
                    "maxProperties": 1,
                }
        return external_actions

    def _build_aicos_interaction_schema(self, ui_mapping: dict) -> dict:
        """聚合所有 AIC-OS 交互的 Schema."""
        aicos_properties = {}

        base_interactions = self.application_manager.build_base_interaction_schema(ui_mapping)
        if base_interactions:
            aicos_properties["base"] = {
                "type": "object",
                "description": "通用的 AIC-OS 界面操作。",
                "properties": base_interactions,
            }

        for platform_id, builder in self.application_manager._builders.items():
            app_schema = builder.get_action_definitions(self.window_manager)
            if app_schema:
                aicos_properties[platform_id] = {
                    "type": "object",
                    "description": f"与 {builder.app_name.upper()} 应用的交互。",
                    "properties": app_schema,
                }
        return aicos_properties

    async def _extract_context_from_ui(
        self,
    ) -> tuple[str, Optional[str], Optional[str], Optional["ISession"]]:
        from src.os.models import WindowStatus
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

            session: Optional["ISession"] = None
            builder = self.application_manager.get_builder_by_name(platform_id)
            if builder and isinstance(builder, IApp) and self.container:
                session = await builder.get_session(conversation_uid, self.container)

            return "cellular", platform_id, conversation_uid, session

        return "core", "core", None, None


class UIPromptStrategy(IPromptBuildingStrategy):
    """为独立的UI模式构建Prompt的策略。"""

    def __init__(
        self,
        state_manager: "AIStateManager",
        thought_storage_service: "ThoughtStorageService",
        goal_manager: "GoalManager",
    ):
        self.state_manager = state_manager
        self.thought_storage_service = thought_storage_service
        self.goal_manager = goal_manager
        self.schema_builder = SchemaBuilder()
        self.working_memory_builder = WorkingMemoryBuilder()

    async def build_prompts_components(
        self, last_external_info_snapshot: Optional[str], ui_message: Optional[str] = None
    ) -> tuple[PromptComponents, Optional["ISession"], dict, str]:
        # 1. 构建UI专属的 external_info
        external_info = self._build_ui_external_info(ui_message)

        # 2. 构建UI专属的 System/User Prompt Blocks
        system_blocks = await self._build_ui_system_prompt_blocks(last_external_info_snapshot)
        user_blocks = {
            "external_info_block": external_info,
            "ui_message_block": ui_message if ui_message else "" # 将 ui_message 作为单独的块
        }

        # 3. 构建UI专属的 Response Schema
        response_schema = self._build_ui_response_schema()

        # 4. 组装并返回
        prompt_components_obj = PromptComponents(
            system_prompt_blocks=system_blocks,
            user_prompt_blocks=user_blocks,
            response_schema=response_schema,
            image_references=[],
        )

        logger.debug(
            f"UIPromptStrategy: Prompt components built. "
            f"System blocks: {len(system_blocks)}, "
            f"User blocks: {len(user_blocks)}"
        )

        return prompt_components_obj, None, {}, external_info

    async def _build_ui_system_prompt_blocks(
        self, last_external_info_snapshot: str | None
    ) -> dict[str, Any]:
        """为UI模式构建一个专属的、轻量级的System Prompt组件字典。"""
        memory_depth = self.working_memory_builder.get_memory_depth()
        recent_thoughts = await self.thought_storage_service.get_recent_thought_documents(
            limit=memory_depth
        )
        memory_fragments = self.working_memory_builder.build_fragments(
            recent_thoughts, last_external_info_snapshot
        )
        working_memories_block = self.working_memory_builder.render_to_xml_string(memory_fragments)

        current_goals_block = self.goal_manager.get_formatted_goals()
        current_state_block = "你当前正通过 MasterUI 与主人直接进行对话。"

        return {
            "aicarus_rule_block": AICARUS_RULE,
            "current_time": get_formatted_time_for_llm(),
            "persona_block": (
                f'你是"{config.persona.bot_name}"；\n'
                f"{config.persona.description}\n"
                f"{config.persona.profile}"
            ),
            "current_goals_block": current_goals_block,
            "current_state_block": current_state_block,
            "working_memories_block": working_memories_block,
            "sticker_collection_block": "",
            "available_platforms_block": "",
            "behavior_guidelines_block": CORE_BEHAVIOR_GUIDELINES,
        }

    def _build_ui_external_info(self, ui_message: Optional[str] = None) -> str:
        """生成一段描述当前UI交互状态的文本。"""
        base_info = """[AIC-OS Status]
- Connection: Direct connection via MasterUI
- Current Interface: MasterUI Chat Window
- User: Master (master_001)
- Conversation: Private Chat with Master (master_chat_001)"""

        if ui_message:
            # 假设 ui_message 已经是多条消息的合并字符串，每条消息可能用换行符分隔
            # 这里将其包裹在 <chat_history> 标签中，并为每条消息添加 "User: " 前缀
            formatted_messages = "\n".join([f"User: {msg.strip()}" for msg in ui_message.split('\n') if msg.strip()])
            chat_history = f"""
<chat_history>
{formatted_messages}
</chat_history>"""
            return f"{base_info}\n{chat_history}"
        else:
            return f"""{base_info}

[Interface Content]
- Chat History is provided separately in the user prompt."""

    def _build_ui_response_schema(self) -> dict[str, Any]:
        """构建一个包含 send_ui_message 动作的Schema。"""
        schema_parts = {
            "internal_state": {
                "type": "object",
                "description": "你的内心状态。",
                "properties": {
                    "mood": {"type": "string", "description": "你当前的情绪。"},
                    "think": {"type": "string", "description": "你当前的内心想法。"},
                    "intent": {"type": "string", "description": "你当前的意图。"},
                },
                "required": ["mood", "think", "intent"],
            },
            "action": {
                "type": "object",
                "properties": {
                    "external": {
                        "type": "object",
                        "description": "与MasterUI界面的交互动作。",
                        "properties": {
                            "send_ui_message": {
                                "type": "object",
                                "description": "向UI聊天窗口发送一条消息。",
                                "properties": {
                                    "message": {"type": "string", "description": "要发送的消息内容。"}
                                },
                                "required": ["message"]
                            }
                        }
                    }
                }
            }
        }
        return self.schema_builder.assemble(schema_parts)
