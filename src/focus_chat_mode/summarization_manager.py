# 文件路径: src/focus_chat_mode/summarization_manager.py
# 哼，又帮你打扫了一遍，这下总该干净了吧！

from typing import TYPE_CHECKING, Any

from src.common.custom_logging.logging_config import get_logger
from src.config import config

if TYPE_CHECKING:
    from .chat_session import ChatSession

logger = get_logger(__name__)


class SummarizationManager:
    """
    摘要管理员（最终清理版）。
    哼，现在我的职责很明确，就是决定什么时候该做总结，然后喊别人来干活。
    """

    def __init__(self, session: "ChatSession") -> None:
        self.session = session
        self.event_storage = session.event_storage
        self.summarization_service = session.summarization_service
        self.summary_storage_service = session.summary_storage_service
        self.summary_threshold = config.focus_chat_mode.summary_interval

    async def _handle_summary_process(self, final_save: bool) -> None:
        """
        一个私有方法，把重复的脏活都干了。
        final_save 这个开关决定了最后是只更新内存，还是存到数据库。
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
                return

            log_prefix = f"[{self.session.conversation_id}]"
            summary_type = "最终" if final_save else "阶段性"
            logger.info(
                f"{log_prefix} 已达到{summary_type}总结阈值({len(events_to_summarize)}/{self.summary_threshold if not final_save else 'N/A'})，开始整合摘要..."
            )

            # 3. 准备调用总结服务所需的东西
            bot_profile = await self.session.get_bot_profile()
            conversation_info = {
                "id": self.session.conversation_id,
                "name": self.session.conversation_name or "未知会话",
                "type": self.session.conversation_type,
                "platform": self.session.platform,
            }

            # --- 【核心修正点！】 ---
            # 调用 SummarizationService 时，不再需要自己构建和传递 user_map 了！
            new_summary = await self.summarization_service.consolidate_summary(
                previous_summary=self.session.current_handover_summary,
                recent_events=events_to_summarize,
                bot_profile=bot_profile,
                conversation_info=conversation_info,
                event_storage=self.event_storage, # 把 event_storage 传进去
            )

            if not new_summary:
                logger.warning(f"{log_prefix} LLM未能生成有效的{summary_type}摘要。")
                return

            # 4. 根据模式决定怎么处理总结结果
            event_ids_covered = [event.get("_key") for event in events_to_summarize if event.get("_key")]

            if final_save:
                await self._save_summary_to_db(new_summary, event_ids_covered)
                self.session.current_handover_summary = new_summary
                logger.info(f"{log_prefix} 最终摘要已成功保存到数据库。")
            else:
                self.session.current_handover_summary = new_summary
                logger.info(f"{log_prefix} 内存中的摘要已通过阶段性总结更新。")

            # 5. 把用过的事件标记为 "summarized"
            await self.event_storage.update_events_status_to_summarized(event_ids_covered)

        except Exception as e:
            logger.error(f"[{self.session.conversation_id}] 执行{summary_type}总结时发生错误: {e}", exc_info=True)

    async def consolidate_summary_if_needed(self) -> None:
        """【日常模式】检查并执行阶段性总结。"""
        await self._handle_summary_process(final_save=False)

    async def create_and_save_final_summary(self) -> None:
        """【收尾模式】执行最终总结。"""
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