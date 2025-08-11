# 文件: src/action/components/message_builder.py
import asyncio
import random
from pathlib import Path
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
        """根据表情包ID，查找文件并构建一个 sticker 消息段."""
        if not sticker_id:
            logger.warning("MessageBuilder: sticker 指令缺少 sticker_id 参数。")
            return

        if not self.action_handler or not self.action_handler.sticker_storage_service:
            logger.error("MessageBuilder: StickerStorageService 未初始化，无法发送表情包。")
            return

        sticker_doc = await self.action_handler.sticker_storage_service.get_sticker_by_id(
            platform=self.platform_id, sticker_id=sticker_id
        )

        if not sticker_doc:
            logger.error(f"MessageBuilder: 找不到编号为 '{sticker_id}' 的表情包。")
            # 也可以选择发送一段错误文本，让AI知道出错了
            self._add_text(f"[系统提示：我想发送表情包'{sticker_id}'，但我好像没有这个表情包]")
            return

        # 确保 action_handler._stickers_dir 已经被初始化
        stickers_dir = getattr(self.action_handler, "_stickers_dir", None)
        if not stickers_dir or not isinstance(stickers_dir, Path):
            logger.error("ActionHandler 中的 _stickers_dir 未正确初始化！")
            return

        filename = sticker_doc.get("filename")
        if not filename:
            logger.error(f"MessageBuilder: 表情包 '{sticker_id}' 在数据库中缺少文件名。")
            return

        filepath = stickers_dir / filename
        logger.debug(f"添加表情包: {sticker_id} (路径: {filepath})")
        # 我们创建一个新的 'sticker' 类型的 Seg，并把文件路径放进去
        # 适配器层会知道如何处理它
        self._current_segments.append(Seg(type="sticker", data={"filepath": str(filepath)}))

    async def _send_current_message(self) -> bool:
        """将工作台上拼接好的所有消息段打包发送.

        现在它会处理 ActionResult 并从中提取 action_id 存入 session.
        """
        if not self._current_segments:
            return False

        has_text = any(seg.type == "text" for seg in self._current_segments)

        if has_text:
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
            logger.error(f"消息发送失败，原因: {action_result.error_message}")

        return action_result.is_success
