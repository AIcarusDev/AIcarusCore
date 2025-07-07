# src/focus_chat_mode/action_executor.py
import asyncio
import json
import random
import time
import uuid
from typing import TYPE_CHECKING

from aicarus_protocols import ConversationInfo, Seg, SegBuilder, UserInfo
from aicarus_protocols import Event as ProtocolEvent
from src.common.custom_logging.logging_config import get_logger
from src.common.utils import is_valid_message
from src.config import config
from src.database import DBEventDocument

if TYPE_CHECKING:
    from .chat_session import ChatSession

logger = get_logger(__name__)


class ActionExecutor:
    """专门负责执行LLM决策的行动执行官。
    哼，别想让我干别的！
    """

    def __init__(self, session: "ChatSession") -> None:
        self.session = session
        self.action_handler = session.action_handler
        self.event_storage = session.event_storage

    def _calculate_typing_delay(self, text: str) -> float:
        """计算模拟打字需要的时间。哼，就是个简单的数学题."""
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

    async def execute_action(self, parsed_data: dict, uid_map: dict) -> tuple[bool, int, int]:
        """根据LLM的决策执行回复或记录内部思考。
        现在返回一个元组: (是否发生了互动, 实际发送数, 计划发送数)
        """
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
        valid_sentences = [msg for msg in reply_text_list if is_valid_message(msg)]

        self.session.messages_planned_this_turn = len(valid_sentences)

        # 用 valid_sentences 来判断是否要互动
        has_interaction = parsed_data.get("reply_willing") and valid_sentences

        if has_interaction:
            # 获取要发送消息的数量
            self.session.consecutive_bot_messages_count += len(valid_sentences)
            self.session.no_action_count = 0

            # 我决定说话了，话痨计数器就加上我实际要说的条数，沉默计数器清零
            logger.debug(
                f"[{self.session.conversation_id}] 机器人决定发言 {len(valid_sentences)} 条，"
                f"consecutive_bot_messages_count 增加到 {self.session.consecutive_bot_messages_count}，"
                f"no_action_count 已重置。"
            )
            # 把已经算好的 valid_sentences 传给 _send_reply，省得它再算一遍
            sent_count = await self._send_reply(parsed_data, uid_map, valid_sentences)
            return True, sent_count, len(valid_sentences)
        else:
            # 我决定不说话，沉默计数器+1，话痨计数器不清零
            self.session.no_action_count += 1
            logger.debug(
                f"[{self.session.conversation_id}] 机器人不发言，"
                f"no_action_count 增加到 {self.session.no_action_count}，"
                f"consecutive_bot_messages_count 保持在 {self.session.consecutive_bot_messages_count}。"
            )
            await self._log_internal_thought(parsed_data)
            return False, 0, 0

    async def _send_reply(
        self, parsed_data: dict, uid_map: dict, valid_sentences: list[str]
    ) -> int:
        """发送回复消息。现在它会返回实际发送的消息数量。
        并且在被取消时能优雅地处理。
        """
        # 不再需要自己计算 valid_sentences 了，直接用传进来的
        if not valid_sentences:
            logger.info(
                f"[{self.session.conversation_id}] _send_reply 收到空的有效消息列表，不发送。"
            )
            return 0

        at_target_values_raw = parsed_data.get("at_someone")
        quote_msg_id = parsed_data.get("quote_reply")
        current_motivation = parsed_data.get("motivation")

        bot_profile = await self.session.get_bot_profile()
        correct_bot_id = str(bot_profile.get("user_id", self.session.bot_id))

        sent_count = 0  # 这是我们的小计数器

        # 烦人的循环开始了
        try:
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

                success, result_payload = await self.action_handler.execute_simple_action(
                    platform_id=self.session.platform,
                    action_name="send_message",
                    params={
                        "conversation_id": self.session.conversation_id,
                        "conversation_type": self.session.conversation_type,
                        "content": content_segs_payload,
                    },
                    description="发送专注模式回复",
                )

                if (
                    success
                    and isinstance(result_payload, dict)
                    and result_payload.get("sent_message_id")
                ):
                    logger.info(f"发送回复成功，回执: {result_payload}")
                    sent_count += 1  # 发送成功，计数器+1
                    self.session.message_count_since_last_summary += 1

                    sent_message_id = str(result_payload["sent_message_id"])

                    # --- ❤❤❤ 看这里！这就是塞纸条的地方！❤❤❤ ---
                    extra_data_for_backpack = {}
                    motivation_for_log = (
                        current_motivation
                        if i == 0 and current_motivation and current_motivation.strip()
                        else None
                    )
                    if motivation_for_log:
                        extra_data_for_backpack["motivation"] = motivation_for_log

                    # 把小背包（字典）变成一个字符串，这样才能塞进 raw_data
                    raw_data_string = (
                        json.dumps(extra_data_for_backpack) if extra_data_for_backpack else None
                    )

                    final_content_dicts = [
                        SegBuilder.message_metadata(message_id=sent_message_id).to_dict(),
                        *content_segs_payload,
                    ]
                    final_content_segs = [Seg.from_dict(d) for d in final_content_dicts]

                    my_message_event = ProtocolEvent(
                        event_id=f"self_msg_{sent_message_id}",
                        event_type=f"message.{self.session.platform}.{self.session.conversation_type}",
                        time=int(time.time() * 1000),
                        bot_id=correct_bot_id,
                        content=final_content_segs,
                        user_info=UserInfo(
                            user_id=correct_bot_id, user_nickname=bot_profile.get("nickname")
                        ),
                        conversation_info=ConversationInfo(
                            conversation_id=self.session.conversation_id,
                            type=self.session.conversation_type,
                        ),
                        raw_data=raw_data_string,  # <-- 看！把带小纸条的背包塞进去了！
                    )

                    db_doc_to_save = DBEventDocument.from_protocol(my_message_event)
                    db_doc_to_save.status = "read"

                    await self.event_storage.save_event_document(db_doc_to_save.to_dict())

                else:
                    logger.error(f"发送回复失败或未收到有效回执: {result_payload}")
                    break

                # // 如果还有下一条，就睡一会儿，假装在打字，真麻烦
                if len(valid_sentences) > 1 and i < len(valid_sentences) - 1:
                    await asyncio.sleep(random.uniform(0.5, 1.5))
            # 结束循环，返回实际发送的消息数量
            return sent_count

        except asyncio.CancelledError:
            logger.info(
                f"[{self.session.conversation_id}] 消息发送任务被取消。已发送 {sent_count}/{len(valid_sentences)} 条。"
            )
            # 在被取消时，也返回已经发送的数量
            return sent_count
        finally:
            # ❤❤❤ 无论如何，都要留下遗言！❤❤❤
            self.session.messages_sent_this_turn = sent_count
            logger.debug(
                f"[{self.session.conversation_id}] ActionExecutor 报告：本轮实际发送 {sent_count} 条消息。"
            )

    def _build_reply_segments(
        self, index: int, text: str, quote_id: str | None, at_raw: str | list | None, uid_map: dict
    ) -> list:
        """构建单条回复消息的 segments."""
        payload = []
        if index == 0:
            if quote_id:
                payload.append(Seg(type="quote", data={"message_id": str(quote_id)}).to_dict())
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
        """记录内部思考（不回复）."""
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
            "user_info": UserInfo(
                user_id=correct_bot_id, user_nickname=correct_bot_nickname
            ).to_dict(),
            "conversation_info": ConversationInfo(
                conversation_id=self.session.conversation_id,
                type=self.session.conversation_type,
                # platform=self.session.platform,
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
