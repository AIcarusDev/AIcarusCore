import json
import re
from typing import TYPE_CHECKING

from src.common.custom_logging.logger_manager import get_logger

if TYPE_CHECKING:
    from .chat_session import ChatSession

logger = get_logger(__name__)


class LLMResponseHandler:
    """
    LLM响应处理器，专门解析和验证LLM返回的那些乱七八糟的文本。
    """

    def __init__(self, session: "ChatSession") -> None:
        self.session = session
        self.core_logic = session.core_logic
        self.chat_session_manager = session.chat_session_manager

    def parse(self, response_text: str) -> dict | None:
        """从LLM的文本响应中解析出JSON数据。"""
        if not response_text:
            return None
        match = re.search(r"```json\s*(\{[\s\S]*?\})\s*```", response_text, re.DOTALL)
        if match:
            json_str = match.group(1)
            try:
                return json.loads(json_str)
            except json.JSONDecodeError as e:
                logger.error(
                    f"[{self.session.conversation_id}] 解析被```json包裹的响应时JSONDecodeError: {e}. JSON string: {json_str[:200]}..."
                )
                return None
        else:
            try:
                return json.loads(response_text)
            except json.JSONDecodeError:
                logger.warning(
                    f"[{self.session.conversation_id}] LLM响应不是有效的JSON，且未被```json包裹: {response_text[:200]}"
                )
                return None

    async def handle_end_focus_chat_if_needed(self, parsed_data: dict) -> bool:
        """检查并处理结束专注模式的决策。返回 True 表示应终止循环。"""
        if parsed_data.get("end_focused_chat") is True:
            logger.info(f"[{self.session.conversation_id}] LLM决策结束专注模式。")
            handover_summary = self.session.current_handover_summary or "我结束了专注，但似乎没什么特别的总结可以交接。"
            last_session_think = self.session.last_llm_decision.get("think", "专注会话结束，无特定最终想法。")
            last_session_mood = self.session.last_llm_decision.get("mood", "平静")

            if hasattr(self.core_logic, "trigger_immediate_thought_cycle"):
                self.core_logic.trigger_immediate_thought_cycle(handover_summary, last_session_think, last_session_mood)

            if hasattr(self.chat_session_manager, "deactivate_session"):
                # 在停用会话前，保存最终的总结
                # 注意：这里需要调用 session 里的 summarization_manager
                if self.session.summarization_manager:
                    await self.session.summarization_manager.save_final_summary()
                await self.chat_session_manager.deactivate_session(self.session.conversation_id)

            return True
        return False
