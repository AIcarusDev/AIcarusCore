# src/prompt_builder/system_prompt_parts_builder.py
import json
from typing import TYPE_CHECKING, Any, Optional

from src.aicos.application_manager import ApplicationManager

# 导入 AIC-OS 相关模型和服务
from src.aicos.models import WindowStatus
from src.aicos.window_manager import WindowManager
from src.common.time_utils import get_formatted_time_for_llm
from src.config import config
from src.prompt_templates.aicarus_rule import AICARUS_RULE
from src.prompt_templates.core_prompts import CORE_BEHAVIOR_GUIDELINES

if TYPE_CHECKING:
    from src.core_logic.internal_info_builder import InternalInfoBuilder
    from src.core_logic.state_manager import AIStateManager
    from src.database.services.entity_graph_service import EntityGraphService
    from src.focus_chat_mode.chat_session import ChatSession


class SystemPromptPartsBuilder:
    """[AIC-OS 重构版]负责构建填充 System Prompt 模板所需的所有部分.

    它现在直接与 AIC-OS 的核心服务交互，以获取上下文信息。
    """

    def __init__(
        self,
        internal_info_builder: "InternalInfoBuilder",
        state_manager: "AIStateManager",
        window_manager: "WindowManager",
        application_manager: "ApplicationManager",
        entity_service: "EntityGraphService",
    ) -> None:
        self.internal_info_builder = internal_info_builder
        self.state_manager = state_manager
        self.window_manager = window_manager
        self.application_manager = application_manager
        self.entity_service = entity_service

    async def build(
        self,
        session: Optional["ChatSession"],
        is_context_switch_flag: bool,
    ) -> dict[str, Any]:
        """构建并返回一个包含 System Prompt 所有组件的字典."""
        # --- 并发获取数据 ---
        internal_info_task = self.internal_info_builder.build_internal_info_block(
            is_context_switch=is_context_switch_flag,
            session=session,
            user_map=None,  # user_map 已被 UI 取代
        )

        # --- 同步获取数据 ---
        # 在 AIC-OS 模式下，这些信息可以直接从管理器中同步获取，无需异步
        current_state_block = self._get_current_state_block()
        working_memories_block = self._build_working_memories_block()
        deliberation_summary_block = self._get_deliberation_summary_block(session)
        current_goals_block = self.state_manager.goal_manager.get_formatted_goals()

        # 等待异步任务完成
        internal_info_block = await internal_info_task

        return {
            "aicarus_rule_block": AICARUS_RULE,
            "current_time": get_formatted_time_for_llm(),
            "persona_block": f'你是"{config.persona.bot_name}"；\n{config.persona.description}\n{config.persona.profile}',  # noqa: E501
            "current_goals_block": current_goals_block,
            "current_state_block": current_state_block,
            "deliberation_summary_block": deliberation_summary_block,
            "working_memories_block": working_memories_block,
            "internal_info_block": internal_info_block,
            # 以下 block 在 AIC-OS 模式下被废弃或整合，提供空字符串
            "sticker_collection_block": "",
            "available_platforms_block": "",
            "behavior_guidelines_block": CORE_BEHAVIOR_GUIDELINES,  # 行为准则可以简化和统一
            "input_XML_block_description": self._get_input_xml_block_description(),
            "available_consciousness_controls": "",  # 由 SchemaBuilder 负责
            "available_actions": "",  # 由 SchemaBuilder 负责
        }

    def _get_current_state_block(self) -> str:
        """[新核心逻辑]根据 WindowManager 的状态构建当前状态的描述."""
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

        if active_window.status == WindowStatus.MAXIMIZE:
            status_desc = "被最大化显示"
        else:
            status_desc = "是当前激活的窗口"

        return f"你当前正专注于应用窗口 '{active_window.title}'，它{status_desc}。"

    async def _build_working_memories_block(self) -> str:
        """构建 <working_memories> 块，包含最近的行动和意识控制历史."""
        recent_thoughts = await self.state_manager.thought_service.get_recent_thought_documents(
            limit=8, max_age_seconds=60
        )

        if not recent_thoughts:
            return ""

        # 移除外层标签，模板中已有
        memory_lines = [
            "  <desc>以下是你的有印象/记得的，之前自己做的事。</desc>",
        ]

        for i, thought in enumerate(recent_thoughts):
            payload = thought.get("action_payload")

            # 过滤逻辑，确保 payload 包含有效指令
            if not payload or (
                not payload.get("external_action") and not payload.get("internal_action")
            ):
                continue

            # 移除 internal_state，因为它太大且与此处目的无关
            payload.pop("internal_state", None)

            # 再次检查，如果移除后只剩下 thought_id，也跳过
            if set(payload.keys()) <= {"thought_id"}:
                continue

            try:
                # FIX 3: 使用 separators 参数生成最紧凑的单行 JSON
                json_content = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                memory_lines.append(f'  <memory cycle_ago="{i + 1}">')
                memory_lines.append(f"    <![CDATA[{json_content}]]>")
                memory_lines.append("  </memory>")
            except (TypeError, ValueError):
                continue

        if len(memory_lines) == 1:  # 只有 <desc>，没有实际内容
            return ""

        return "\n".join(memory_lines)

    def _get_deliberation_summary_block(self, session: Optional["ChatSession"]) -> str:
        """构建慢脑思考决策摘要块."""
        if session and session.working_memory:
            remaining = session.working_memory.get("remaining_turns", 0)
            if remaining > 0:
                summary = session.working_memory.get("summary", "无内容。")
                session.working_memory["remaining_turns"] -= 1
                return (
                    f'<deliberation_summary duration="{remaining}_cycles">\n'
                    f"<!-- 这是你仔细思考后的总结，将在 {remaining} 轮思考后遗忘 -->\n"
                    f"{summary}\n"
                    f"</deliberation_summary>"
                )
            else:
                session.working_memory.clear()
        return ""

    def _get_input_xml_block_description(self) -> str:
        """为 AIC-OS 提供新的、更简洁的输入块描述."""
        return """
输入 XML 块介绍：
- <external_info>: 这个块包含了你当前能“看到”的 AIC-OS 虚拟操作系统的完整图形界面。所有你能进行的操作，都基于这个界面中显示的元素。
- <action_response>: (可选) 如果你上一轮的行动有文本返回结果，会在这里显示。
- <command_feedback>: (可选) 如果你上一轮的内部指令执行有反馈（如切换窗口成功），会在这里显示。
"""  # noqa: E501
