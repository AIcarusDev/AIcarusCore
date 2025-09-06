# 文件路径: src/prompting/system_prompt_parts_builder.py

from typing import TYPE_CHECKING, Any

from src.common.time_utils import get_formatted_time_for_llm
from src.config import config
from src.mind.memory_system.working_memory.builder import WorkingMemoryBuilder
from src.os.application_manager import ApplicationManager
from src.os.apps.interfaces import ISession
from src.os.models import WindowStatus
from src.os.window_manager import WindowManager
from src.prompting.templates.aicarus_rule import AICARUS_RULE
from src.prompting.templates.core_prompts import CORE_BEHAVIOR_GUIDELINES

if TYPE_CHECKING:
    from src.mind.state_manager import AIStateManager
    from src.services.database.services.entity_graph_service import EntityGraphService


class SystemPromptPartsBuilder:
    """负责构建填充 System Prompt 模板所需的所有部分."""

    def __init__(
        self,
        state_manager: "AIStateManager",
        window_manager: "WindowManager",
        application_manager: "ApplicationManager",
        entity_service: "EntityGraphService",
    ) -> None:
        self.state_manager = state_manager
        self.window_manager = window_manager
        self.application_manager = application_manager
        self.entity_service = entity_service
        self.working_memory_builder = WorkingMemoryBuilder()

    # 修改 build 方法的签名以接收 session
    async def build(
        self,
        session: ISession | None,
        is_context_switch_flag: bool,
        last_external_info_snapshot: str | None,
    ) -> dict[str, Any]:
        """构建并返回一个包含 System Prompt 所有组件的字典."""
        memory_depth = self.working_memory_builder.get_memory_depth()
        recent_thoughts = await self.state_manager.thought_service.get_recent_thought_documents(
            limit=memory_depth
        )
        memory_fragments = self.working_memory_builder.build_fragments(
            recent_thoughts, last_external_info_snapshot
        )
        working_memories_block = self.working_memory_builder.render_to_xml_string(memory_fragments)

        # 将 session 传递给 _get_current_state_block
        current_state_block = self._get_current_state_block(session)
        current_goals_block = self.state_manager.goal_manager.get_formatted_goals()

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
            "sticker_collection_block": "",  # 占位，未来实现
            "available_platforms_block": "",  # 占位，未来实现
            "behavior_guidelines_block": CORE_BEHAVIOR_GUIDELINES,
        }

    # +++ [核心修复] 修改 _get_current_state_block 的签名以接收 session +++
    def _get_current_state_block(self, session: ISession | None) -> str:
        """根据 WindowManager 的状态和当前会话构建状态描述."""
        active_window = next(
            (
                w
                for w in reversed(self.window_manager.get_all_windows_sorted())
                if w.status != WindowStatus.MINIMIZE
            ),
            None,
        )

        if not active_window:
            return "你当前正看着 AIC-OS 的桌面，没有任何激活的应用窗口。"

        if session and session.conversation_name:
            # 如果我们处于一个具体的聊天会话中
            return f"你当前正专注于与 '{session.conversation_name}' 的对话窗口。"

        if active_window.status == WindowStatus.MAXIMIZE:
            status_desc = "被最大化显示"
        else:
            status_desc = "是当前激活的窗口"

        return f"你当前正专注于应用窗口 '{active_window.title}'，它{status_desc}。"
