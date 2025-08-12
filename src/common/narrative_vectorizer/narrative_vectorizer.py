# src/common/narrative_vectorizer/narrative_vectorizer.py
import asyncio
from typing import TYPE_CHECKING, Any

from aicarus_protocols import Event as ProtocolEvent
from src.common.custom_logging.logging_config import get_logger
from src.common.intelligent_interrupt_system.models import SemanticModel

if TYPE_CHECKING:
    from src.database.services.entity_graph_service import EntityGraphService
    from src.message_processing.image_analysis_service import ImageAnalysisService

logger = get_logger(__name__)


class NarrativeVectorizer:
    """超级句子生成器.

    一个专门的服务, 负责将一个原始的、多模态的事件,
    转化为一个高信息密度的“超级句子”（叙事化文本）及其对应的语义向量.
    这是 PYTHIA 情感系统信息处理的第一步.
    """

    def __init__(
        self,
        entity_service: "EntityGraphService",
        image_analysis_service: "ImageAnalysisService",
        semantic_model: SemanticModel,
    ) -> None:
        self.entity_service = entity_service
        self.image_analysis_service = image_analysis_service
        self.semantic_model = semantic_model
        logger.info("NarrativeVectorizer 初始化完成。")

    async def build_and_vectorize(
        self, event: ProtocolEvent
    ) -> tuple[str | None, list[float] | None]:
        """核心方法：构建“超级句子”并进行向量化.

        Args:
            event: 原始的 aicarus_protocols.Event 对象.

        Returns:
            一个元组 (narrative_sentence, event_vector)。如果无法构建，则返回 (None, None).
        """
        try:
            descriptor = await self._build_semantic_descriptor(event)
            if not descriptor:
                return None, None

            narrative_sentence = self._format_narrative_sentence(descriptor)

            event_vector_list = self.semantic_model.encode([narrative_sentence])
            event_vector = event_vector_list[0].tolist() if len(event_vector_list) > 0 else None

            logger.debug(f"事件 {event.event_id} 已成功向量化。")
            return narrative_sentence, event_vector

        except Exception as e:
            logger.error(f"为事件 {event.event_id} 构建叙事向量时失败: {e}", exc_info=True)
            return None, None

    async def _build_semantic_descriptor(self, event: ProtocolEvent) -> dict[str, Any] | None:
        """内部辅助方法，用于组装包含完整情境的 Semantic Descriptor JSON."""
        if not event.user_info or not event.user_info.user_id:
            return None

        sender_id = event.user_info.user_id
        # TODO: 从 EntityGraphService 获取更丰富的主观关系，目前先用角色占位
        sender_profile = {
            "id": sender_id,
            "role": event.user_info.permission_level or "成员",
            "relationship": "对话参与者",  # 占位
        }

        context = {}
        if event.conversation_info:
            context["platform"] = event.get_platform()
            context["conversation_name"] = event.conversation_info.name or "未知会话"

        action = {"type": "send_message", "text_content": event.get_text_content()}

        modality_content = None
        image_segs = [
            seg for seg in event.content if seg.type == "image" and seg.data.get("base64")
        ]
        if image_segs:
            analysis_tasks = []
            for seg in image_segs:
                seg_data = seg.data
                base64_data = seg_data.get("base64")
                # 使用 ImageAnalysisService 内部的方法来计算哈希，确保一致性
                image_hash = self.image_analysis_service._calculate_image_hash(base64_data)
                # 调用新的、带等待机制的方法
                analysis_tasks.append(
                    self.image_analysis_service.get_analysis_result(
                        image_hash, base64_data, seg_data
                    )
                )

            # 并发执行所有图片的分析
            analysis_results = await asyncio.gather(*analysis_tasks)

            image_descriptions = [
                res.get("details", {}).get("description", "一张图片")
                for res in analysis_results
                if res
            ]

            if image_descriptions:
                modality_content = {
                    "type": "image",
                    "description": "；".join(image_descriptions),
                }

        return {
            "sender": sender_profile,
            "context": context,
            "action": action,
            "modality_content": modality_content,
        }

    def _format_narrative_sentence(self, descriptor: dict[str, Any]) -> str:
        """使用模板将 Semantic Descriptor JSON 转化为连贯的“超级句子”."""
        sender = descriptor.get("sender", {})
        context = descriptor.get("context", {})
        action = descriptor.get("action", {})
        modality = descriptor.get("modality_content")

        parts = [
            f"{sender.get('role', '')}",
            f"{sender.get('relationship', '')}",
            f"'{sender.get('id', '')}'",
            f"在'{context.get('conversation_name', '')}'的场景下",
        ]

        if modality:
            parts.append(f"发送了{modality.get('type', '媒体')}")
            if desc := modality.get("description"):
                parts.append(f"内容为“{desc}”")

        if text := action.get("text_content"):
            parts.append(f"并附言：“{text}”")
        else:
            if not modality:
                parts.append("进行了一次无内容的互动")

        return "，".join(filter(None, parts)) + "。"
