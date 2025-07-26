# src/core_logic/internal_info_builder.py (完整实现版 v2.0)
from typing import TYPE_CHECKING, Optional

from aicarus_protocols import Event
from src.common.custom_logging.logging_config import get_logger
from src.database.services.thought_storage_service import ThoughtStorageService

if TYPE_CHECKING:
    from src.core_logic.prompt_builder import ThoughtPromptBuilder
    from src.focus_chat_mode.chat_session import ChatSession

logger = get_logger(__name__)


class InternalInfoBuilder:
    """负责构建AI纯粹的内部信息块 (v2.0 结构化快照版)."""

    def __init__(self, thought_storage_service: ThoughtStorageService) -> None:
        self.thought_storage_service = thought_storage_service
        self.prompt_builder: ThoughtPromptBuilder | None = None

    async def build_internal_info_block(
        self,
        is_context_switch: bool,
        session: Optional["ChatSession"] = None,
        user_map_from_prompt_builder: dict | None = None,
    ) -> str:
        """构建结构化的 <internal_info> 块，包含意识快照."""
        try:
            latest_thought = await self.thought_storage_service.get_latest_thought_document()
            if not latest_thought:
                return (
                    "<internal_info>\n<!-- 你刚刚开始思考，还没有任何内部状态历史。 -->\n"
                    "</internal_info>"
                )

            snapshot_lines = []

            # 检查中断状态
            if session and session.interruption_context:
                snapshot_lines.append('<snapshot time="T-1" status="INTERRUPTED">')
                snapshot_lines.extend(self._format_thought_content(latest_thought))
                snapshot_lines.append(self._format_planned_action(latest_thought))
                snapshot_lines.append(
                    await self._format_interruption(session, user_map_from_prompt_builder)
                )
                # 中断发生后，清理上下文，避免下次思考时重复报告
                session.interruption_context = None
            else:
                snapshot_lines.append('<snapshot time="T-1" status="COMPLETED">')
                snapshot_lines.extend(self._format_thought_content(latest_thought))
                snapshot_lines.append(self._format_completed_action(latest_thought))

            snapshot_lines.append("</snapshot>")

            # 使用缩进美化输出
            indented_lines = "\n".join(f"    {line}" for line in snapshot_lines)
            return f"<internal_info>\n{indented_lines}\n</internal_info>"

        except Exception as e:
            logger.error(f"构建内部信息块时发生严重错误: {e}", exc_info=True)
            return "<internal_info>\n<!-- 内部信息构建失败 -->\n</internal_info>"

    def _format_thought_content(self, thought_doc: dict) -> list[str]:
        """格式化思想内容（心情、想法、目标）."""
        lines = []
        lines.append(f"<mood>{thought_doc.get('mood', '平静')}</mood>")
        lines.append(f"<think>{thought_doc.get('think', '...')}</think>")
        if goal := thought_doc.get("goal"):
            lines.append(f"<goal>{goal}</goal>")
        return lines

    def _format_planned_action(self, thought_doc: dict) -> str:
        """完整地格式化被中断前计划执行的动作，复现提案逻辑."""
        action_payload = thought_doc.get("action_payload", {})
        action_part = action_payload.get("action")
        control_part = action_payload.get("consciousness_control")
        descriptions = []

        if action_part and isinstance(action_part, dict):
            if action_part.get("core", {}).get("do_nothing"):
                descriptions.append("决定不采取任何行动")
            else:
                try:
                    platform_key, platform_actions = next(iter(action_part.items()))
                    if isinstance(platform_actions, dict):
                        action_name, action_params = next(iter(platform_actions.items()))
                        if platform_key == "qq" and action_name == "send_message":
                            steps = action_params.get("steps", [])
                            texts = [
                                s.get("params", {}).get("content")
                                for s in steps
                                if s.get("command") == "text" and s.get("params", {}).get("content")
                            ]
                            if not texts:
                                descriptions.append("发送一条非文本消息")
                            else:
                                formatted_texts = "、".join(f"“{t}”" for t in texts)
                                descriptions.append(f"发言（内容：{formatted_texts}）")
                        else:
                            descriptions.append(f"执行 {platform_key}.{action_name}")
                except Exception as e:
                    logger.debug(f"解析 planned_action 失败: {e}")
                    descriptions.append("执行一个复杂的动作")

        if control_part and isinstance(control_part, dict):
            try:
                command, params = next(iter(control_part.items()))
                motivation = params.get("motivation", "无")
                descriptions.append(f"转移注意力（指令: {command}，动机: {motivation}）")
            except Exception:
                descriptions.append("转移注意力")

        if not descriptions:
            return "<planned_action>无</planned_action>"

        return f"<planned_action>{' 并且 '.join(descriptions)}</planned_action>"

    def _format_completed_action(self, thought_doc: dict) -> str:
        """格式化已完成的动作及其结果."""
        action_result = thought_doc.get("action_result")
        if not action_result or "决策中未包含任何行动指令" in action_result:
            return "<completed_action>无</completed_action>"

        # 为了XML格式的整洁，对结果进行缩进处理
        indented_result = "\n        ".join(action_result.split("\n"))
        return f"<completed_action>\n        {indented_result}\n    </completed_action>"

    async def _format_interruption(self, session: "ChatSession", user_map: dict | None) -> str:
        """格式化中断信息."""
        context = session.interruption_context
        event_doc = context.get("interrupting_event_doc", {})
        if not event_doc:
            return "<interruption>未知</interruption>"
        # 解析事件文档，提取必要信息
        event = Event.from_dict(event_doc)
        text = self._escape_xml_text(event.get_text_content() or "[非文本消息]")
        sender_id = event.user_info.user_id if event.user_info else "未知"

        sender_uid = f"未知用户({sender_id[:4]})"
        if user_map and sender_id != "未知":
            # 遍历 user_map 找到对应的 uid_str
            for p_id, data in user_map.items():
                if str(p_id) == str(sender_id):
                    sender_uid = data.get("uid_str", sender_uid)
                    break

        lines = [
            "<interruption>",
            f"    <source>user {sender_uid}</source>",
            f"    <content>{text}</content>",
            "</interruption>",
        ]

        # 使用缩进连接字符串
        return "\n        ".join(lines)

    def _escape_xml_text(self, text: str) -> str:
        """对文本进行标准的XML转义，防止破坏结构."""
        text = text.replace("&", "&")
        text = text.replace("<", "<")
        text = text.replace(">", ">")
        text = text.replace('"', "&quot;")
        text = text.replace("'", "&apos;")
        return text
