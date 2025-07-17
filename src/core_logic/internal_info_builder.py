# 文件: src/core_logic/internal_info_builder.py

from typing import TYPE_CHECKING, Optional

from aicarus_protocols import Event
from src.common.custom_logging.logging_config import get_logger
from src.database.services.thought_storage_service import ThoughtStorageService

if TYPE_CHECKING:
    from src.core_logic.prompt_builder import ThoughtPromptBuilder
    from src.focus_chat_mode.chat_session import ChatSession

logger = get_logger(__name__)


class InternalInfoBuilder:
    """负责构建和格式化AI的内部信息块（internal_info_block）.

    它现在支持中断记忆的特殊处理，能够生成详细的被打断报告.
    """

    def __init__(self, thought_storage_service: ThoughtStorageService) -> None:
        self.thought_storage_service = thought_storage_service
        # 我们需要一个对 prompt_builder 的引用来获取UID映射，这个需要在 main.py 里注入
        self.prompt_builder: ThoughtPromptBuilder | None = None

    async def build_internal_info_block(
        self, is_context_switch: bool, session: Optional["ChatSession"] = None
    ) -> str:
        """构建内部信息块。这是所有内心活动报告的唯一出口."""
        logger.debug(f"开始构建内部信息块... (上下文切换: {is_context_switch})")
        try:
            # 1. 优先检查中断记忆，这是最高优先级的叙事
            if session and session.interruption_context:
                logger.info(f"[{session.conversation_id}] 检测到中断记忆，正在生成特殊报告...")
                report = await self._build_interruption_report(session)
                # 使用后立即清除，这是一次性记忆
                session.interruption_context = None
                return report

            latest_thought_doc = await self.thought_storage_service.get_latest_thought_document()

            # 2. 如果是上下文切换，生成特殊的“刚刚抵达”报告
            if is_context_switch:
                logger.info("检测到上下文切换，生成“刚刚抵达”的内部信息报告。")
                if not latest_thought_doc:
                    # 这是一个不应该发生的状态。如果发生，说明有严重的逻辑错误。
                    # 我们不再静默处理，而是抛出异常，让主循环捕获它。
                    critical_error_msg = "状态不一致：在上下文切换时，未能找到上一个思想点！"
                    logger.critical(critical_error_msg)
                    raise RuntimeError(critical_error_msg)

                # 提取触发切换的动机
                motivation = "未知原因"
                control_payload = latest_thought_doc.get("action_payload", {}).get("consciousness_control")
                if control_payload and isinstance(control_payload, dict):
                    command, params = next(iter(control_payload.items()))
                    motivation = params.get("motivation", f"执行 {command} 指令")

                # 复用上一轮的核心状态
                goal = latest_thought_doc.get("goal") or "无"
                mood = latest_thought_doc.get("mood", "平静")
                think = latest_thought_doc.get("think", "...")

                # 判断是抵达平台还是会话
                arrival_target = "这个平台"
                if session: # 如果有 session 对象，说明已进入底层会话
                    arrival_target = "这个会话"

                lines = [
                    f"你当前的目标是：【{goal}】",
                    f'你刚才的心情是："{mood}"',
                    f'你刚才的内心想法是："{think}"',
                    f"出于这个想法，你刚刚来到{arrival_target}。",
                    f'因为："{motivation}"'
                ]
                return "\n".join(lines)

            # 3. 如果没有中断且不是上下文切换，走正常流程
            if not latest_thought_doc:
                logger.warning("思想链为空，返回初始文本。")
                return "你刚刚开始思考，还没有任何内部状态历史。"

            goal = latest_thought_doc.get("goal") or "无"
            mood = latest_thought_doc.get("mood", "平静")
            think = latest_thought_doc.get("think", "...")
            # 生成上一个动作的描述（如果有的话）
            action_desc = self._format_previous_action(latest_thought_doc)
            action_payload = latest_thought_doc.get("action_payload", {})

            # 尝试从多个潜在位置提取动机
            motivation = None
            if action_payload.get("napcat_qq"):
                send_message_action = action_payload["napcat_qq"].get("send_message", {})
                motivation = send_message_action.get("motivation")
            if not motivation and action_payload.get("core"):
                web_search_action = action_payload["core"].get("web_search", {})
                motivation = web_search_action.get("motivation")

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
        """根据中断记忆，构建符合您预期的、详细的被打断报告."""
        context = session.interruption_context
        interrupting_event_doc = context.get("interrupting_event_doc", {})

        # 1. 获取上一轮的思考，这是我们报告的基础
        #    对于中断来说，最新的思想点就是被打断的那个计划，所以我们直接获取它
        latest_thought_doc = await self.thought_storage_service.get_latest_thought_document()
        if not latest_thought_doc:
                    # 这是一个不应该发生的状态。如果发生，说明有严重的逻辑错误。
                    # 我们不静默处理，而是抛出异常，让主循环捕获它。
                    critical_error_msg = "状态不一致：在上下文切换时，未能找到上一个思想点！"
                    logger.critical(critical_error_msg)
                    raise RuntimeError(critical_error_msg)

        # --- 场景一：思考时被打断 ---
        # 这种情况下，AI的思考任务被取消，没有产生新的思想点，所以我们应该描述“被打断前”的状态。
        # 最稳妥的方式是直接回滚到上一个正常的内部信息块。
        if context.get("was_interrupted_while_thinking"):
            logger.info(
                f"[{session.conversation_id}] 正在生成“思考时被打断”的报告，将回滚到上一状态。"
            )
            # 这里的 is_context_switch 设为 False，因为我们不是在切换上下文，而是在恢复被打断前的状态。
            # session 设为 None，以确保它生成的是一个通用的、不依赖于当前会话的报告。
            return await self.build_internal_info_block(is_context_switch=False, session=None)

        # --- 场景二：行动时被打断 (包含发送消息) ---
        # 这是我们新的、更通用的中断处理逻辑。
        if context.get("was_interrupted_while_acting"):
            logger.info(f"[{session.conversation_id}] 正在生成“行动时被打断”的报告...")

            # a. 提取打断事件的关键信息
            interrupting_event = Event.from_dict(interrupting_event_doc)
            interrupt_text = interrupting_event.get_text_content()
            interrupt_sender_id = (
                interrupting_event.user_info.user_id if interrupting_event.user_info else "未知用户"
            )

            # b. 获取打断者的UID，以便LLM理解
            interrupt_sender_uid = "未知UID"
            if self.prompt_builder:
                # 获取最新的UID映射
                # 注意：这里我们重新调用了构建聊天历史的函数，只为了获取UID映射。
                # 这是一个小小的性能开销，但确保了信息的准确性。
                history_components = await self.prompt_builder._get_external_and_meta_info_blocks(
                    "cellular", session.platform, session.conversation_id
                )
                uid_map = (
                    history_components[2].uid_str_to_platform_id_map
                    if history_components and history_components[2]
                    else {}
                )
                # 反转映射以便通过 platform_id 查找 uid
                pid_to_uid_map = {pid: uid for uid, pid in uid_map.items()}
                interrupt_sender_uid = pid_to_uid_map.get(interrupt_sender_id, f"未知用户({interrupt_sender_id[:4]})")

            # c. 从思想点中提取AI被打断前的计划
            mood = latest_thought_doc.get("mood", "平静")
            think = latest_thought_doc.get("think", "...")
            goal = latest_thought_doc.get("goal", "无")

            # 使用 _format_planned_action 来生成“本来想做”的描述
            planned_action_desc = self._format_planned_action(latest_thought_doc)

            # d. 组装最终的、连贯的叙事报告
            lines = [
                f"你当前的目标是：【{goal}】",
                f'你刚才的心情是："{mood}"。',
                f'你刚才的内心想法是："{think}"。',
                planned_action_desc, # "出于这个想法，你本来想做：发言(...)"
            ]

            # 根据是否已经发送了部分消息，选择不同的措辞
            sent_count = session.messages_sent_this_turn or 0 # 从会话状态获取
            if sent_count > 0:
                lines.append(
                    f"但是，在你发送到第 {sent_count} 条消息后，{interrupt_sender_uid} 的新消息"
                    f"“{interrupt_text}”似乎让你感觉有一点意外，所以你停下了后续的行动。"
                )
            else:
                lines.append(
                    f"但是，在你正要行动时，{interrupt_sender_uid} 的新消息“{interrupt_text}”"
                    f"似乎让你感觉有一点意外，所以你停下了动作。"
                )

            return "\n".join(lines)

        # 如果 context 中有旧的 was_interrupted_while_sending，也可以在这里添加一个兼容性处理
        # 但既然我们已经统一为 was_interrupted_while_acting，理论上这里可以不写。
        # 为了健壮性，可以加一个警告。
        if context.get("was_interrupted_while_sending"):
            logger.warning("发现遗留的中断类型 'was_interrupted_while_sending'，请更新逻辑。")
            # 这里可以复用上面的逻辑，或者返回一个通用消息
            return "你在发送消息时被打断了。"


        # 如果是未知的、无法处理的中断类型，返回一个通用提示
        logger.error(f"发现未知的中断类型，上下文: {context}")
        return "你的行动被一个未知类型的事件打断了。"

    def _format_previous_action(self, thought_doc: dict) -> str:
        """格式化【已完成】的动作描述."""
        action_payload = thought_doc.get("action_payload")
        if not action_payload or not action_payload.get("napcat_qq"):  # 简化，只处理QQ平台的
            return "出于这个想法，你决定不采取任何行动。"

        action_data = action_payload.get("napcat_qq")
        action_name = next(iter(action_data))

        if action_name == "send_message":
            sent_count = thought_doc.get("messages_sent", 0)
            if sent_count > 0:
                # 未来可以做得更精细，把发送内容也记录下来
                return f"出于这个想法，你做了：发言（并且发送了 {sent_count} 条消息）"
            else:
                return "出于这个想法，你最终决定不发言。"
        else:
            return f"出于这个想法，你做了：{action_name}"

    def _format_planned_action(self, thought_doc: dict) -> str:
        """格式化【计划中】的动作描述，用于中断报告，完美复现您的文档要求."""
        action_payload = thought_doc.get("action_payload")
        if not action_payload or not action_payload.get("napcat_qq"):
            return "你本来什么也不打算做。"

        action_data = action_payload["napcat_qq"]
        action_name = next(iter(action_data))

        if action_name == "send_message":
            planned_count = thought_doc.get("messages_planned", 0)
            steps = action_data.get("send_message", {}).get("steps", [])
            # 提取所有文本内容
            texts = [
                s["params"]["text"]
                for s in steps
                if s.get("command") == "text" and s.get("params", {}).get("text")
            ]
            text_preview = "、".join(f'"{t}"' for t in texts)
            return (
                f"出于这个想法，你做了：发言（并且本来想发送 {planned_count} 条消息，"
                f"依次是{text_preview}）"
            )
        else:
            return f"出于这个想法，你本来想做：{action_name}"
