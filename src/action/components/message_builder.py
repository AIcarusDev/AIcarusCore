# D:\Aic\AIcarusCore\src\action\components\message_builder.py
import asyncio
import random
from typing import TYPE_CHECKING

from aicarus_protocols import ConversationInfo, Seg, SegBuilder
from pypinyin import Style, pinyin
from src.common.custom_logging.logging_config import get_logger

if TYPE_CHECKING:
    from src.focus_chat_mode.chat_session import ChatSession

logger = get_logger(__name__)


class MessageBuilder:
    """一个专门为ChatSession设计的消息构建器.

    它能读懂LLM用“链式指令”（steps数组）写的“操作步骤”，
    然后把这些步骤翻译成一条或多条可以发送给适配器的标准消息.
    """

    def __init__(self, session: "ChatSession", motivation: str | None) -> None:
        self.session = session
        self.motivation = motivation
        self.action_handler = session.action_handler
        self.platform_id = session.platform
        self.conversation_info = ConversationInfo(
            conversation_id=session.conversation_id, type=session.conversation_type
        )
        self._current_segments: list[Seg] = []

    async def process_steps(self, steps: list[dict]) -> bool:
        """这是核心工作方法。它会一步步阅读指令清单（steps），并执行翻译.

        Args:
            steps: 一个包含指令的列表，LLM的决策结果.

        Returns:
            bool: 如果整个过程至少成功发送了一条消息，则返回True.
        """
        logger.info(
            f"MessageBuilder 开始为会话 {self.conversation_info.conversation_id} "
            f"处理 {len(steps)} 个指令步骤..."
        )

        self.session.messages_planned_this_turn = 1 + sum(
            1 for step in steps if step.get("command") == "send_and_break"
        )
        self.session.messages_sent_this_turn = 0

        any_message_sent = False

        for i, step in enumerate(steps):
            # 检查是否有中断信号
            # 如果有中断，立刻停止处理后续步骤
            if self.session.interruption_context:
                logger.info(
                    f"MessageBuilder 在处理步骤 {i + 1} 前检测到中断信号，停止后续消息发送。"
                )
                break  # 如果已中断，立刻停止

            command = step.get("command")
            params = step.get("params", {})

            if command == "text":
                self._add_text(params.get("content"))
            elif command == "at":
                self._add_at(params.get("user_id"))
            elif command == "reply":
                self._add_reply(params.get("message_id"))
            # 在这里为未来新的指令（如 image, face）预留 elif
            # elif command == "image":
            #     self._add_image(params.get("image"))
            # 遇到“发送并换行”指令，或者这是最后一步了
            # 检查工作台上是否有内容需要发送
            if (command == "send_and_break" or (i == len(steps) - 1)) and self._current_segments:
                # 再次检查是否有中断信号
                if self.session.interruption_context:
                    logger.info("MessageBuilder 在发送消息前检测到中断信号，取消本次发送。")
                    break
                success, sent_action_id = await self._send_current_message()
                if success and sent_action_id:
                    any_message_sent = True
                    # 只有发送成功了，才增加消息计数
                    self.session.messages_sent_this_turn += 1
                    self.session.sent_action_ids_this_turn.append(sent_action_id)
                    logger.debug(
                        f"[{self.session.conversation_id}] "
                        f"成功发送第 {self.session.messages_sent_this_turn} 条消息。"
                    )
                self._clear_segments()

        # 3. 行动流程结束后
        # 如果是因为中断而结束的，保留 messages_sent_this_turn 的值给下一轮思考用
        # 如果是正常结束的，就清理计数器
        if not self.session.interruption_context:
            self.session.messages_planned_this_turn = 0
            self.session.messages_sent_this_turn = 0
            logger.debug(
                f"[{self.session.conversation_id}] MessageBuilder 正常完成，重置发送计数器。"
            )
        else:
            logger.info(
                f"[{self.session.conversation_id}] MessageBuilder 因中断而停止，保留发送计数 "
                f"(已发送: {self.session.messages_sent_this_turn} / "
                f"计划: {self.session.messages_planned_this_turn})。"
            )

        # 唤醒主循环的逻辑移到 ActionHandler 中，由它统一在 finally 中触发
        return any_message_sent

    # 这个方法迁移到这里了，处理打字延迟的计算
    # 逻辑与之前一样，只是现在放在了 MessageBuilder 类里，
    def _calculate_typing_delay(self, text: str) -> float:
        """计算模拟打字延迟.

        这个方法会根据输入的文本内容，计算出一个模拟打字的延迟时间。
        主要考虑以下因素：
        - 中文字符的拼音输入延迟
        - 英文字母的输入延迟
        - 特定标点符号的停顿
        - 空格的微小停顿
        - 整体打字思考时间
        - 模拟打错字和修正的延迟
        - 确保总延迟时间不会过长
        Args:
            text (str): 要计算延迟的文本内容。
        Returns:
            float: 计算出的总延迟时间（秒）。
        """
        # --- 基础延迟参数 ---
        # 模拟敲击单个字母或拼音的延迟（秒）
        key_delay_min = 0.06
        key_delay_max = 0.18

        # --- 中文输入特有参数 ---
        # 模拟在输入法中选择汉字（词）的延迟（秒）
        char_selection_delay_min = 0.1
        char_selection_delay_max = 0.3

        # --- 通用停顿参数 ---
        # 模拟在词语之间（空格）的微小停顿
        space_pause = 0.1
        # 模拟在需要思考的标点符号后的停顿
        punctuation_pause_min = 0.4
        punctuation_pause_max = 0.9
        # 定义哪些标点需要长停顿
        punctuation_to_pause = "，。！？；、,."

        # --- 整体控制参数 ---
        # 模拟在开始打字前的“思考”时间
        initial_thinking_min = 0.3
        initial_thinking_max = 0.8
        # 防止总延迟时间过长，设置封顶值（秒）
        max_total_delay = 25.0

        if not text:
            return 0.0

        # 1. 初始思考延迟
        total_delay = random.uniform(initial_thinking_min, initial_thinking_max)

        # 2. 遍历文本，根据字符类型计算延迟
        for char in text:
            # --- Case 1: 中文字符 ---
            if "\u4e00" <= char <= "\u9fff":
                try:
                    # 获取该汉字的拼音
                    p_list = pinyin(char, style=Style.NORMAL)
                    p_str = p_list[0][0]

                    # 累加输入该拼音所有字母的延迟
                    for _ in p_str:
                        total_delay += random.uniform(key_delay_min, key_delay_max)

                    # 累加选择该汉字的延迟
                    total_delay += random.uniform(
                        char_selection_delay_min, char_selection_delay_max
                    )
                except IndexError:
                    # 对于pypinyin无法处理的罕见字，使用一个固定延迟
                    total_delay += 0.2

            # --- Case 2: 英文字母 ---
            elif "a" <= char.lower() <= "z":
                total_delay += random.uniform(key_delay_min, key_delay_max)

            # --- Case 3: 需要长停顿的标点 ---
            elif char in punctuation_to_pause:
                total_delay += random.uniform(punctuation_pause_min, punctuation_pause_max)

            # --- Case 4: 空格 ---
            elif char.isspace():
                total_delay += space_pause

            # --- Case 5: 其他所有字符 (如数字、普通标点等) ---
            else:
                total_delay += random.uniform(key_delay_min, key_delay_max)

        # 3. 模拟一定概率下的打错字和修正过程
        if len(text) > 10 and random.random() < 0.15:
            correction_time = random.uniform(0.5, 1.2)
            total_delay += correction_time

        # 4. 确保总延迟不超过封顶值
        return min(total_delay, max_total_delay)

    def _add_text(self, text: str | None) -> None:
        """处理 'text' 指令，往工作台上添加文字."""
        if text:
            logger.debug(f"添加文字: '{text}'")
            self._current_segments.append(SegBuilder.text(text))

    def _add_at(self, user_id: str | None) -> None:
        """处理 'at' 指令，往工作台上添加@某人."""
        if user_id:
            logger.debug(f"添加@: {user_id}")
            # QQ的@后面最好跟个空格，不然会粘连
            self._current_segments.append(SegBuilder.at(user_id=user_id))
            self._current_segments.append(SegBuilder.text(" "))

    def _add_reply(self, message_id: str | None) -> None:
        """处理 'reply' 指令，往工作台上添加引用回复."""
        if message_id:
            logger.debug(f"添加引用回复: {message_id}")
            self._current_segments.append(SegBuilder.reply(message_id))

    def _clear_segments(self) -> None:
        """清空工作台."""
        logger.debug("清空当前消息段列表。")
        self._current_segments = []

    async def _send_current_message(self) -> bool:
        """将工作台上拼接好的所有消息段打包，通过老板（ActionHandler）发送出去."""
        if not self._current_segments:
            logger.debug("工作台是空的，无需发送。")
            return False

        # 1. 提取要发送的纯文本，用于计算延迟
        text_to_send = "".join(
            seg.data.get("text", "") for seg in self._current_segments if seg.type == "text"
        ).strip()

        if text_to_send:
            # 2. 计算延迟时间
            typing_delay = self._calculate_typing_delay(text_to_send)
            logger.debug(
                f"[{self.conversation_info.conversation_id}] 模拟打字: '{text_to_send[:20]}...'，"
                f"预计耗时 {typing_delay:.2f} 秒..."
            )
            # 3. 异步“睡眠”，模拟打字过程
            await asyncio.sleep(typing_delay)

        logger.info(f"准备发送拼接好的消息，包含 {len(self._current_segments)} 个消息段。")

        bot_profile = await self.session.get_bot_profile()
        correct_bot_id = bot_profile.get("user_id", self.session.bot_id)

        # 我们使用老板（ActionHandler）提供的那个简单的发动作工具
        # execute_simple_action 内部会处理事件构建和发送
        success, payload = await self.action_handler.execute_simple_action(
            platform_id=self.platform_id,
            action_name="send_message",
            params={
                "conversation_id": self.conversation_info.conversation_id,
                "conversation_type": self.conversation_info.type,
                # 把我们辛辛苦苦拼好的消息段列表变成字典列表
                "content": [seg.to_dict() for seg in self._current_segments],
            },
            bot_id=correct_bot_id,
            description="由MessageBuilder拼接并发送",
        )

        action_id = payload.get("action_id") if isinstance(payload, dict) else None

        if success:
            logger.info(f"消息发送成功，回执: {payload}")

            self.session.consecutive_bot_messages_count += 1
            self.session.messages_sent_this_turn += 1
            logger.debug(
                f"[{self.conversation_info.conversation_id}] "
                f"MessageBuilder报告：成功发送1条消息，"
                f"consecutive_bot_messages_count 更新为: "
                f"{self.session.consecutive_bot_messages_count}"
            )
            # 这里可以根据需要，等待一小会儿，模拟人类打字的间隔
            await asyncio.sleep(random.uniform(0.5, 1.5))
        else:
            logger.error(f"消息发送失败，原因: {payload}")

        return success, action_id
