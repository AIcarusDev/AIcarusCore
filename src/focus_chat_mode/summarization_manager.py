from typing import TYPE_CHECKING

from src.common.custom_logging.logger_manager import get_logger
from src.config import config

if TYPE_CHECKING:
    from .chat_session import ChatSession

logger = get_logger(__name__)


class SummarizationManager:
    """
    摘要管理员，负责管理所有和“总结”相关的事情。
    别想让我干别的，写会议纪要很累的！
    """

    def __init__(self, session: "ChatSession"):
        self.session = session
        self.event_storage = session.event_storage
        self.summarization_service = session.summarization_service
        self.summary_storage_service = session.summary_storage_service

    async def queue_events_for_summary(self, event_ids: list[str]) -> None:
        """获取事件详情以用于总结。"""
        if not event_ids:
            return
        try:
            event_docs = await self.event_storage.get_events_by_ids(event_ids)
            if event_docs:
                self.session.events_since_last_summary.extend(event_docs)
                self.session.message_count_since_last_summary += len(event_docs)
                logger.debug(f"[{self.session.conversation_id}] Added {len(event_docs)} processed events to summary queue.")
            else:
                logger.warning(f"[{self.session.conversation_id}] Could not fetch event documents for IDs: {event_ids}")
        except Exception as e:
            logger.error(f"[{self.session.conversation_id}] Error during queueing events for summary: {e}", exc_info=True)

    async def consolidate_summary_if_needed(self) -> None:
        """检查并执行摘要。"""
        if self.session.message_count_since_last_summary < self.session.SUMMARY_INTERVAL:
            return

        logger.info(f"[{self.session.conversation_id}] 已达到总结间隔，开始整合摘要...")
        try:
            bot_profile_for_summary = await self.session.get_bot_profile()
            if not bot_profile_for_summary:
                logger.warning(f"[{self.session.conversation_id}] 无法获取机器人档案，本次总结可能缺少相关信息。")
                bot_profile_for_summary = {}

            conversation_info_for_summary = {
                "name": self.session.conversation_name or "未知会话",
                "type": self.session.conversation_type,
                "id": self.session.conversation_id
            }

            user_map_for_summary = self._build_user_map(bot_profile_for_summary)

            if hasattr(self.summarization_service, "consolidate_summary"):
                new_summary = await self.summarization_service.consolidate_summary(
                    previous_summary=self.session.current_handover_summary,
                    recent_events=self.session.events_since_last_summary,
                    bot_profile=bot_profile_for_summary,
                    conversation_info=conversation_info_for_summary,
                    user_map=user_map_for_summary,
                )
                self.session.current_handover_summary = new_summary
                self.session.events_since_last_summary = []
                self.session.message_count_since_last_summary = 0
                logger.info(f"[{self.session.conversation_id}] 摘要已整合。新摘要(前50字符): {new_summary[:50]}...")
        except Exception as e:
            logger.error(f"[{self.session.conversation_id}] 整合摘要时发生错误: {e}", exc_info=True)

    def _build_user_map(self, bot_profile: dict) -> dict:
        user_map = {}
        uid_counter = 0
        
        bot_id = bot_profile.get('user_id', self.session.bot_id)
        user_map[bot_id] = {
            "uid_str": "U0",
            "nick": bot_profile.get('nickname', config.persona.bot_name),
            "card": bot_profile.get('card', config.persona.bot_name),
            "title": bot_profile.get('title', ""),
            "perm": bot_profile.get('role', "成员"),
        }

        for event in self.session.events_since_last_summary:
            user_info = event.get('user_info')
            if isinstance(user_info, dict):
                p_user_id = user_info.get('user_id')
                if p_user_id and p_user_id not in user_map:
                    uid_counter += 1
                    user_map[p_user_id] = {
                        "uid_str": f"U{uid_counter}",
                        "nick": user_info.get('user_nickname', f"用户{p_user_id}"),
                        "card": user_info.get('user_cardname', user_info.get('user_nickname', f"用户{p_user_id}")),
                        "title": user_info.get('user_titlename', ""),
                        "perm": user_info.get('permission_level', "成员"),
                    }
        return user_map

    async def save_final_summary(self) -> None:
        """保存当前会话的最终总结到数据库。"""
        final_summary = self.session.current_handover_summary
        if not final_summary or not final_summary.strip():
            logger.info(f"[{self.session.conversation_id}] 没有最终总结可保存，跳过。")
            return

        event_ids_covered = [
            event.get("event_id") for event in self.session.events_since_last_summary if event.get("event_id")
        ]

        logger.info(f"[{self.session.conversation_id}] 正在尝试保存最终的会话总结...")
        try:
            success = await self.summary_storage_service.save_summary(
                conversation_id=self.session.conversation_id,
                summary_text=final_summary,
                platform=self.session.platform,
                bot_id=self.session.bot_id,
                event_ids_covered=event_ids_covered,
            )
            if success:
                logger.info(f"[{self.session.conversation_id}] 成功保存最终总结。")
            else:
                logger.warning(f"[{self.session.conversation_id}] 保存最终总结失败（服务返回False）。")
        except Exception as e:
            logger.error(f"[{self.session.conversation_id}] 保存最终总结时发生意外错误: {e}", exc_info=True)
