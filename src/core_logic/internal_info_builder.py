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

    TEMPLATE = """
你当前的目标是：【{goal}】
你刚才的心情是："{mood}"
你刚才的内心想法是："{think}"
{from_think_action}
{and_separator}
{from_think_consciousness_controls}
{if_interruptions}
{action_response}
    """.strip()

    def __init__(self, thought_storage_service: ThoughtStorageService) -> None:
        self.thought_storage_service = thought_storage_service
        # 我们需要一个对 prompt_builder 的引用来获取UID映射，这个需要在 main.py 里注入
        self.prompt_builder: ThoughtPromptBuilder | None = None
        self.current_focus_path: str | None = "core" # 初始化

    async def build_internal_info_block(
        self,
        is_context_switch: bool, # 注意：is_context_switch 现在主要用于控制 control 块的措辞
        session: Optional["ChatSession"] = None,
        handover_result: dict | None = None,
    ) -> str:
        """构建内部信息块。这是所有内心活动报告的唯一出口。"""
        logger.debug(f"开始构建内部信息块... (上下文切换: {is_context_switch})")

        try:
            latest_thought = await self.thought_storage_service.get_latest_thought_document()
            if not latest_thought:
                return "你刚刚开始思考，还没有任何内部状态历史。"

            # 1. 初始化所有模板变量
            template_vars = {
                "goal": latest_thought.get("goal") or "无",
                "mood": latest_thought.get("mood", "平静"),
                "think": latest_thought.get("think", "..."),
                "from_think_action": "",
                "and_separator": "",
                "from_think_consciousness_controls": "",
                "if_interruptions": "",
                "action_response": "",
            }

            if session and session.interruption_context:
                # 如果有中断，只填充中断报告，常规动作描述留空
                template_vars["if_interruptions"] = await self._build_interruption_report(session, latest_thought)
                session.interruption_context = None # 用完即焚
            else:
                # 如果没有中断，正常填充常规动作描述
                action_payload = latest_thought.get("action_payload", {})
                control_payload = action_payload.get("consciousness_control")

                template_vars["from_think_action"] = self._build_action_desc(action_payload)
                template_vars["from_think_consciousness_controls"] = self._build_control_desc(
                    control_payload, is_context_switch, session
                )

                if template_vars["from_think_action"] and template_vars["from_think_consciousness_controls"]:
                    template_vars["and_separator"] = "并且，"

            # 动作结果的填充逻辑保持不变，因为它与中断无关
            template_vars["action_response"] = self._build_action_response_desc(
                latest_thought, handover_result
            )

            rendered_string = self.TEMPLATE.format(**template_vars)
            non_empty_lines = [line.strip() for line in rendered_string.splitlines() if line.strip()]

            return "\n".join(non_empty_lines)

        except Exception as e:
            logger.error(f"构建内部信息块时发生严重错误: {e}", exc_info=True)
            return "<!-- 内部信息构建失败 -->"

    async def _build_interruption_report(self, session: "ChatSession", latest_thought_doc: dict) -> str:
        """【已重构】只生成中断部分的叙事文本，不再重复构建整个块。"""
        context = session.interruption_context
        interrupting_event_doc = context.get("interrupting_event_doc", {})

        if not interrupting_event_doc:
            return "你的行动被一个未知事件打断了。"

        # a. 提取打断事件的关键信息
        interrupting_event = Event.from_dict(interrupting_event_doc)
        interrupt_text = interrupting_event.get_text_content() or "[非文本消息]"
        interrupt_sender_id = (interrupting_event.user_info.user_id if interrupting_event.user_info else "未知用户")

        # b. 获取打断者的UID
        interrupt_sender_uid = "未知UID"
        if self.prompt_builder:
            history_components = await self.prompt_builder._get_external_and_meta_info_blocks(
                "cellular", session.platform, session.conversation_id
            )
            # aicarus_protocols v1.7.0 后，uid_str_to_platform_id_map 移动到了 history_components[2] (PromptComponents)
            if history_components and history_components[2] and history_components[2].uid_str_to_platform_id_map:
                uid_map = history_components[2].uid_str_to_platform_id_map
                pid_to_uid_map = {pid: uid for uid, pid in uid_map.items()}
                interrupt_sender_uid = pid_to_uid_map.get(interrupt_sender_id, f"未知用户({interrupt_sender_id[:4]})")

        # c. 生成“本来想做什么”的描述
        planned_action_desc = self._format_planned_action(latest_thought_doc)

        # d. 组装最终的、连贯的叙事报告
        lines = [planned_action_desc]

        sent_count = session.messages_sent_this_turn or 0
        if sent_count > 0:
            lines.append(
                f"但是，在你发送了 {sent_count} 条消息后，{interrupt_sender_uid} 的新消息"
                f"“{interrupt_text}”打断了你，所以你停下了后续的行动。"
            )
        else:
            lines.append(
                f"但是，在你正要行动时，{interrupt_sender_uid} 的新消息“{interrupt_text}”"
                f"打断了你，所以你停下了动作。"
            )

        return "\n".join(lines)

    def _format_planned_action(self, thought_doc: dict) -> str:
        """【新增辅助方法】专门格式化【计划中】的动作描述，用于中断报告。"""
        action_payload = thought_doc.get("action_payload", {})
        action_part = action_payload.get("action")
        control_part = action_payload.get("consciousness_control")

        descriptions = []

        if action_part and isinstance(action_part, dict):
            if action_part.get("core", {}).get("do_nothing"):
                descriptions.append("你本来决定不采取任何行动。")
            else:
                try:
                    for platform_key, platform_actions in action_part.items():
                        if isinstance(platform_actions, dict) and platform_actions:
                            action_name, action_params = next(iter(platform_actions.items()))
                            if platform_key == "napcat_qq" and action_name == "send_message":
                                planned_count = thought_doc.get("messages_planned", 0)
                                steps = action_params.get("steps", [])
                                texts = [s["params"]["text"] for s in steps if s.get("command") == "text" and s.get("params", {}).get("text")]
                                text_preview = "、".join(f'“{t}”' for t in texts)
                                descriptions.append(f"你本来想发言（计划发送 {planned_count} 条消息，内容大概是：{text_preview}）。")
                            else:
                                descriptions.append(f"你本来想做：{platform_key}.{action_name}。")
                except (StopIteration, AttributeError, TypeError):
                    descriptions.append("你本来想执行一个复杂的动作。")

        if control_part and isinstance(control_part, dict):
            try:
                command, params = next(iter(control_part.items()))
                motivation = params.get("motivation", "没有明确动机")
                descriptions.append(f"你本来想转移注意力（指令: {command}），因为：“{motivation}”。")
            except (StopIteration, AttributeError, TypeError):
                descriptions.append("你本来想转移注意力。")

        if not descriptions:
            return "你本来什么也不打算做。"

        return " ".join(descriptions)

    def _build_action_desc(self, action_payload: dict | None) -> str:
        """构建【基于想法的动作】描述。"""
        if not action_payload:
            return "" # 无动作，返回空

        # 我们只关心 "action" 键，忽略 "consciousness_control"
        action_part = action_payload.get("action")
        if not action_part or not isinstance(action_part, dict):
            return ""

        # 检查是否为 "do_nothing"
        if do_nothing_params := action_part.get("core", {}).get("do_nothing"):
            motivation = do_nothing_params.get("motivation", "决定保持沉默")
            return f'出于你刚才的想法，你决定不采取任何行动，因为："{motivation}"'

        # 尝试解析第一个具体的动作
        try:
            # 遍历平台的key ('core', 'napcat_qq', etc.)
            for platform_key, platform_actions in action_part.items():
                if isinstance(platform_actions, dict) and platform_actions:
                    # 找到第一个动作和它的参数
                    action_name, action_params = next(iter(platform_actions.items()))
                    # 【关键修复】确保 action_params 是字典
                    if isinstance(action_params, dict):
                        motivation = action_params.get("motivation", "没有明确动机")
                        return f'出于你刚才的想法，你做了：{platform_key}.{action_name}\n因为："{motivation}"'
                    else:
                        # 这种情况不应该发生，但作为保护
                        logger.warning(f"解析动作参数时发现非字典类型: {action_params}")
                        return f'出于你刚才的想法，你做了：{platform_key}.{action_name}（参数格式异常）。'

        except (StopIteration, AttributeError, TypeError) as e:
            logger.warning(f"解析动作描述时遇到非预期结构，将回退。错误: {e}, Payload: {action_part}")

        # 如果遍历完所有平台都没有找到有效动作，或者解析失败，提供一个无害的回退
        return "出于你刚才的想法，你执行了一个未被详细记录的动作。"

    def _build_control_desc(self, control_payload: dict | None, is_context_switch: bool, session: Optional["ChatSession"]) -> str:
        """构建【注意力控制】描述。"""
        if not control_payload or not is_context_switch:
            # 只有在上下文切换时才显示此块
            return ""

        try:
            command, params = next(iter(control_payload.items()))
            motivation = params.get("motivation", "没有明确动机")

            arrival_target = "这个地方"
            if session:
                arrival_target = f"这个会话({session.conversation_name or session.conversation_id})"
            elif self.current_focus_path and self.current_focus_path != "core":
                path_parts = self.current_focus_path.split('.')
                if len(path_parts) == 1:
                    arrival_target = f"这个平台({path_parts[0]})"

            return f'出于你刚才的想法，你刚刚来到{arrival_target}。\n因为："{motivation}"'
        except (StopIteration, AttributeError):
            return ""

    def _build_action_response_desc(self, latest_thought: dict, handover_result: dict | None) -> str:
        """构建【动作结果】描述。"""
        action_result_text = None
        action_name = None

        # 优先使用交接来的结果，这是最可靠的
        if handover_result:
            action_result_text = handover_result.get("result_text")
            action_name = handover_result.get("action_name")
        # 其次，检查思想点本身是否有 action_result
        elif thought_action_result := latest_thought.get("action_result"):
            action_result_text = thought_action_result
            # 从思想点的 action_payload 中精确地找到被执行的动作名称
            try:
                action_payload = latest_thought.get("action_payload", {})
                action_part = action_payload.get("action", {})
                if action_part:
                    platform, actions = next(iter(action_part.items()))
                    action_name, _ = next(iter(actions.items()))
            except (StopIteration, AttributeError):
                action_name = "某个动作"

        if not action_result_text or not action_name:
            return ""

        # 过滤掉 action_handler 写入的“无需执行”这类占位信息
        if "决策中未包含任何行动指令" in action_result_text:
            return ""

        return (f'<action_response>\n'
                f'你刚才的行动 "{action_name}" 成功了，返回了以下信息：\n'
                f'{action_result_text}\n'
                f'</action_response>')

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
