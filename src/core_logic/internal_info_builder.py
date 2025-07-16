# 文件: src/core_logic/internal_info_builder.py

from typing import TYPE_CHECKING

from aicarus_protocols import Event
from src.common.custom_logging.logging_config import get_logger
from src.database.services.thought_storage_service import ThoughtStorageService

if TYPE_CHECKING:
    from src.core_logic.prompt_builder import ThoughtPromptBuilder
    from src.focus_chat_mode.chat_session import ChatSession

logger = get_logger(__name__)


class InternalInfoBuilder:
    """负责构建和格式化AI的内部信息块（internal_info_block）。
    它现在支持中断记忆的特殊处理，能够生成详细的被打断报告。
    """

    def __init__(self, thought_storage_service: ThoughtStorageService):
        self.thought_storage_service = thought_storage_service
        # 我们需要一个对 prompt_builder 的引用来获取UID映射，这个需要在 main.py 里注入
        self.prompt_builder: ThoughtPromptBuilder | None = None

    async def build_internal_info_block(self, is_context_switch: bool, session: ChatSession | None = None) -> str:
        """构建内部信息块。这是所有内心活动报告的唯一出口。
        """
        logger.debug(f"开始构建内部信息块... (上下文切换: {is_context_switch})")
        try:
            # 1. 优先检查中断记忆，这是最高优先级的叙事
            if session and session.interruption_context:
                logger.info(f"[{session.conversation_id}] 检测到中断记忆，正在生成特殊报告...")
                report = await self._build_interruption_report(session)
                # 使用后立即清除，这是一次性记忆
                session.interruption_context = None
                return report

            # 2. 如果没有中断，走正常流程
            latest_thought_doc = await self.thought_storage_service.get_latest_thought_document()
            if not latest_thought_doc:
                logger.warning("思想链为空，返回初始文本。")
                return "你刚刚开始思考，还没有任何内部状态历史。"

            # 3. 处理正常的、非中断的思考历史
            goal = latest_thought_doc.get("goal") or "无"
            mood = latest_thought_doc.get("mood", "平静")
            think = latest_thought_doc.get("think", "...")

            # 使用新的、更详细的动作描述格式化方法
            action_desc = self._format_previous_action(latest_thought_doc)
            motivation = latest_thought_doc.get("action_payload", {}).get("napcat_qq", {}).get("send_message", {}).get("motivation") or latest_thought_doc.get("action_payload", {}).get("core", {}).get("web_search", {}).get("motivation")
            action_result = latest_thought_doc.get("action_result")

            lines = [
                f"你当前的目标是：【{goal}】",
                f'你刚才的心情是："{mood}"',
                f'你刚才的内心想法是："{think}"',
                action_desc,
            ]
            if motivation:
                lines.append(f'因为："{motivation}"')
            if action_result:
                lines.append(f"行动的结果是：{action_result}")

            return "\n".join(lines)

        except Exception as e:
            logger.error(f"构建内部信息块时发生严重错误: {e}", exc_info=True)
            return "<!-- 内部信息构建失败 -->"

    async def _build_interruption_report(self, session: "ChatSession") -> str:
        """根据中断记忆，构建符合您预期的、详细的被打断报告。"""
        context = session.interruption_context
        interrupting_event_doc = context.get("interrupting_event_doc", {})

        # 1. 获取上一轮的思考，这是我们报告的基础
        latest_thought_doc = await self.thought_storage_service.get_latest_thought_document()
        if not latest_thought_doc:
            return "你似乎被打断了，但之前的思考已经记不清了。"

        # 情况1：思考时被打断，完美复现您文档中的“无事发生”
        if context.get("was_interrupted_while_thinking"):
            logger.info(f"[{session.conversation_id}] 正在生成“思考时被打断”的报告，将回滚到上一状态。")
            # 我们直接复用正常流程，但是传入的是倒数第二个思想点
            # （注意：这个逻辑需要 get_second_latest_thought_document，暂时简化）
            # 简化版：直接返回上一轮的完整描述
            return await self.build_internal_info_block(False, None)

        # 情况2 & 3：发送时被打断
        if context.get("was_interrupted_while_sending"):
            logger.info(f"[{session.conversation_id}] 正在生成“发送时被打断”的报告...")
            # 提取打断事件的关键信息
            interrupting_event = Event.from_dict(interrupting_event_doc)
            interrupt_text = interrupting_event.get_text_content()
            interrupt_sender_id = interrupting_event.user_info.user_id if interrupting_event.user_info else "未知用户"

            # 获取打断者的UID
            interrupt_sender_uid = "未知UID"
            if self.prompt_builder:
                # 获取最新的UID映射
                history_components = await self.prompt_builder._get_external_and_meta_info_blocks("cellular", session.platform, session.conversation_id)
                uid_map = history_components[2].uid_str_to_platform_id_map if history_components[2] else {}
                for uid, pid in uid_map.items():
                    if pid == interrupt_sender_id:
                        interrupt_sender_uid = uid
                        break

            # 组装报告
            mood = latest_thought_doc.get("mood", "平静")
            think = latest_thought_doc.get("think", "...")
            goal = latest_thought_doc.get("goal", "无")
            motivation = latest_thought_doc.get("action_payload", {}).get("napcat_qq", {}).get("send_message", {}).get("motivation")

            lines = [
                f"你当前的目标是：【{goal}】",
                f'你刚才的心情是："{mood}"。',
                f'你刚才的内心想法是："{think}"。',
                self._format_planned_action(latest_thought_doc), # 使用新的、更详细的“计划”描述
            ]
            if motivation:
                lines.append(f'因为："{motivation}"')

            sent_count = session.messages_sent_this_turn
            if sent_count > 0:
                # 情况2
                lines.append(f"但是，在你发送到第 {sent_count} 条消息后，{interrupt_sender_uid} 的新消息“{interrupt_text}”似乎让你感觉有一点意外，所以你停下了后续的消息发送。")
            else:
                # 情况3
                lines.append(f"但是，你还没有开始发言，{interrupt_sender_uid} 的新消息“{interrupt_text}”似乎让你感觉有一点意外，所以你停下了发言的动作。")

            return "\n".join(lines)

        return "<!-- 未知的中断类型 -->"

    def _format_previous_action(self, thought_doc: dict) -> str:
        """格式化【已完成】的动作描述"""
        action_payload = thought_doc.get("action_payload")
        if not action_payload or not action_payload.get("napcat_qq"): # 简化，只处理QQ平台的
            return "出于这个想法，你决定不采取任何行动。"

        action_data = action_payload.get("napcat_qq")
        action_name = next(iter(action_data))

        if action_name == "send_message":
            sent_count = thought_doc.get("messages_sent", 0)
            if sent_count > 0:
                # 未来可以做得更精细，把发送内容也记录下来
                return f'出于这个想法，你做了：发言（并且发送了 {sent_count} 条消息）'
            else:
                return "出于这个想法，你最终决定不发言。"
        else:
            return f"出于这个想法，你做了：{action_name}"

    def _format_planned_action(self, thought_doc: dict) -> str:
        """格式化【计划中】的动作描述，用于中断报告，完美复现您的文档要求。"""
        action_payload = thought_doc.get("action_payload")
        if not action_payload or not action_payload.get("napcat_qq"):
            return "你本来什么也不打算做。"

        action_data = action_payload["napcat_qq"]
        action_name = next(iter(action_data))

        if action_name == "send_message":
            planned_count = thought_doc.get("messages_planned", 0)
            steps = action_data.get("send_message", {}).get("steps", [])
            # 提取所有文本内容
            texts = [s["params"]["text"] for s in steps if s.get("command") == "text" and s.get("params", {}).get("text")]
            text_preview = '、'.join(f'"{t}"' for t in texts)
            return f'出于这个想法，你做了：发言（并且本来想发送 {planned_count} 条消息，依次是{text_preview}）'
        else:
            return f"出于这个想法，你本来想做：{action_name}"
