# 文件: src/core_logic/internal_info_builder.py (净化版 V1.3)
from typing import TYPE_CHECKING, Any, Optional

from aicarus_protocols import Event
from src.common.custom_logging.logging_config import get_logger
from src.database.services.thought_storage_service import ThoughtStorageService

if TYPE_CHECKING:
    from src.core_logic.prompt_builder import ThoughtPromptBuilder
    from src.focus_chat_mode.chat_session import ChatSession

logger = get_logger(__name__)


class InternalInfoBuilder:
    """
    负责构建AI纯粹的内部信息块。
    它不再关心行动结果，只负责报告内心独白和中断情况。
    """

    def __init__(self, thought_storage_service: ThoughtStorageService) -> None:
        self.thought_storage_service = thought_storage_service
        self.prompt_builder: "ThoughtPromptBuilder" | None = None
        self.current_focus_path: str | None = "core"

    async def build_internal_info_block(
        self,
        is_context_switch: bool,
        session: Optional["ChatSession"] = None,
    ) -> str:
        """构建内部信息块。"""
        logger.debug(f"开始构建内部信息块... (上下文切换: {is_context_switch})")

        try:
            latest_thought = await self.thought_storage_service.get_latest_thought_document()
            if not latest_thought:
                return "你刚刚开始思考，还没有任何内部状态历史。"

            report_lines = [
                f"你当前的目标是：【{latest_thought.get('goal') or '无'}】",
                f"你刚才的心情是：{latest_thought.get('mood', '平静')}",
                f"你刚才的内心想法是：{latest_thought.get('think', '...')}",
            ]

            if session and session.interruption_context:
                interruption_report = await self._build_interruption_report(session, latest_thought)
                if interruption_report:
                    report_lines.append(interruption_report)
                session.interruption_context = None
            else:
                action_payload = latest_thought.get("action_payload", {})
                action_desc = self._build_action_desc(action_payload.get("action"))
                control_desc = self._build_control_desc(action_payload.get("consciousness_control"), is_context_switch, session)
                
                if action_desc:
                    report_lines.append(action_desc)
                if control_desc:
                    prefix = "并且，" if action_desc else ""
                    report_lines.append(prefix + control_desc)
            
            return "\n".join(report_lines)

        except Exception as e:
            logger.error(f"构建内部信息块时发生严重错误: {e}", exc_info=True)
            return "<!-- 内部信息构建失败 -->"

    # (_build_interruption_report, _format_planned_action, _build_action_desc, _format_action_description, _build_control_desc 方法保持不变)
    async def _build_interruption_report(self, session: "ChatSession", latest_thought_doc: dict) -> str:
        context = session.interruption_context
        interrupting_event_doc = context.get("interrupting_event_doc", {})
        if not interrupting_event_doc:
            return "你的行动被一个未知事件打断了。"
        interrupting_event = Event.from_dict(interrupting_event_doc)
        interrupt_text = interrupting_event.get_text_content() or "[非文本消息]"
        interrupt_sender_id = interrupting_event.user_info.user_id if interrupting_event.user_info else "未知用户"
        interrupt_sender_uid = f"未知用户({interrupt_sender_id[:4]})"
        if self.prompt_builder:
            components, _ = await self.prompt_builder.build_prompts_components(session.chat_session_manager.current_focus_path, session)
            pid_to_uid_map = {pid: data['uid_str'] for pid, data in components.user_map.items()}
            interrupt_sender_uid = pid_to_uid_map.get(interrupt_sender_id, interrupt_sender_uid)
        planned_action_desc = self._format_planned_action(latest_thought_doc)
        return (f"{planned_action_desc}\n"
                f"但是，在你正要行动时，{interrupt_sender_uid} 的新消息“{interrupt_text}”"
                f"打断了你，所以你停下了动作。")

    def _format_planned_action(self, thought_doc: dict) -> str:
        action_payload = thought_doc.get("action_payload", {})
        action_part = action_payload.get("action")
        control_part = action_payload.get("consciousness_control")
        descriptions = []
        if action_part and isinstance(action_part, dict):
            if action_part.get("core", {}).get("do_nothing"): descriptions.append("你本来决定不采取任何行动。")
            else:
                try:
                    platform_key, platform_actions = next(iter(action_part.items()))
                    if isinstance(platform_actions, dict):
                        action_name, action_params = next(iter(platform_actions.items()))
                        if platform_key == "qq" and action_name == "send_message":
                            steps = action_params.get("steps", [])
                            texts = [s.get("params", {}).get("content") for s in steps if s.get("command") == "text" and s.get("params", {}).get("content")]
                            if not texts: descriptions.append("你本来想发送一条非文本消息。")
                            elif len(texts) == 1: descriptions.append(f"你本来想做：发言（发言内容为：“{texts[0]}”）")
                            else: descriptions.append(f"你本来想做：发言（发言内容依次为：{'、'.join(f'“{t}”' for t in texts)}）")
                        else: descriptions.append(f"你本来想做：{platform_key}.{action_name}。")
                except: descriptions.append("你本来想执行一个复杂的动作。")
        if control_part and isinstance(control_part, dict):
            try:
                command, params = next(iter(control_part.items()))
                motivation = params.get("motivation", "没有明确动机")
                descriptions.append(f"你本来想转移注意力（指令: {command}），因为：“{motivation}”。")
            except: descriptions.append("你本来想转移注意力。")
        if not descriptions: return "你本来什么也不打算做。"
        return " ".join(descriptions)

    def _build_action_desc(self, action_part: dict | None) -> str:
        if not action_part or not isinstance(action_part, dict): return ""
        if do_nothing_params := action_part.get("core", {}).get("do_nothing"):
            return f'出于你刚才的想法，你决定不采取任何行动，因为："{do_nothing_params.get("motivation", "决定保持沉默")}"'
        try:
            platform_key, platform_actions = next(iter(action_part.items()))
            if isinstance(platform_actions, dict) and platform_actions:
                action_name, action_params = next(iter(platform_actions.items()))
                return self._format_action_description(platform_key, action_name, action_params)
        except Exception as e:
            logger.error(f"解析动作描述失败: {e}, Payload: {action_part}", exc_info=True)
        return "出于你刚才的想法，你执行了一个未被详细记录的动作。"

    def _format_action_description(self, platform_key: str, action_name: str, action_params: Any) -> str:
        if not isinstance(action_params, dict): return f"出于你刚才的想法，你做了：{platform_key}.{action_name}（参数格式异常）。"
        motivation = action_params.get("motivation", "没有明确动机")
        if action_name == "send_message":
            steps = action_params.get("steps", [])
            texts = [s.get("params", {}).get("content") for s in steps if s.get("command") == "text" and s.get("params", {}).get("content")]
            if not texts: return f'出于你刚才的想法，你发送了一条非文本消息\n因为："{motivation}"'
            elif len(texts) == 1: return f"出于你刚才的想法，你做了：发言（发言内容为：“{texts[0]}”）\n因为：\"{motivation}\""
            else: return f"出于你刚才的想法，你做了：发言（发言内容依次为：{'、'.join(f'“{t}”' for t in texts)}）\n因为：\"{motivation}\""
        return f'出于你刚才的想法，你做了：{action_name}\n因为："{motivation}"'

    def _build_control_desc(self, control_payload: dict | None, is_context_switch: bool, session: Optional["ChatSession"]) -> str:
        if not control_payload or not is_context_switch: return ""
        try:
            command, params = next(iter(control_payload.items()))
            motivation = params.get("motivation", "没有明确动机")
            arrival_target = "这个地方"
            if session: arrival_target = f"这个会话({session.conversation_name or session.conversation_id})"
            elif self.current_focus_path and self.current_focus_path != "core":
                path_parts = self.current_focus_path.split(".")
                if len(path_parts) == 1: arrival_target = f"这个平台({path_parts[0]})"
            return f'出于你刚才的想法，你刚刚来到{arrival_target}。\n因为："{motivation}"'
        except: return ""