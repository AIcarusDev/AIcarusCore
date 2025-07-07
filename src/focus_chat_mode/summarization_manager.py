# 文件路径: src/focus_chat_mode/summarization_manager.py
from typing import TYPE_CHECKING

from src.common.custom_logging.logging_config import get_logger
from src.config import config

if TYPE_CHECKING:
    from .chat_session import ChatSession

logger = get_logger(__name__)


class SummarizationManager:
    """摘要管理器，用于处理会话的阶段性和最终总结.

    这个管理器负责在会话中积累事件，并在达到一定条件时生成阶段性或最终摘要。
    它会根据配置的摘要间隔和事件数量来决定何时生成摘要，并将结果保存到数据库或内存中。
    这个类的设计目的是为了简化摘要生成的逻辑，避免在会话中直接处理复杂的摘要逻辑。
    它提供了两个主要方法：`consolidate_summary_if_needed` 用于执行阶段性总结，
    `create_and_save_final_summary` 用于执行最终总结。

    这个类的实例通常在会话开始时创建，并在会话结束时调用
    `create_and_save_final_summary` 方法来保存最终摘要。
    它依赖于会话的事件存储服务、摘要服务和摘要存储服务来完成其工作。
    通过这种方式，摘要管理器可以独立于会话逻辑进行工作，
    使得代码更加模块化和易于维护。

    Attributes:
        session: ChatSession - 当前会话实例，提供事件存储、摘要服务和摘要存储服务的访问。
        event_storage: EventStorageService - 事件存储服务，用于获取可总结的事件。
        summarization_service: SummarizationService - 摘要服务，用于生成摘要。
        summary_storage_service: SummaryStorageService - 摘要存储服务，用于保存摘要到数据库。
        summary_threshold: int - 阶段性总结的事件数量阈值，超过此数量将触发阶段性总结。
    """

    def __init__(self, session: "ChatSession") -> None:
        self.session = session
        self.event_storage = session.event_storage
        self.summarization_service = session.summarization_service
        self.summary_storage_service = session.summary_storage_service
        self.summary_threshold = config.focus_chat_mode.summary_interval

    async def _handle_summary_process(
        self,
        final_save: bool,
        shift_motivation: str | None = None,
        target_conversation_id: str | None = None,
    ) -> None:
        """处理摘要生成的核心逻辑.

        这个方法会根据是否是最终保存来决定摘要的处理方式。
        如果是阶段性总结，将生成内存中的摘要并更新会话状态。
        如果是最终保存，将生成摘要并保存到数据库中。

        Args:
            final_save (bool): 是否是最终保存。如果是，则会将摘要保存到数据库中。
            shift_motivation (str | None): 跳槽动机，如果有的话，将被用来生成摘要。
            target_conversation_id (str | None): 目标会话ID，如果有的话，将被用来生成摘要。
        这两个参数在最终保存时可能会被用来提供额外的上下文信息，
        例如跳槽的原因或目标会话的ID。
        """
        try:
            # 1. 直接捞货，不数了，懒得数。
            events_to_summarize = await self.event_storage.get_summarizable_events(
                self.session.conversation_id
            )

            # 2. 检查数量，不够就不干了。
            # 如果是最终保存，即使没有新事件，只要有内存里的摘要，也可能需要保存（比如在转移时）
            if not events_to_summarize and not (
                final_save and self.session.current_handover_summary
            ):
                return

            if not final_save and len(events_to_summarize) < self.summary_threshold:
                return

            log_prefix = f"[{self.session.conversation_id}]"
            summary_type = "最终" if final_save else "阶段性"
            logger.info(
                f"{log_prefix} 已达到{summary_type}总结阈值({len(events_to_summarize)}/",
                f"{'N/A' if final_save else self.summary_threshold})，开始整合摘要...",
            )

            # 3. 准备调用总结服务所需的东西
            bot_profile = await self.session.get_bot_profile()
            conversation_info = {
                "id": self.session.conversation_id,
                "name": self.session.conversation_name or "未知会话",
                "type": self.session.conversation_type,
                "platform": self.session.platform,
            }

            # --- 【核心改造点！】 ---
            # 把“跳槽动机”也塞进摘要服务的Prompt里
            new_summary = await self.summarization_service.consolidate_summary(
                previous_summary=self.session.current_handover_summary,
                recent_events=events_to_summarize,
                bot_profile=bot_profile,
                conversation_info=conversation_info,
                event_storage=self.event_storage,
                shift_motivation=shift_motivation,  # 看！新玩具！
                target_conversation_id=target_conversation_id,  # 还有这个！
            )

            if not new_summary:
                logger.warning(f"{log_prefix} LLM未能生成有效的{summary_type}摘要。")
                return

            # 4. 根据模式决定怎么处理总结结果
            event_ids_covered = [
                event.get("_key") for event in events_to_summarize if event.get("_key")
            ]

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
            logger.error(
                f"[{self.session.conversation_id}] 执行{summary_type}总结时发生错误: {e}",
                exc_info=True,
            )

    async def consolidate_summary_if_needed(self) -> None:
        """检查并执行阶段性总结.

        这个方法会在会话中定期调用，检查是否需要生成阶段性摘要。
        如果当前会话的事件数量超过配置的阈值，将调用摘要服务生成摘要，并更新会话的内存摘要。
        如果没有达到阈值，则不会生成摘要。
        """
        await self._handle_summary_process(final_save=False)

    async def create_and_save_final_summary(
        self, shift_motivation: str | None = None, target_conversation_id: str | None = None
    ) -> None:
        """创建并保存最终摘要.

        这个方法会在会话结束时调用，生成最终的摘要并保存到数据库。

        Args:
            shift_motivation (str | None): 如果有跳槽动机，这个参数将被用来生成摘要。
            target_conversation_id (str | None): 如果有目标会话ID，这个参数将被用来生成摘要。
        """
        await self._handle_summary_process(
            final_save=True,
            shift_motivation=shift_motivation,
            target_conversation_id=target_conversation_id,
        )

    async def _save_summary_to_db(self, summary_text: str, event_ids: list[str]) -> None:
        """内部辅助方法，保存摘要到数据库.

        Args:
            summary_text (str): 要保存的摘要文本。
            event_ids (list[str]): 涉及的事件ID列表，用于记录哪些事件被包含在摘要中。
        """
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
            logger.error(
                f"[{self.session.conversation_id}] 内部保存摘要到数据库时失败: {e}", exc_info=True
            )
