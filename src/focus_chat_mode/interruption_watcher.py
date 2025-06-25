import asyncio
from typing import TYPE_CHECKING

from src.common.custom_logging.logger_manager import get_logger

if TYPE_CHECKING:
    from .chat_session import ChatSession

logger = get_logger(__name__)


class InterruptionWatcher:
    """
    一个轻量级的观察员，在思考期间监视新消息并决定是否中断。
    哼，我就是那个躲在暗处盯着你们聊天的小猫咪！
    """

    def __init__(self, session: "ChatSession", interruption_event: asyncio.Event) -> None:
        self.session = session
        self.event_storage = session.event_storage
        self._interruption_event = interruption_event
        self._shutting_down = False

    async def run(self, observe_start_timestamp: float) -> None:
        """
        启动观察员，在思考期间监视新消息。
        """
        interruption_score = 0
        threshold = 100
        processed_event_keys_in_this_run = set()
        last_checked_timestamp = observe_start_timestamp
        logger.debug(f"[{self.session.conversation_id}] 中断观察员已启动，观察起点: {last_checked_timestamp}")

        bot_profile = await self.session.get_bot_profile()
        current_bot_id = str(bot_profile.get("user_id") or self.session.bot_id)

        while not self._shutting_down and not self._interruption_event.is_set():
            try:
                new_events = await self.event_storage.get_message_events_after_timestamp(
                    self.session.conversation_id, last_checked_timestamp, limit=10
                )

                if new_events:
                    for event_doc in new_events:
                        event_key = event_doc.get("_key")
                        if not event_key or event_key in processed_event_keys_in_this_run:
                            continue

                        sender_info = event_doc.get("user_info", {})
                        sender_id = sender_info.get("user_id") if isinstance(sender_info, dict) else None

                        if sender_id and str(sender_id) == current_bot_id:
                            logger.debug(
                                f"[{self.session.conversation_id}] 观察员发现一条自己发的消息({event_key})，已忽略。"
                            )
                            processed_event_keys_in_this_run.add(event_key)
                            continue

                        processed_event_keys_in_this_run.add(event_key)

                        score_to_add = self._calculate_score(event_doc, current_bot_id)
                        interruption_score += score_to_add
                        logger.debug(
                            f"[{self.session.conversation_id}] 新消息({event_key})计分后，中断分数: {interruption_score}"
                        )

                        last_checked_timestamp = new_events[-1]["timestamp"]

                        if interruption_score >= threshold:
                            logger.info(
                                f"[{self.session.conversation_id}] 中断分数达到阈值 ({interruption_score}/{threshold})！发送中断信号！"
                            )
                            self._interruption_event.set()
                            return

                await asyncio.sleep(0.5)

            except Exception as e:
                logger.error(f"[{self.session.conversation_id}] 中断观察员出错: {e}", exc_info=True)
                await asyncio.sleep(2)

        logger.debug(f"[{self.session.conversation_id}] 中断观察员正常退出。")

    def shutdown(self) -> None:
        self._shutting_down = True

    async def _calculate_score(self, event_doc: dict, current_bot_id: str) -> int:
        if self._is_mentioning_me(event_doc, current_bot_id):
            return 100
        if await self._is_quoting_me(event_doc, current_bot_id):
            return 80

        content = event_doc.get("content", [])
        if content and isinstance(content, list) and len(content) > 0:
            main_seg_type = content[0].get("type")
            if main_seg_type in ["image", "video", "forward", "share"]:
                return 30
            elif main_seg_type == "text":
                text_content = "".join([s.get("data", {}).get("text", "") for s in content if s.get("type") == "text"])
                char_count = len(text_content.replace(" ", "").replace("\n", ""))
                if char_count >= 25:
                    return 35
                elif char_count >= 5:
                    return 20
                else:
                    return 5
            elif main_seg_type == "record":
                return 15
            elif main_seg_type in ["face", "poke"]:
                return 5
            else:
                return 10
        return 0

    def _is_mentioning_me(self, event_doc: dict, current_bot_id: str) -> bool:
        if not current_bot_id:
            return False
        for seg in event_doc.get("content", []):
            if seg.get("type") == "at":
                at_user_id_raw = seg.get("data", {}).get("user_id")
                if at_user_id_raw is not None and str(at_user_id_raw) == current_bot_id:
                    logger.debug(f"[{self.session.conversation_id}] 确认被@，当前机器人ID: {current_bot_id}")
                    return True
        return False

    async def _is_quoting_me(self, event_doc: dict, current_bot_id: str) -> bool:
        quoted_message_id = None
        for seg in event_doc.get("content", []):
            if seg.get("type") in ["quote", "reply"]:
                quoted_message_id = seg.get("data", {}).get("message_id")
                break

        if not quoted_message_id or not current_bot_id:
            return False

        try:
            original_message_docs = await self.event_storage.get_events_by_ids([str(quoted_message_id)])
            if not original_message_docs:
                logger.warning(f"[{self.session.conversation_id}] 找不到被引用的消息, ID: {quoted_message_id}")
                return False

            original_message_doc = original_message_docs[0]
            original_sender_info = original_message_doc.get("user_info", {})
            original_sender_id = original_sender_info.get("user_id") if isinstance(original_sender_info, dict) else None

            if original_sender_id and str(original_sender_id) == current_bot_id:
                logger.debug(
                    f"[{self.session.conversation_id}] 确认被回复，被引用的消息 {quoted_message_id} 是由我 ({current_bot_id}) 发送的。"
                )
                return True
        except Exception as e:
            logger.error(f"[{self.session.conversation_id}] 在检查是否被回复时发生数据库查询错误: {e}", exc_info=True)

        return False
