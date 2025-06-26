# D:\Aic\AIcarusCore\src\focus_chat_mode\summarization_manager.py

from typing import TYPE_CHECKING, Any

from src.common.custom_logging.logging_config import get_logger
from src.common.summarization_observation.summarization_service import SummarizationService
from src.config import config

if TYPE_CHECKING:
    from .chat_session import ChatSession

logger = get_logger(__name__)


class SummarizationManager:
    """
    摘要管理员（混合模式版）。
    哼，被小懒猫重构过了，现在没那么啰嗦了。
    """

    def __init__(self, session: "ChatSession") -> None:
        self.session = session
        self.event_storage = session.event_storage
        self.summarization_service: SummarizationService = session.summarization_service
        self.summary_storage_service = session.summary_storage_service
        self.summary_threshold = config.focus_chat_mode.summary_interval

    async def _handle_summary_process(self, final_save: bool) -> None:
        """
        // 哼，一个私有方法，把重复的脏活都干了。
        // final_save 这个开关决定了最后是只更新内存，还是存到数据库。
        """
        try:
            # 1. 直接捞货，不数了，懒得数。
            events_to_summarize = await self.event_storage.get_summarizable_events(self.session.conversation_id)

            # 2. 检查数量，不够就不干了。
            if not events_to_summarize:
                if final_save and self.session.current_handover_summary:
                    logger.info(f"[{self.session.conversation_id}] 没有新事件需要最终总结，但将保存内存中的现有摘要。")
                    await self._save_summary_to_db(self.session.current_handover_summary, [])
                return

            if not final_save and len(events_to_summarize) < self.summary_threshold:
                # 日常摸鱼，数量不够，不总结。
                return

            log_prefix = f"[{self.session.conversation_id}]"
            summary_type = "最终" if final_save else "阶段性"
            logger.info(
                f"{log_prefix} 已达到{summary_type}总结阈值({len(events_to_summarize)}/{self.summary_threshold if not final_save else 'N/A'})，开始整合摘要..."
            )

            # 3. 构造用户信息和调用LLM，这些都是体力活。
            bot_profile = await self.session.get_bot_profile()
            conversation_info = {
                "name": self.session.conversation_name or "未知会话",
                "type": self.session.conversation_type,
                "id": self.session.conversation_id,
            }
            user_map = self._build_user_map(bot_profile, events_to_summarize)

            new_summary = await self.summarization_service.consolidate_summary(
                previous_summary=self.session.current_handover_summary,
                recent_events=events_to_summarize,
                bot_profile=bot_profile,
                conversation_info=conversation_info,
                user_map=user_map,
            )

            if not new_summary:
                logger.warning(f"{log_prefix} LLM未能生成有效的{summary_type}摘要。")
                return

            # 4. 根据模式决定怎么处理总结结果
            event_ids_covered = [event.get("_key") for event in events_to_summarize if event.get("_key")]

            if final_save:
                # 最终模式：存数据库，更新内存
                await self._save_summary_to_db(new_summary, event_ids_covered)
                self.session.current_handover_summary = new_summary
                logger.info(f"{log_prefix} 最终摘要已成功保存到数据库。")
            else:
                # 日常模式：只更新内存
                self.session.current_handover_summary = new_summary
                logger.info(f"{log_prefix} 内存中的摘要已通过阶段性总结更新。")

            # 5. 把用过的事件标记为 "summarized"，省得下次还来烦我。
            await self.event_storage.update_events_status_to_summarized(event_ids_covered)

        except Exception as e:
            logger.error(f"[{self.session.conversation_id}] 执行{summary_type}总结时发生错误: {e}", exc_info=True)

    async def consolidate_summary_if_needed(self) -> None:
        """
        【日常模式】检查并执行阶段性总结。
        只更新内存中的摘要。
        """
        await self._handle_summary_process(final_save=False)

    async def create_and_save_final_summary(self) -> None:
        """
        【收尾模式】执行最终总结。
        处理所有剩余的'read'消息，并将最终结果存入数据库。
        """
        await self._handle_summary_process(final_save=True)

    async def _save_summary_to_db(self, summary_text: str, event_ids: list[str]) -> None:
        """内部辅助方法，保存摘要到数据库。"""
        if not summary_text or not summary_text.strip():
            return
        try:
            await self.summary_storage_service.save_summary(
                conversation_id=self.session.conversation_id,
                summary_text=summary_text,
                platform=self.session.platform,
                bot_id=self.session.bot_id,
                event_ids_covered=event_ids,
            )
        except Exception as e:
            logger.error(f"[{self.session.conversation_id}] 内部保存摘要到数据库时失败: {e}", exc_info=True)

    def _build_user_map(self, bot_profile: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
        """构建用户映射表，和原来一样。"""
        user_map: dict[str, dict[str, Any]] = {}
        uid_counter = 0

        bot_id = bot_profile.get("user_id", self.session.bot_id)
        user_map[bot_id] = {
            "uid_str": "U0",
            "nick": bot_profile.get("nickname", config.persona.bot_name),
            "card": bot_profile.get("card", config.persona.bot_name),
            "title": bot_profile.get("title", ""),
            "perm": bot_profile.get("role", "成员"),
        }

        for event in events:
            user_info = event.get("user_info")
            if isinstance(user_info, dict):
                p_user_id = user_info.get("user_id")
                if p_user_id and p_user_id not in user_map:
                    uid_counter += 1
                    user_map[p_user_id] = {
                        "uid_str": f"U{uid_counter}",
                        "nick": user_info.get("user_nickname", f"用户{p_user_id}"),
                        "card": user_info.get("user_cardname", user_info.get("user_nickname", f"用户{p_user_id}")),
                        "title": user_info.get("user_titlename", ""),
                        "perm": user_info.get("permission_level", "成员"),
                    }
        return user_map
