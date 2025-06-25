import asyncio
import random
import time
import uuid
from typing import TYPE_CHECKING

from aicarus_protocols.conversation_info import ConversationInfo
from aicarus_protocols.seg import SegBuilder
from aicarus_protocols.user_info import UserInfo

from src.common.custom_logging.logger_manager import get_logger
from src.common.text_splitter import process_llm_response
from src.config import config

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

    async def execute_action(self, parsed_data: dict, uid_map: dict) -> bool:
        """根据LLM的决策执行回复或记录内部思考。"""
        # --- Sanitize optional fields ---
        fields_to_sanitize = ["at_someone", "quote_reply", "reply_text", "poke", "action_to_take", "action_motivation"]
        for field in fields_to_sanitize:
            if parsed_data.get(field) == "":
                parsed_data[field] = None
        if parsed_data.get("action_to_take") is None:
            parsed_data["action_motivation"] = None
        # --- End sanitization ---

        has_interaction = parsed_data.get("reply_willing") and parsed_data.get("reply_text")

        if has_interaction:
            self.session.no_action_count = 0
            logger.debug(f"[{self.session.conversation_id}] 检测到互动行为，no_action_count 已重置。")
            return await self._send_reply(parsed_data, uid_map)
        else:
            self.session.no_action_count += 1
            logger.debug(
                f"[{self.session.conversation_id}] 无互动行为，no_action_count 增加到 {self.session.no_action_count}。"
            )
            return await self._log_internal_thought(parsed_data)

    async def _send_reply(self, parsed_data: dict, uid_map: dict) -> bool:
        """发送回复消息。"""
        original_reply_text = parsed_data["reply_text"]
        split_sentences = process_llm_response(
            text=original_reply_text,
            enable_kaomoji_protection=config.focus_chat_mode.enable_kaomoji_protection,
            enable_splitter=config.focus_chat_mode.enable_splitter,
            max_length=config.focus_chat_mode.max_length,
            max_sentence_num=config.focus_chat_mode.max_sentence_num,
        )

        at_target_values_raw = parsed_data.get("at_someone")
        quote_msg_id = parsed_data.get("quote_reply")
        current_motivation = parsed_data.get("motivation")

        action_recorded = False
        for i, sentence_text in enumerate(split_sentences):
            content_segs_payload = self._build_reply_segments(
                i, sentence_text, quote_msg_id, at_target_values_raw, uid_map
            )
            action_event_dict = {
                "event_id": f"sub_chat_reply_{uuid.uuid4()}",
                "event_type": "action.message.send",
                "platform": self.session.platform,
                "bot_id": self.session.bot_id,
                "conversation_info": {
                    "conversation_id": self.session.conversation_id,
                    "type": self.session.conversation_type,
                },
                "content": content_segs_payload,
                "motivation": current_motivation
                if i == 0 and current_motivation and current_motivation.strip()
                else None,
            }
            success, msg = await self.action_handler.submit_constructed_action(action_event_dict, "发送子意识聊天回复")
            if success and "执行失败" not in msg:
                logger.info(f"Action to send reply segment {i + 1} submitted successfully.")
                self.session.events_since_last_summary.append(action_event_dict)
                self.session.message_count_since_last_summary += 1
                action_recorded = True
            else:
                logger.error(f"Failed to submit/execute action to send reply segment {i + 1}: {msg}")
                break
            if len(split_sentences) > 1 and i < len(split_sentences) - 1:
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
        internal_act_event_dict = {
            "event_id": f"internal_act_{uuid.uuid4()}",
            "event_type": "internal.focus_chat_mode.thought_log",
            "time": time.time() * 1000,
            "platform": self.session.platform,
            "bot_id": self.session.bot_id,
            "user_info": UserInfo(user_id=self.session.bot_id, user_nickname=config.persona.bot_name).to_dict(),
            "conversation_info": ConversationInfo(
                conversation_id=self.session.conversation_id,
                type=self.session.conversation_type,
                platform=self.session.platform,
            ).to_dict(),
            "content": [SegBuilder.text(motivation).to_dict()],
        }
        try:
            await self.event_storage.save_event_document(internal_act_event_dict)
            self.session.events_since_last_summary.append(internal_act_event_dict)
            self.session.message_count_since_last_summary += 1
            return True
        except Exception as e:
            logger.error(f"Failed to save internal ACT event: {e}", exc_info=True)
            return False
