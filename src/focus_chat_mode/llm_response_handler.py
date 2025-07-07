# src/focus_chat_mode/llm_response_handler.py
from typing import TYPE_CHECKING

from src.common.custom_logging.logging_config import get_logger
from src.common.json_parser.json_parser import parse_llm_json_response

if TYPE_CHECKING:
    from .chat_session import ChatSession

logger = get_logger(__name__)


class LLMResponseHandler:
    """LLM响应处理器，用于解析和处理来自LLM的响应数据.

    Attributes:
        session (ChatSession): 当前会话的实例。
        core_logic (CoreLogic): 会话的核心逻辑处理器。
        chat_session_manager (ChatSessionManager): 会话管理器，用于处理会话的激活和停用。
    """

    def __init__(self, session: "ChatSession") -> None:
        self.session = session
        self.core_logic = session.core_logic
        self.chat_session_manager = session.chat_session_manager

    def parse(self, response_text: str) -> dict | None:
        """从LLM的文本响应中解析出JSON数据.

        Args:
            response_text (str): LLM返回的文本响应，可能包含JSON格式的数据。

        Returns:
            dict | None: 解析出的JSON数据，如果无法解析则返回None。
        """
        return parse_llm_json_response(response_text)

    async def handle_decision(self, parsed_data: dict) -> bool:
        """处理LLM的决策逻辑.

        Args:
            parsed_data (dict): 从LLM响应中解析出的JSON数据。
        Returns:
            bool: 如果执行了特定的决策操作，则返回True；否则返回False
        """
        if parsed_data.get("end_focused_chat") is True:
            logger.info(f"[{self.session.conversation_id}] LLM决策结束专注模式。")
            await self._trigger_session_deactivation()
            return True

        target_conv_id = parsed_data.get("active_focus_on_conversation_id")
        if (
            target_conv_id
            and isinstance(target_conv_id, str)
            and target_conv_id.strip().lower() != "null"
        ):
            logger.info(f"[{self.session.conversation_id}] LLM决策转移专注到: {target_conv_id}")
            shift_motivation = parsed_data.get("motivation_for_shift", "看到一个更有趣的话题。")
            await self._handle_focus_shift(target_conv_id, shift_motivation)
            return True

        # 3. 啥也不干，让循环继续
        return False

    async def _handle_focus_shift(self, target_conv_id: str, shift_motivation: str) -> None:
        """处理专注模式的转移逻辑.

        这个方法会在专注模式转移时调用，执行以下步骤:
        1. 在当前会话中创建并保存最终摘要。
        2. 如果有目标会话ID，则创建一个新的专注会话。
        3. 触发主意识的思考循环，激活新的专注会话。
        4. 停用当前会话。

        Args:
            target_conv_id (str): 目标会话ID，表示转移到哪个会话。
            shift_motivation (str): 转移的动机，用于生成摘要。
        """
        await self.session.summarization_manager.create_and_save_final_summary(
            shift_motivation=shift_motivation, target_conversation_id=target_conv_id
        )

        # 注意！这里不再需要传递任何参数了！主意识会自己去思想链里找状态。
        if hasattr(self.core_logic, "trigger_immediate_thought_cycle"):
            # // 直接激活 CoreLogic 的新 focus 流程
            # // CoreLogic 会自己处理后续逻辑
            # 直接激活 CoreLogic 的新 focus 流程
            # CoreLogic 会自己处理后续逻辑
            logger.info(
                f"[{self.session.conversation_id}] 请求主意识直接激活新会话: {target_conv_id}"
            )
            await self.session.core_logic._activate_new_focus_session_from_core(target_conv_id)

        await self.chat_session_manager.deactivate_session(self.session.conversation_id)

    async def _trigger_session_deactivation(self) -> None:
        """触发会话的正常关闭流程."""
        # 在停用会话时，调用总结
        await self.session.summarization_manager.create_and_save_final_summary()

        # 触发主意识思考，不再需要传递任何参数
        if hasattr(self.core_logic, "trigger_immediate_thought_cycle"):
            self.core_logic.trigger_immediate_thought_cycle()

        # 停用会话
        await self.chat_session_manager.deactivate_session(self.session.conversation_id)
