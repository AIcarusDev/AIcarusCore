# src/action/components/message_builder.py
import asyncio
import base64
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

    它能读懂LLM用“链式指令”（steps数组）写的“操作步骤”,
    然后把这些步骤翻译成一条或多条可以发送给适配器的标准消息.
    它不再处理中断逻辑，因为中断由更高层的竞速机制处理.
    """

    def __init__(self, session: "ChatSession", motivation: str | None) -> None:
        self.session = session
        self.motivation = motivation
        self.action_handler = session.action_handler
        self.platform_id = session.platform
        self.conversation_info = ConversationInfo(
            conversation_id=session.conversation_info.conversation_id,
            type=session.conversation_type,
        )
        self._current_segments: list[Seg] = []

    async def process_steps(self, steps: list[dict]) -> bool:
        """核心工作方法。它会一步步阅读指令清单（steps），并执行翻译.

        在竞速模式下，它不再检查中断信号.
        """
        logger.info(
            f"MessageBuilder 开始为会话 {self.conversation_info.conversation_id} "
            f"处理 {len(steps)} 个指令步骤..."
        )
        any_message_sent = False

        for i, step in enumerate(steps):
            command = step.get("command")
            params = step.get("params", {})

            if command == "text":
                self._add_text(params.get("content"))
            elif command == "at":
                self._add_at(params.get("user_id"))
            elif command == "reply":
                self._add_reply(params.get("message_id"))
            elif command == "sticker":
                await self._add_sticker(params.get("sticker_id"))

            # 遇到“发送并换行”指令，或者这是最后一步了, 且工作台上有内容
            if (command == "send_and_break" or (i == len(steps) - 1)) and self._current_segments:
                success = await self._send_current_message()
                if success:
                    any_message_sent = True
                self._clear_segments()

        return any_message_sent

    def _calculate_typing_delay(self, text: str) -> float:
        """计算模拟打字延迟 (逻辑保持不变)."""
        key_delay_min = 0.06
        key_delay_max = 0.18
        char_selection_delay_min = 0.1
        char_selection_delay_max = 0.3
        space_pause = 0.1
        punctuation_pause_min = 0.4
        punctuation_pause_max = 0.9
        punctuation_to_pause = "，。！？；、,."
        initial_thinking_min = 0.3
        initial_thinking_max = 0.8
        max_total_delay = 25.0

        if not text:
            return 0.0

        total_delay = random.uniform(initial_thinking_min, initial_thinking_max)
        for char in text:
            if "\u4e00" <= char <= "\u9fff":
                try:
                    p_list = pinyin(char, style=Style.NORMAL)
                    p_str = p_list[0][0]
                    for _ in p_str:
                        total_delay += random.uniform(key_delay_min, key_delay_max)
                    total_delay += random.uniform(
                        char_selection_delay_min, char_selection_delay_max
                    )
                except IndexError:
                    total_delay += 0.2
            elif "a" <= char.lower() <= "z":
                total_delay += random.uniform(key_delay_min, key_delay_max)
            elif char in punctuation_to_pause:
                total_delay += random.uniform(punctuation_pause_min, punctuation_pause_max)
            elif char.isspace():
                total_delay += space_pause
            else:
                total_delay += random.uniform(key_delay_min, key_delay_max)

        if len(text) > 10 and random.random() < 0.15:
            correction_time = random.uniform(0.5, 1.2)
            total_delay += correction_time

        return min(total_delay, max_total_delay)

    def _add_text(self, text: str | None) -> None:
        if text:
            logger.debug(f"添加文字: '{text}'")
            self._current_segments.append(SegBuilder.text(text))

    def _add_at(self, user_id: str | None) -> None:
        if user_id:
            logger.debug(f"添加@: {user_id}")
            self._current_segments.append(SegBuilder.at(user_id=user_id))
            self._current_segments.append(SegBuilder.text(" "))

    def _add_reply(self, message_id: str | None) -> None:
        if message_id:
            logger.debug(f"添加引用回复: {message_id}")
            self._current_segments.append(SegBuilder.reply(message_id))

    def _clear_segments(self) -> None:
        logger.debug("清空当前消息段列表。")
        self._current_segments = []

    async def _add_sticker(self, sticker_id: str | None) -> None:
        """根据表情包ID，查找文件，将其编码为Base64，并构建一个标准的image消息段."""
        if not sticker_id:
            logger.warning("MessageBuilder: sticker 指令缺少 sticker_id 参数。")
            return

        # --- vvv 重构后的核心逻辑 vvv ---
        if not self.action_handler or not self.action_handler.sticker_service:
            logger.error("MessageBuilder: StickerService 未初始化，无法发送表情包。")
            self._add_text("[系统提示：表情包系统出现故障]")
            return

        filepath = await self.action_handler.sticker_service.get_sticker_file_path(
            platform_id=self.platform_id, sticker_id=sticker_id
        )
        # --- ^^^ 重构后的核心逻辑 ^^^ ---

        if not filepath:
            # get_sticker_file_path 内部已经记录了详细错误，这里只做回退
            self._add_text(f"[系统提示：我想发送表情包'{sticker_id}'，但我好像没有这个表情包]")
            return

        try:
            if not filepath.exists():
                raise FileNotFoundError

            logger.debug(f"添加表情包: {sticker_id} (路径: {filepath})。")

            # 1. 读取文件内容
            with open(filepath, "rb") as f:
                image_bytes = f.read()

            # 2. Base64 编码
            base64_data = base64.b64encode(image_bytes).decode("utf-8")

            # 3. 创建一个标准的 'image' 消息段，而不是自定义的 'sticker' 段
            #    我们用 'summary' 字段来告诉 Adapter 这是一个表情包
            image_seg_data = {
                "base64": base64_data,
                "summary": "sticker",
            }
            self._current_segments.append(Seg(type="image", data=image_seg_data))

        except FileNotFoundError:
            logger.error(f"MessageBuilder: 表情包文件 '{filepath}' 不存在！")
            self._add_text(f"[系统提示：我想发送表情包'{sticker_id}'，但它的文件好像丢了]")
        except Exception as e:
            logger.error(f"MessageBuilder: 准备表情包 '{filepath}' 时出错: {e}", exc_info=True)
            self._add_text(f"[系统提示：发送表情包'{sticker_id}'时遇到了技术问题]")

    async def _send_current_message(self) -> bool:
        """将工作台上拼接好的所有消息段打包发送.

        现在它会处理 ActionResult 并从中提取 action_id 存入 session.
        """
        if not self._current_segments:
            return False

        if any(seg.type == "text" for seg in self._current_segments):
            text_to_send = "".join(
                seg.data.get("text", "") for seg in self._current_segments if seg.type == "text"
            ).strip()
            if text_to_send:
                typing_delay = self._calculate_typing_delay(text_to_send)
                logger.debug(
                    f"[{self.conversation_info.conversation_id}] 模拟打字: "
                    f"'{text_to_send[:20]}...'，"
                    f"预计耗时 {typing_delay:.2f} 秒..."
                )
                await asyncio.sleep(typing_delay)

        logger.info(f"准备发送拼接好的消息，包含 {len(self._current_segments)} 个消息段。")

        bot_profile = await self.session.get_bot_profile()
        correct_bot_id = bot_profile.get("user_id", self.session.bot_id)

        logger.debug(
            f"[{self.conversation_info.conversation_id}] MessageBuilder 正在使用 bot_id "
            f"'{correct_bot_id}' 来执行 send_message 动作。"
        )

        # execute_simple_action 现在返回 ActionResult 对象
        action_result = await self.action_handler.execute_simple_action(
            platform_id=self.platform_id,
            action_name="send_message",
            params={
                "conversation_id": self.conversation_info.conversation_id,
                "conversation_type": self.conversation_info.type,
                "content": [seg.to_dict() for seg in self._current_segments],
            },
            bot_id=correct_bot_id,
            description="由MessageBuilder拼接并发送",
            motivation=self.motivation,
        )

        if action_result.is_success:
            logger.info(f"消息发送成功，回执: {action_result.payload}")
            # 从 ActionResult 对象中获取 action_id
            if action_result.action_id:
                self.session.sent_action_ids_this_turn.append(action_result.action_id)
                logger.debug(
                    f"[{self.session.conversation_id}] 动作ID '{action_result.action_id}' 已记录."
                )
            else:
                logger.warning(
                    f"[{self.session.conversation_id}] 消息发送成功，"
                    f"但未能从 ActionResult 中获取到 action_id!"
                )

            self.session.consecutive_bot_messages_count += 1
            await asyncio.sleep(random.uniform(0.5, 1.5))
        else:
            logger.error(f"消息发送失败，原因: {action_result.error_message[:100]}...")

        return action_result.is_success
