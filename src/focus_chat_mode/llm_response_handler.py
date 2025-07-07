# src/focus_chat_mode/llm_response_handler.py
from typing import TYPE_CHECKING

from src.common.custom_logging.logging_config import get_logger
from src.common.json_parser.json_parser import parse_llm_json_response

if TYPE_CHECKING:
    from .chat_session import ChatSession

logger = get_logger(__name__)


class LLMResponseHandler:
    """
    LLM响应处理器，专门解析和验证LLM返回的那些乱七八糟的文本。
    哼，现在我只负责动脑，不负责动手。
    """

    def __init__(self, session: "ChatSession") -> None:
        self.session = session
        self.core_logic = session.core_logic
        self.chat_session_manager = session.chat_session_manager

    def parse(self, response_text: str) -> dict | None:
        """从LLM的文本响应中解析出JSON数据。"""
        return parse_llm_json_response(response_text)

    async def handle_decision(self, parsed_data: dict) -> bool:
        """
        根据解析后的LLM决策，判断是否需要结束或转移会话。
        我只负责判断，不负责执行动作！
        返回 True 表示会话应该终止，False 表示继续。
        """
        if parsed_data.get("end_focused_chat") is True:
            logger.info(f"[{self.session.conversation_id}] LLM决策结束专注模式。")
            await self._trigger_session_deactivation()
            return True

        target_conv_id = parsed_data.get("active_focus_on_conversation_id")
        if target_conv_id and isinstance(target_conv_id, str) and target_conv_id.strip().lower() != "null":
            logger.info(f"[{self.session.conversation_id}] LLM决策转移专注到: {target_conv_id}")
            shift_motivation = parsed_data.get("motivation_for_shift", "看到一个更有趣的话题。")
            await self._handle_focus_shift(target_conv_id, shift_motivation)
            return True

        # 3. 啥也不干，让循环继续
        return False

    async def _handle_focus_shift(self, target_conv_id: str, shift_motivation: str) -> None:
        """处理专注模式的转移。我只负责打电话，不带行李。"""
        # a. 强制执行最终总结，并把“跳槽动机”塞进去
        await self.session.summarization_manager.create_and_save_final_summary(
            shift_motivation=shift_motivation, target_conversation_id=target_conv_id
        )

        # b. 呼叫主意识，告诉它我要“灵魂转移”了
        #    注意！这里不再需要传递任何参数了！主意识会自己去思想链里找状态。
        if hasattr(self.core_logic, "trigger_immediate_thought_cycle"):
            # // 直接激活 CoreLogic 的新 focus 流程
            # // CoreLogic 会自己处理后续逻辑
            # 直接激活 CoreLogic 的新 focus 流程
            # CoreLogic 会自己处理后续逻辑
            logger.info(f"[{self.session.conversation_id}] 请求主意识直接激活新会话: {target_conv_id}")
            await self.session.core_logic._activate_new_focus_session_from_core(target_conv_id)

        # c. 让自己这个会话安乐死
        await self.chat_session_manager.deactivate_session(self.session.conversation_id)

    async def _trigger_session_deactivation(self) -> None:
        """触发会话的正常关闭流程。"""
        # 在停用会话时，调用总结
        await self.session.summarization_manager.create_and_save_final_summary()

        # 触发主意识思考，不再需要传递任何参数
        if hasattr(self.core_logic, "trigger_immediate_thought_cycle"):
            self.core_logic.trigger_immediate_thought_cycle()

        # 停用会话
        await self.chat_session_manager.deactivate_session(self.session.conversation_id)
