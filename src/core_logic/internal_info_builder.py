# src/core_logic/internal_info_builder.py
import json
from typing import TYPE_CHECKING, Optional

from src.common.custom_logging.logging_config import get_logger
from src.database.services.thought_storage_service import ThoughtStorageService

if TYPE_CHECKING:
    from src.core_logic.prompt_builder import ThoughtPromptBuilder
    from src.focus_chat_mode.chat_session import ChatSession

logger = get_logger(__name__)


class InternalInfoBuilder:
    """负责构建AI纯粹的内部信息块."""

    def __init__(self, thought_storage_service: ThoughtStorageService) -> None:
        self.thought_storage_service = thought_storage_service
        self.prompt_builder: ThoughtPromptBuilder | None = None

    async def build_internal_info_block(
        self,
        is_context_switch: bool,
        session: Optional["ChatSession"] = None,
        user_map_from_prompt_builder: dict | None = None,
    ) -> str:
        """构建结构化的 <history_internal_info> 块."""
        try:
            latest_thought = await self.thought_storage_service.get_latest_thought_document()
            if not latest_thought:
                return "\n<!-- 你刚刚开始思考，还没有任何内部状态历史。 -->\n"

            snapshot_lines = []
            action_payload = latest_thought.get("action_payload", {}) or {}

            # 检查中断状态
            if session and session.interruption_context:
                snapshot_lines.append('<snapshot time="T-1" status="INTERRUPTED">')
                snapshot_lines.extend(self._format_thought_content(latest_thought))

                interruption_info = await self._format_interruption(
                    session, user_map_from_prompt_builder
                )
                snapshot_lines.extend(
                    [
                        self._format_completed_action(action_payload),
                        self._format_completed_consciousness_control(action_payload),
                        interruption_info,
                    ]
                )
                session.interruption_context = None
            else:
                snapshot_lines.append('<snapshot time="T-1" status="COMPLETED">')
                snapshot_lines.extend(self._format_thought_content(latest_thought))

                snapshot_lines.extend(
                    [
                        self._format_completed_action(action_payload),
                        self._format_completed_consciousness_control(action_payload),
                    ]
                )

            snapshot_lines.append("</snapshot>")

            indented_lines = "\n".join(f"    {line}" for line in snapshot_lines)
            return f"\n{indented_lines}\n"

        except Exception as e:
            logger.error(f"构建内部信息块时发生严重错误: {e}", exc_info=True)
            return "\n<!-- 内部信息构建失败 -->\n"

    def _format_thought_content(self, thought_doc: dict) -> list[str]:
        """格式化思想内容（心情、想法、意图）."""
        lines = [
            f"<mood>{self._escape_xml_text(thought_doc.get('mood', '平静'))}</mood>",
            f"<think>{self._escape_xml_text(thought_doc.get('think', '...'))}</think>",
        ]
        if intent := thought_doc.get("intent"):
            lines.append(f"<intent>{self._escape_xml_text(intent)}</intent>")
        return lines

    def _format_payload_as_json_string(self, payload: dict | None) -> str:
        """专门解析action的“行动组”.

        如果 payload 为空或不是字典，返回 "None" 字符串。
        现在使用 CDATA 来包裹 JSON，避免过度转义。
        """
        if not payload or not isinstance(payload, dict):
            return "None"
        try:
            # 检查是否是 do_nothing 动作
            # 注意：这里的 payload 可能是 {"core": {"do_nothing": ...}} 或直接是 {"do_nothing": ...}
            if payload.get("do_nothing") or payload.get("core", {}).get("do_nothing"):
                return "None"

            # 格式化 JSON 字符串
            formatted_payload = json.dumps(payload, ensure_ascii=False)

            # 使用 CDATA 块包裹，这是处理 XML 中大段文本的最佳实践
            return f"<![CDATA[{formatted_payload}]]>"
        except Exception as e:
            logger.error(f"格式化 payload 为 JSON CDATA 时出错: {e}")
            # 返回通用错误消息，避免暴露敏感数据
            return "<![CDATA[[格式化错误]]]>"

    def _format_completed_action(self, action_payload: dict) -> str:
        """从完整的 payload 中提取 'action' 部分并格式化."""
        action_part = action_payload.get("action")
        formatted_json = self._format_payload_as_json_string(action_part)
        return f"<completed_action>{formatted_json}</completed_action>"

    def _format_completed_consciousness_control(self, action_payload: dict) -> str:
        """从完整的 payload 中提取 'consciousness_control' 部分并格式化."""
        control_part = action_payload.get("consciousness_control")
        formatted_json = self._format_payload_as_json_string(control_part)
        return (
            f"<completed_consciousness_control>{formatted_json}</completed_consciousness_control>"
        )

    async def _format_interruption(self, session: "ChatSession", user_map: dict | None) -> str:
        """格式化中断信息."""
        context = session.interruption_context
        # ======================== [ 核心改造点 ] ========================
        # 从上下文中获取 Stimulus 对象
        stimulus = context.get("interrupting_stimulus")
        if not stimulus:
            return "<interruption>未知</interruption>"

        # 直接从 Stimulus 对象获取信息
        text = self._escape_xml_text(stimulus.text_content or "[非文本消息]")
        sender_id = stimulus.sender_id or "未知"
        # =============================================================

        sender_uid = f"未知用户({sender_id[:4]})"
        if user_map and sender_id != "未知":
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
        return "\n        ".join(lines)

    def _escape_xml_text(self, text: str) -> str:
        """对文本进行标准的XML转义，防止破坏结构."""
        return (
            text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&apos;")
        )
