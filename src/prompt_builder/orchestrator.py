# src/prompt_builder/orchestrator.py
from typing import TYPE_CHECKING, Any, Optional

# 导入新的 AIC-OS 模型和服务
from src.aicos.models import WindowStatus
from src.aicos.state_generator import AICOSStateGenerator
from src.domain.models import Stimulus
from src.focus_chat_mode.components import PromptComponents
from src.prompt_templates import prompt_templates
from src.prompt_templates.deliberation_prompts import (
    DELIBERATION_RESPONSE_SCHEMA,
    DELIBERATION_SYSTEM_PROMPT,
    DELIBERATION_USER_PROMPT,
)

from .schema_builder import SchemaBuilder
from .system_prompt_parts_builder import SystemPromptPartsBuilder
from .user_prompt_parts_builder import UserPromptPartsBuilder

if TYPE_CHECKING:
    from src.aicos.window_manager import WindowManager
    from src.core_communication.core_ws_server import CoreWebsocketServer
    from src.core_logic.internal_info_builder import InternalInfoBuilder
    from src.core_logic.state_manager import AIStateManager
    from src.database.services.entity_graph_service import EntityGraphService
    from src.database.services.thought_storage_service import ThoughtStorageService
    from src.focus_chat_mode.chat_session import ChatSession
    from src.focus_chat_mode.chat_session_manager import ChatSessionManager


class ThoughtPromptBuilder:
    """[AIC-OS]构建思维提示的总编排器.

    它协调所有子构建器，将 AIC-OS 的内部状态、历史信息和外部事件
    整合成一个完整的、可供 LLM 理解的上下文。
    """

    def __init__(
        self,
        # 新增 AICOS 核心服务作为依赖
        aicos_state_generator: "AICOSStateGenerator",
        window_manager: "WindowManager",
        # 旧的依赖，部分仍然需要
        internal_info_builder: "InternalInfoBuilder",
        state_manager: "AIStateManager",
        thought_storage_service: "ThoughtStorageService",
        entity_graph_service: "EntityGraphService",
        chat_session_manager: Optional["ChatSessionManager"] = None,
        core_ws_server: Optional["CoreWebsocketServer"] = None,
    ) -> None:
        self.is_context_switch_flag: bool = False

        # AIC-OS 核心服务
        self.aicos_state_generator = aicos_state_generator
        self.window_manager = window_manager

        # 子构建器
        self.schema_builder = SchemaBuilder(window_manager)
        self.system_prompt_parts_builder = SystemPromptPartsBuilder(
            internal_info_builder,
            state_manager,
            chat_session_manager,
            core_ws_server,
            # action_handler, # AICOS 模式下，SystemPrompt不再需要直接访问ActionHandler
            entity_graph_service,
        )
        self.user_prompt_parts_builder = UserPromptPartsBuilder(
            thought_storage_service, entity_graph_service, chat_session_manager, state_manager
        )
        # TODO: UserPromptPartsBuilder 和 SystemPromptPartsBuilder 也需要进行相应的 AIC-OS 适配改造

    async def build_prompts_components(
        self,
        # handover_result 在 AIC-OS 模式下可能需要重新设计，暂时保留
        handover_result: dict | None = None,
    ) -> tuple[PromptComponents, list[Stimulus] | None, dict]:
        """构建所有 Prompt 组件，并返回 UI 映射表.

        这是在新架构下的核心入口方法。
        """
        # 1. 生成 AI 的“视觉世界”：XML 界面 和 UI 映射表
        external_info_block, ui_mapping = await self.aicos_state_generator.build_current_state()

        # 2. 从 AI 的“视觉”中反向推断出当前的上下文状态
        level, platform_id, conv_id, session = self._extract_context_from_ui()

        # 3. 构建 System Prompt 的各个部分
        # 注意: is_context_switch_flag 和 can_go_back 的逻辑需要适配新的窗口历史管理
        system_prompt_blocks = await self.system_prompt_parts_builder.build(
            level=level,
            platform_id=platform_id,
            conv_id=conv_id,
            session=session,
            user_map=None,  # 在 AIC-OS 模式下，用户信息直接体现在 UI 中，不再需要独立的 user_map
            can_go_back=False,  # TODO: 替换为基于 WindowManager 的历史记录判断
            is_context_switch_flag=self.is_context_switch_flag,
        )

        # 4. 构建 User Prompt 的各个部分
        # TODO: `processed_stimuli` 的概念需要重新审视。在 AIC-OS 中，"未读"的概念
        #       体现在 UI 元素的 `unread` 属性上，而不是一个事件列表。
        processed_stimuli = None
        user_prompt_blocks = await self.user_prompt_parts_builder.build(
            handover_result=handover_result,
            meta_info_block="",  # meta_info 也可以整合进 XML 的 <desc> 标签中
            external_info_block=external_info_block,
            platform_id=platform_id,
            level=level,
            session=session,
        )

        # 5. [关键] 将 ui_mapping 传递给 SchemaBuilder 来生成动态 Schema
        response_schema = self.schema_builder.build_response_schema(
            ui_mapping=ui_mapping,
        )

        # 6. 组装所有零件
        prompt_components_obj = PromptComponents(
            system_prompt_blocks=system_prompt_blocks,
            user_prompt_blocks=user_prompt_blocks,
            response_schema=response_schema,
            # 以下字段在 AIC-OS 模式下可能不再需要，或需要新的实现方式
            last_valid_text_message=None,
            image_references=[],
            user_map={},
            uid_str_to_platform_id_map={},
        )

        # 7. 返回所有产物，尤其是 ui_mapping，它将传递给 DecisionDispatcher
        return prompt_components_obj, processed_stimuli, ui_mapping

    def _extract_context_from_ui(
        self,
    ) -> tuple[str, str | None, str | None, Optional["ChatSession"]]:
        """[新核心逻辑]通过分析 WindowManager 中的窗口状态，来确定 AI 当前的上下文.

        这完全取代了旧的 focus_path 字符串机制。
        """
        # 1. 查找当前激活（z_order 最高且非最小化）的窗口
        all_windows = self.window_manager.get_all_windows_sorted()
        active_window = next(
            (w for w in reversed(all_windows) if w.status != WindowStatus.MINIMIZE), None
        )

        if not active_window:
            # 没有激活的窗口 -> AI 正在看桌面 -> core level
            return "core", "core", None, None

        # 2. 从激活的窗口信息中推断上下文
        # 假设 window_class 的格式是 "type/subtype"，例如 "main/conversation_list"
        window_class_parts = active_window.window_class.split("/")

        if len(window_class_parts) > 1 and window_class_parts[0] == "main":
            # 如果是应用主窗口 -> platform level
            platform_id = active_window.parent_app_id  # 假设 parent_app_id 是平台ID
            return "platform", platform_id, None, None

        elif window_class_parts[0] == "conversation":
            # 如果是会话窗口 -> cellular level
            platform_id = active_window.parent_app_id

            # 从窗口的 content_state 中获取持久化的会话 UID
            conversation_uid = active_window.content_state.get("conversation_uid")
            if not conversation_uid:
                # 这是一个错误状态，但我们提供一个回退
                return "platform", platform_id, None, None

            # TODO: 需要从 ChatSessionManager 获取 session 实例
            # session = self.chat_session_manager.sessions.get(conversation_uid)
            session = None

            return "cellular", platform_id, conversation_uid, session

        # 默认回退到 core level
        return "core", "core", None, None

    def finalize_prompts(self, components: PromptComponents) -> tuple[str, str, dict[str, Any]]:
        """最终化提示组件，生成系统和用户提示的字符串表示."""
        # 注意：模板文件也需要进行相应的 AIC-OS 改造
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
