# src/focus_chat_mode/action_executor.py
import asyncio
import random
import re
import time
import uuid
from typing import TYPE_CHECKING

from aicarus_protocols import Event as ProtocolEvent
from aicarus_protocols.conversation_info import ConversationInfo
from aicarus_protocols.seg import SegBuilder
from aicarus_protocols.user_info import UserInfo

from src.common.custom_logging.logging_config import get_logger
from src.config import config
from src.database.models import DBEventDocument

if TYPE_CHECKING:
    from .chat_session import ChatSession

logger = get_logger(__name__)


class ActionExecutor:
    """
    专门负责执行LLM决策的行动执行官。
    哼，别想让我干别的！
    """

    def __init__(self, session: "ChatSession") -> None:
        self.session = session
        self.action_handler = session.action_handler
        self.event_storage = session.event_storage

    def _is_valid_message(self, msg: str) -> bool:
        """检查消息是否有效，过滤掉 null 和占位符。真麻烦。"""
        if not msg or not isinstance(msg, str) or msg.strip().lower() == "null":
            return False
        # // 正则表达式，用来匹配 "text_数字" 这种无聊的占位符
        return not re.fullmatch(r"text_\d+", msg.strip())

    def _calculate_typing_delay(self, text: str) -> float:
        """
        计算模拟打字需要的时间。哼，就是个简单的数学题。
        """
        # 定义哪些标点需要停顿久一点，假装在思考
        punctuation_to_pause = "，。！？；、,."
        # 普通字/字母的打字延迟
        char_delay_min = 0.05
        char_delay_max = 0.15
        # 遇到标点符号的额外停顿
        punc_delay_min = 0.3
        punc_delay_max = 0.5
        # 封顶延迟，免得一句话等半天
        max_total_delay = 5.0

        total_delay = 0.0
        for char in text:
            if char in punctuation_to_pause:
                total_delay += random.uniform(punc_delay_min, punc_delay_max)
            else:
                total_delay += random.uniform(char_delay_min, char_delay_max)

        # 别睡太久了，懒鬼！
        final_delay = min(total_delay, max_total_delay)
        return final_delay

    async def execute_action(self, parsed_data: dict, uid_map: dict) -> bool:
        """根据LLM的决策执行回复或记录内部思考。"""
        # --- Sanitize optional fields ---
        fields_to_sanitize = ["at_someone", "quote_reply", "reply_text", "poke"]
        for field in fields_to_sanitize:
            if parsed_data.get(field) == "":
                parsed_data[field] = None
        # --- End sanitization ---

        # --- 改造点在这里！ ---
        reply_text_list = parsed_data.get("reply_text", [])
        # 先确保它是个列表，免得出错
        if not isinstance(reply_text_list, list):
            reply_text_list = []

        # 在这里就提前把有效消息算出来
        valid_sentences = [msg for msg in reply_text_list if self._is_valid_message(msg)]

        # 用 valid_sentences 来判断是否要互动
        has_interaction = parsed_data.get("reply_willing") and valid_sentences

        if has_interaction:
            # 获取要发送消息的数量
            num_messages_to_send = len(valid_sentences)

            # 我决定说话了，话痨计数器就加上我实际要说的条数，沉默计数器清零
            self.session.consecutive_bot_messages_count += num_messages_to_send
            self.session.no_action_count = 0
            logger.debug(
                f"[{self.session.conversation_id}] 机器人决定发言 {num_messages_to_send} 条，"
                f"consecutive_bot_messages_count 增加到 {self.session.consecutive_bot_messages_count}，"
                f"no_action_count 已重置。"
            )
            # 把已经算好的 valid_sentences 传给 _send_reply，省得它再算一遍
            return await self._send_reply(parsed_data, uid_map, valid_sentences)
        else:
            # 我决定不说话，沉默计数器+1，话痨计数器不清零
            self.session.no_action_count += 1
            logger.debug(
                f"[{self.session.conversation_id}] 机器人不发言，"
                f"no_action_count 增加到 {self.session.no_action_count}，"
                f"consecutive_bot_messages_count 保持在 {self.session.consecutive_bot_messages_count}。"
            )
            return await self._log_internal_thought(parsed_data)

    async def _send_reply(self, parsed_data: dict, uid_map: dict, valid_sentences: list[str]) -> bool:
        """发送回复消息。现在它直接接收已经过滤好的消息列表。"""
        # 不再需要自己计算 valid_sentences 了，直接用传进来的
        if not valid_sentences:
            logger.info(f"[{self.session.conversation_id}] _send_reply 收到空的有效消息列表，不发送。")
            return False

        at_target_values_raw = parsed_data.get("at_someone")
        quote_msg_id = parsed_data.get("quote_reply")
        current_motivation = parsed_data.get("motivation")

        bot_profile = await self.session.get_bot_profile()
        correct_bot_id = str(bot_profile.get("user_id", self.session.bot_id))

        action_recorded = False
        # 烦人的循环开始了
        for i, sentence_text in enumerate(valid_sentences):
            # 1. 计算这条消息的“模拟打字”时间
            typing_delay = self._calculate_typing_delay(sentence_text)
            logger.debug(
                f"[{self.session.conversation_id}] 模拟打字: '{sentence_text[:20]}...'，预计耗时 {typing_delay:.2f} 秒..."
            )

            # 2. 假装在打字，睡一会儿
            await asyncio.sleep(typing_delay)

            # 只有第一条消息才带 @ 和引用，后面的都是纯洁的肉体
            content_segs_payload = self._build_reply_segments(
                i, sentence_text, quote_msg_id, at_target_values_raw, uid_map
            )

            action_event_dict = {
                "event_id": f"sub_chat_reply_{uuid.uuid4()}",
                "event_type": "action.message.send",
                "time": time.time() * 1000,  # 加上时间戳
                "platform": self.session.platform,
                "bot_id": correct_bot_id,
                "user_info": UserInfo(
                    user_id=correct_bot_id, user_nickname=bot_profile.get("nickname")
                ).to_dict(),  # 把自己的信息也加上
                "conversation_info": {
                    "conversation_id": self.session.conversation_id,
                    "type": self.session.conversation_type,
                },
                "content": content_segs_payload,
                "motivation": current_motivation
                if i == 0 and current_motivation and current_motivation.strip()
                else None,
            }

            # 把原始的动作字典发出去
            success, msg = await self.action_handler.submit_constructed_action(action_event_dict, "发送专注模式回复")

            if success and "执行失败" not in msg:
                logger.info(f"Action to send reply segment {i + 1}/{len(valid_sentences)} submitted successfully.")
                self.session.message_count_since_last_summary += 1
                action_recorded = True
            else:
                logger.error(f"Failed to submit/execute action to send reply segment {i + 1}: {msg}")
                break

            # // 如果还有下一条，就睡一会儿，假装在打字，真麻烦
            if len(valid_sentences) > 1 and i < len(valid_sentences) - 1:
                await asyncio.sleep(random.uniform(0.5, 1.5))
        return action_recorded

    def _build_reply_segments(
        self, index: int, text: str, quote_id: str | None, at_raw: str | list | None, uid_map: dict
    ) -> list:
        """构建单条回复消息的 segments。"""
        payload = []
        if index == 0:
            if quote_id:
                payload.append(SegBuilder.reply(message_id=quote_id).to_dict())
            if at_raw:
                raw_targets = []
                if isinstance(at_raw, str):
                    raw_targets = [t.strip() for t in at_raw.split(",") if t.strip()]
                elif isinstance(at_raw, list):
                    raw_targets = [str(t).strip() for t in at_raw if str(t).strip()]
                else:
                    raw_targets = [str(at_raw).strip()]

                actual_ids = [uid_map.get(t, t) for t in raw_targets]
                for platform_id in actual_ids:
                    payload.append(SegBuilder.at(user_id=platform_id, display_name="").to_dict())
                if actual_ids:
                    payload.append(SegBuilder.text(" ").to_dict())
        payload.append(SegBuilder.text(text).to_dict())
        return payload

    async def _log_internal_thought(self, parsed_data: dict) -> bool:
        """记录内部思考（不回复）。"""
        motivation = parsed_data.get("motivation")
        if not motivation:
            return False

        logger.info(f"Decided not to reply. Motivation: {motivation}")

        bot_profile = await self.session.get_bot_profile()
        correct_bot_id = str(bot_profile.get("user_id", self.session.bot_id))
        correct_bot_nickname = bot_profile.get("nickname", config.persona.bot_name)

        internal_act_event_dict = {
            "event_id": f"internal_act_{uuid.uuid4()}",
            "event_type": "internal.focus_chat_mode.thought_log",
            "time": time.time() * 1000,
            "platform": self.session.platform,
            "bot_id": correct_bot_id,
            "user_info": UserInfo(user_id=correct_bot_id, user_nickname=correct_bot_nickname).to_dict(),
            "conversation_info": ConversationInfo(
                conversation_id=self.session.conversation_id,
                type=self.session.conversation_type,
                platform=self.session.platform,
            ).to_dict(),
            "content": [SegBuilder.text(motivation).to_dict()],
        }
        try:
            # 同样，把它变成DB文档，标记为已读，再存进去
            proto_event_obj = ProtocolEvent.from_dict(internal_act_event_dict)
            db_internal_doc = DBEventDocument.from_protocol(proto_event_obj)
            db_internal_doc.status = "read"
            await self.event_storage.save_event_document(db_internal_doc.to_dict())

            # 这里也不需要再加到内存列表里了
            self.session.message_count_since_last_summary += 1
            return False
        except Exception as e:
            logger.error(f"Failed to save internal ACT event: {e}", exc_info=True)
            return False
