# src/core_logic/internal_info_builder.py
from typing import Any

from src.common.custom_logging.logging_config import get_logger
from src.database.services.thought_storage_service import ThoughtStorageService

logger = get_logger(__name__)


class InternalInfoBuilder:
    """
    负责构建和格式化AI的内部信息块（internal_info_block）。
    该构建器从思想链中提取最新的状态，并将其格式化为统一的、
    流畅的自然语言字符串，供所有层级的Prompt使用。
    """

    def __init__(self, thought_storage_service: ThoughtStorageService):
        """
        初始化InternalInfoBuilder。

        Args:
            thought_storage_service (ThoughtStorageService): 用于访问思想链数据的服务。
        """
        self.thought_storage_service = thought_storage_service

    async def build_internal_info_block(self, is_context_switch: bool = False) -> str:
        """
        构建内部信息块。

        Args:
            is_context_switch (bool): 指示当前是否为上下文切换后的第一轮思考。

        Returns:
            str: 格式化后的内部信息块字符串。
        """
        logger.debug(f"开始构建内部信息块... (上下文切换: {is_context_switch})")
        try:
            latest_thought_doc = await self.thought_storage_service.get_latest_thought_document()

            if not latest_thought_doc:
                logger.warning("思想链为空，返回初始文本。")
                return "你刚刚开始思考，还没有任何内部状态历史。"

            # 从文档中提取所需信息
            goal = latest_thought_doc.get("goal") or "无"
            mood = latest_thought_doc.get("mood")
            think = latest_thought_doc.get("think")
            action = latest_thought_doc.get("action")
            consciousness_control = latest_thought_doc.get("consciousness_control")
            motivation = latest_thought_doc.get("motivation")
            action_response = latest_thought_doc.get("action_response")

            # 如果是上下文切换，优先使用特殊格式
            if is_context_switch and consciousness_control:
                lines = []
                if mood:
                    lines.append(f'你刚才的心情是："{mood}"')
                if think:
                    lines.append(f'你刚才的内心想法是："{think}"')

                control_type = next(iter(consciousness_control))
                if control_type == "focus":
                    lines.append("出于这个想法，你刚刚专注于这个会话。")
                elif control_type == "return":
                    lines.append("出于这个想法，你刚刚返回了上一层。")
                elif control_type == "shift":
                    lines.append("出于这个想法，你刚刚转移到了另一个会话。")
                else:
                    lines.append(f"出于这个想法，你刚刚进行了意识控制：'{control_type}'。")
                
                if motivation:
                    lines.append(f'因为："{motivation}"')
                
                return "\n".join(lines)

            # 正常循环格式
            lines = [f"你当前的目标是：【{goal}】"]

            if mood:
                lines.append(f'你刚才的心情是："{mood}"')
            if think:
                lines.append(f'你刚才的内心想法是："{think}"')

            # 处理动作
            if action and action != "no_action":
                action_name = next(iter(action)) if isinstance(action, dict) else action
                if action_name == "send_message":
                    lines.append("出于这个想法，你选择了发言。")
                else:
                    lines.append(f"出于这个想法，你做了：'{action_name}'。")
            else:
                # 如果上一步不是上下文切换，也不是动作，那就是无动作
                lines.append("出于这个想法，你决定不采取任何行动。")

            if motivation:
                lines.append(f'因为："{motivation}"')

            # 处理动作返回结果
            if action_response:
                action_name_for_response = "未知动作"
                if action and isinstance(action, dict):
                    action_name_for_response = next(iter(action))

                response_content = action_response.get("response", "无返回信息。")
                lines.append("<action_response>")
                lines.append(f"行动 '{action_name_for_response}' 成功了，返回了以下信息：")
                lines.append(str(response_content))
                lines.append("</action_response>")

            return "\n".join(lines)

        except Exception as e:
            logger.error(f"构建内部信息块时发生错误: {e}", exc_info=True)
            return "<!-- 内部信息构建失败 -->"
