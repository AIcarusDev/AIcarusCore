from typing import TYPE_CHECKING

from src.common.custom_logging.logging_config import get_logger
from src.common.json_parser.json_parser import parse_llm_json_response

if TYPE_CHECKING:
    from .chat_session import ChatSession

logger = get_logger(__name__)


class LLMResponseHandler:
    """
    LLM响应处理器，专门解析和验证LLM返回的那些乱七八糟的文本。
    哼，现在它也得学会处理“跳槽”这种麻烦事了。
    """

    def __init__(self, session: "ChatSession") -> None:
        self.session = session
        self.core_logic = session.core_logic
        self.chat_session_manager = session.chat_session_manager

    def parse(self, response_text: str) -> dict | None:
        """从LLM的文本响应中解析出JSON数据。"""
        # // 删掉了 action_to_take 和 action_motivation 的清理逻辑，因为它们已经不存在了。
        parsed = parse_llm_json_response(response_text)
        return parsed

    async def handle_decision(self, parsed_data: dict) -> bool:
        """
        根据解析后的LLM决策，执行所有需要的动作。
        现在它是一个总指挥，负责决定是结束、转移还是继续。
        返回 True 表示会话应该终止（因为结束或转移了），False 表示继续。
        """
        # 1. 检查是否要结束专注
        if parsed_data.get("end_focused_chat") is True:
            logger.info(f"[{self.session.conversation_id}] LLM决策结束专注模式。")
            # // 确保在结束前，把该说的话说完
            await self.session.action_executor.execute_action(parsed_data, self.session.cycler.uid_map)
            await self._trigger_session_deactivation(parsed_data)
            return True

        # 2. 检查是否要转移专注
        # 我用我最喜欢的 .get() 姿势，安全又舒服
        target_conv_id = parsed_data.get("active_focus_on_conversation_id")
        if target_conv_id and isinstance(target_conv_id, str) and target_conv_id.strip().lower() != "null":
            logger.info(f"[{self.session.conversation_id}] LLM决策转移专注到: {target_conv_id}")
            # 转移前也要把话说完
            await self._handle_focus_shift(parsed_data, target_conv_id)
            return True  # 成功发起转移后，当前会话也算结束了，返回True

        # 3. 如果既不结束也不转移，那就继续执行常规动作（发言或记录思考）
        await self.session.action_executor.execute_action(parsed_data, self.session.cycler.uid_map)
        return False  # 常规操作，会话继续，返回False

    async def _handle_focus_shift(self, parsed_data: dict, target_conv_id: str) -> None:
        """处理专注模式的转移。"""
        # a. 如果LLM想在跳槽前说句话，那就先让它说。execute_action 会处理这个。
        #    注意：这里我们假设 execute_action 是阻塞的，会等所有消息都发完。
        await self.session.action_executor.execute_action(parsed_data, self.session.cycler.uid_map)
        logger.info(f"[{self.session.conversation_id}] 转移前的消息已发送完毕。")

        # b. 强制执行最终总结，并把“跳槽动机”塞进去
        logger.info(f"[{self.session.conversation_id}] 准备执行最终总结，为转移做准备。")
        shift_motivation = parsed_data.get("motivation_for_shift", "看到一个更有趣的话题。")
        await self.session.summarization_manager.create_and_save_final_summary(
            shift_motivation=shift_motivation, target_conversation_id=target_conv_id
        )

        # c. 准备交接的“灵魂包裹”
        handover_summary = self.session.current_handover_summary or "我结束了专注，但似乎没什么特别的总结可以交接。"
        last_session_think = parsed_data.get("think", "专注会话结束，无特定最终想法。")
        last_session_mood = parsed_data.get("mood", "平静")

        # d. 呼叫主意识，告诉它我要“灵魂转移”了
        if hasattr(self.core_logic, "trigger_immediate_thought_cycle"):
            self.core_logic.trigger_immediate_thought_cycle(
                handover_summary=handover_summary,
                last_focus_think=last_session_think,
                last_focus_mood=last_session_mood,
                activate_new_focus_id=target_conv_id,
            )

        # e. 让自己这个会话安乐死
        if hasattr(self.chat_session_manager, "deactivate_session"):
            await self.chat_session_manager.deactivate_session(self.session.conversation_id)

    async def _trigger_session_deactivation(self, parsed_data: dict) -> None:
        """触发会话的正常关闭流程。"""
        handover_summary = self.session.current_handover_summary or "我结束了专注，但似乎没什么特别的总结可以交接。"
        last_session_think = parsed_data.get("think", "专注会话结束，无特定最终想法。")
        last_session_mood = parsed_data.get("mood", "平静")

        if hasattr(self.core_logic, "trigger_immediate_thought_cycle"):
            self.core_logic.trigger_immediate_thought_cycle(handover_summary, last_session_think, last_session_mood)

        # 在停用会话时，调用总结
        if self.session.summarization_manager:
            await self.session.summarization_manager.create_and_save_final_summary()

        if hasattr(self.chat_session_manager, "deactivate_session"):
            await self.chat_session_manager.deactivate_session(self.session.conversation_id)