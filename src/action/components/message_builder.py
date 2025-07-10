# D:\Aic\AIcarusCore\src\action\components\message_builder.py
import random
import asyncio
from typing import TYPE_CHECKING, Any, List

from aicarus_protocols import ConversationInfo, Seg, SegBuilder
from src.common.custom_logging.logging_config import get_logger

if TYPE_CHECKING:
    from src.action.action_handler import ActionHandler
    from src.focus_chat_mode.chat_session import ChatSession

logger = get_logger(__name__)

class MessageBuilder:
    """
    一个专门的“消息翻译官”。
    它能读懂LLM用“链式指令”（steps数组）写的“操作步骤”，
    然后把这些步骤翻译成一条或多条可以发送给适配器的标准消息。
    """

    def __init__(self, session: "ChatSession", motivation: str | None):
        """
        初始化翻译官，现在它直接为整个ChatSession服务。

        Args:
            session: 当前的ChatSession实例。
        """
        self.session = session
        self.motivation = motivation
        self.action_handler = session.action_handler
        self.platform_id = session.platform
        self.conversation_info = ConversationInfo(
            conversation_id=session.conversation_id,
            type=session.conversation_type
        )
        self._current_segments: List[Seg] = []

    async def process_steps(self, steps: List[dict]) -> bool:
        """
        这是翻译官的核心工作方法。它会一步步阅读指令清单（steps），并执行翻译。

        Args:
            steps: 一个包含指令的列表，LLM的决策结果。

        Returns:
            bool: 如果整个过程至少成功发送了一条消息，则返回True。
        """
        logger.info(f"MessageBuilder 开始为会话 {self.conversation_info.conversation_id} 处理 {len(steps)} 个指令步骤...")

        any_message_sent = False

        for i, step in enumerate(steps):
            command = step.get("command")
            params = step.get("params", {})

            if command == "text":
                self._add_text(params.get("text"))
            elif command == "at":
                self._add_at(params.get("at"))
            elif command == "reply":
                self._add_reply(params.get("reply"))
            # 在这里为未来新的指令（如 image, face）预留 elif
            # elif command == "image":
            #     self._add_image(params.get("image"))
            # 遇到“发送并换行”指令，或者这是最后一步了
            if command == "send_and_break" or (i == len(steps) - 1):
                # 检查工作台上是否有内容需要发送
                if self._current_segments:
                    success = await self._send_current_message()
                    if success:
                        any_message_sent = True
                    # 发送完后，清空工作台，准备下一条消息
                    self._clear_segments()

        logger.info(f"MessageBuilder 指令处理完毕。共发送消息: {any_message_sent}")
        return any_message_sent

    # 这个方法迁移到这里了，处理打字延迟的计算
    # 逻辑与之前一样，只是现在放在了 MessageBuilder 类里，
    def _calculate_typing_delay(self, text: str) -> float:
        """计算模拟打字延迟.

        这个方法会根据文本内容计算一个模拟打字的延迟时间，
        以增加人性化的交互体验。它会考虑到文本中的标点符号和普通字符的不同，
        并根据预设的延迟范围计算总的打字时间。

        Args:
            text (str): 要计算打字延迟的文本内容。
        Returns:
            float: 计算出的打字延迟时间，单位为秒。
        """
        # 定义哪些标点需要停顿久一点，假装在思考
        punctuation_to_pause = "，。！？；、,."
        # 普通字/字母的打字延迟
        char_delay_min = 0.2
        char_delay_max = 0.6
        # 遇到标点符号的额外停顿
        punc_delay_min = 0.1
        punc_delay_max = 0.4
        # 封顶延迟，免得一句话等半天
        max_total_delay = 20.0

        total_delay = 0.0
        for char in text:
            if char in punctuation_to_pause:
                total_delay += random.uniform(punc_delay_min, punc_delay_max)
            else:
                total_delay += random.uniform(char_delay_min, char_delay_max)

        # 模拟打错字回退增加时长情况，字数越多越容易打错字
        if len(text) > 10 and random.random() < 0.3:
            total_delay *= 1.1

        # 别睡太久了，懒鬼！
        final_delay = min(total_delay, max_total_delay)
        return final_delay

    def _add_text(self, text: str | None):
        """处理 'text' 指令，往工作台上添加文字。"""
        if text:
            logger.debug(f"添加文字: '{text}'")
            self._current_segments.append(SegBuilder.text(text))

    def _add_at(self, user_id: str | None):
        """处理 'at' 指令，往工作台上添加@某人。"""
        if user_id:
            logger.debug(f"添加@: {user_id}")
            # QQ的@后面最好跟个空格，不然会粘连
            self._current_segments.append(SegBuilder.at(user_id=user_id))
            self._current_segments.append(SegBuilder.text(" "))

    def _add_reply(self, message_id: str | None):
        """处理 'reply' 指令，往工作台上添加引用回复。"""
        if message_id:
            logger.debug(f"添加引用回复: {message_id}")
            self._current_segments.append(SegBuilder.reply(message_id))

    def _clear_segments(self):
        """清空工作台。"""
        logger.debug("清空当前消息段列表。")
        self._current_segments = []

    async def _send_current_message(self) -> bool:
        """
        将工作台上拼接好的所有消息段打包，通过老板（ActionHandler）发送出去。
        """
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

        # 我们使用老板（ActionHandler）提供的那个简单的发动作工具
        # execute_simple_action 内部会处理事件构建和发送
        success, payload = await self.action_handler.execute_simple_action(
            platform_id=self.platform_id,
            action_name="send_message",
            params={
                "conversation_id": self.conversation_info.conversation_id,
                "conversation_type": self.conversation_info.type,
                # 把我们辛辛苦苦拼好的消息段列表变成字典列表
                "content": [seg.to_dict() for seg in self._current_segments]
            },
            description="由MessageBuilder拼接并发送"
        )

        if success:
            logger.info(f"消息发送成功，回执: {payload}")

            self.session.consecutive_bot_messages_count += 1
            self.session.messages_sent_this_turn += 1
            logger.debug(f"[{self.conversation_info.conversation_id}] "
                    f"MessageBuilder报告：成功发送1条消息，"
                    f"consecutive_bot_messages_count 更新为: {self.session.consecutive_bot_messages_count}")
            # 这里可以根据需要，等待一小会儿，模拟人类打字的间隔
            await asyncio.sleep(random.uniform(0.5, 1.5))
        else:
            logger.error(f"消息发送失败，原因: {payload}")

        return success
