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
        # // 把 action_to_take 相关的都删掉，眼不见心不烦
        fields_to_sanitize = ["at_someone", "quote_reply", "reply_text", "poke"]
        for field in fields_to_sanitize:
            if parsed_data.get(field) == "":
                parsed_data[field] = None
        # --- End sanitization ---

        # // 现在检查 reply_text 是否是一个有效的列表
        reply_text_list = parsed_data.get("reply_text")
        has_interaction = (
            parsed_data.get("reply_willing")
            and isinstance(reply_text_list, list)
            and any(self._is_valid_message(msg) for msg in reply_text_list)
        )

        if has_interaction:
            self.session.no_action_count = 0
            logger.debug(f"[{self.session.conversation_id}] 检测到互动行为，no_action_count 已重置。")
            # 把整个解析好的数据都传过去，让它自己处理
            return await self._send_reply(parsed_data, uid_map)
        else:
            self.session.no_action_count += 1
            logger.debug(
                f"[{self.session.conversation_id}] 无互动行为，no_action_count 增加到 {self.session.no_action_count}。"
            )
            return await self._log_internal_thought(parsed_data)

    async def _send_reply(self, parsed_data: dict, uid_map: dict) -> bool:
        """发送回复消息。现在它会处理一个消息数组了。"""
        # // 从这里开始，逻辑全变了！
        original_reply_list = parsed_data.get("reply_text", [])

        # // 用我写好的那个烦人的检查函数，把无效的消息都踢出去
        valid_sentences = [msg for msg in original_reply_list if self._is_valid_message(msg)]

        if not valid_sentences:
            logger.info(f"[{self.session.conversation_id}] LLM 提供了 reply_text，但过滤后没有有效消息可发送。")
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
            logger.debug(f"[{self.session.conversation_id}] 模拟打字: '{sentence_text[:20]}...'，预计耗时 {typing_delay:.2f} 秒...")

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
