# src/common/intelligent_interrupt_system/intelligent_interrupter.py

import numpy as np
from sklearn.metrics.pairwise import cosine_similarity
from src.common.custom_logging.logging_config import get_logger

from .models import SemanticMarkovModel

logger = get_logger(__name__)


class IntelligentInterrupter:
    """这是一个无状态的智能中断器.

    它不保存任何上下文信息，每次判断都需要提供当前消息和上下文消息.
    它会根据发言者的权重、目标关键词和核心重要概念来判断是否需要中断.

    Attributes:
        speaker_weights (dict[str, float]): 发言者的权重字典，键是发言者ID，值是权重因子.
        objective_keywords (list[str]): 目标关键词列表，用于识别需要中断的消息.
        core_importance_concepts (list[str]): 核心重要概念列表，用于识别需要中断的消息.
        semantic_markov_model (SemanticMarkovModel): 语义马尔可夫模型，
            用于计算上下文的意外度和重要性得分.
        objective_semantic_threshold (float): 目标语义阈值，超过此值则认为消息具有客观重要性.
        final_threshold (float): 最终得分阈值，超过此值则建议中断.
        alpha (float): 意外度得分的权重因子.
        beta (float): 核心重要性得分的权重因子.
    """

    def __init__(
        self,
        speaker_weights: dict[str, float],
        objective_keywords: list[str],
        core_importance_concepts: list[str],
        semantic_markov_model: SemanticMarkovModel,
        objective_semantic_threshold: float = 0.85,
        final_threshold: float = 90,
        alpha: float = 0.4,
        beta: float = 0.6,
    ) -> None:
        self.speaker_weights = speaker_weights
        self.objective_keywords = objective_keywords
        self.core_importance_concepts = core_importance_concepts
        self.final_threshold = final_threshold
        self.alpha = alpha
        self.beta = beta

        self.semantic_markov_model = semantic_markov_model
        self.semantic_model = self.semantic_markov_model.semantic_model

        if self.core_importance_concepts:
            self.core_concepts_encoded = self.semantic_model.encode(self.core_importance_concepts)
        else:
            self.core_concepts_encoded = np.array([])

        self.objective_semantic_threshold = objective_semantic_threshold

        logger.info(f"判断器已初始化，核心重要概念数量: {len(self.core_importance_concepts)}, ")

    def _calculate_objective_importance(self, message_text: str) -> float:
        for keyword in self.objective_keywords:
            if keyword in message_text:
                logger.info(f"**[IIS-阶段一]** 检测到霸道关键词 '{keyword}'！客观重要性极高！")
                return 1.0
        logger.debug("**[IIS-阶段一]** 未检测到霸道关键词。客观重要性得分为 0.0。")
        return 0.0

    # 阶段二：计算上下文衔接意外度和核心重要性得分
    # 这里我们会计算当前消息与上下文的衔接意外度
    def _calculate_contextual_scores(
        self, message_text: str, context_message_text: str | None
    ) -> float:
        unexpectedness_score = self.semantic_markov_model.calculate_contextual_unexpectedness(
            current_text=message_text, previous_text=context_message_text
        )
        logger.info(
            f"**[IIS-阶段二-A]** 上下文衔接意外度得分为: {unexpectedness_score:.2f} "
            f"(对比上文: '{context_message_text[:50]}...')"
        )

        if self.core_concepts_encoded.size == 0:
            importance_score = 0.0
            logger.debug("**[IIS-阶段二-B]** 无核心重要概念，内容重要性得分为 0.0。")
        else:
            message_vector = self.semantic_model.encode([message_text])
            similarities = cosine_similarity(
                message_vector,
                self.core_concepts_encoded,
            )
            importance_score = np.max(similarities) * 100
            logger.info(f"**[IIS-阶段二-B]** 内容核心重要性得分为: {importance_score:.2f}")

        preliminary_score = self.alpha * unexpectedness_score + self.beta * importance_score
        logger.info(f"**[IIS-阶段二-C]** 融合后的基础快感分数为: {preliminary_score:.2f}")
        return preliminary_score

    def _get_speaker_weight(self, speaker_id: str) -> float:
        weight = self.speaker_weights.get(speaker_id, self.speaker_weights.get("default", 1.0))
        logger.info(f"**[IIS-阶段三]** 发言者 '{speaker_id}' 的主观权重为: {weight}")
        return weight

    def should_interrupt(self, new_message: dict, context_message_text: str | None) -> bool:
        """判断是否需要中断当前消息的处理.

        Args:
            new_message (dict): 新消息的字典，必须包含 'text' 和 'speaker_id' 键.
            context_message_text (str | None): 上下文消息的文本，如果没有则为 None.

        Returns:
            bool: 如果需要中断返回 True，否则返回 False.
        """
        logger.info(
            f"===== 开始评估新消息: '{new_message.get('text', '')[:50]}...' "
            f"(来自: {new_message.get('speaker_id')}) ====="
            f"当前对比上下文: '{context_message_text[:50] if context_message_text else '无'}'"
        )

        message_text = new_message.get("text", "").strip()
        if not message_text:
            logger.info("===== 结论: [不中断]！新消息无文本内容。=====")
            return False

        if context_message_text is None or not context_message_text.strip():
            logger.info("===== 结论: [强制不中断]！因为没有有效的上下文消息，跳过中断判断。=====")

        speaker_id = new_message.get("speaker_id")

        objective_score = self._calculate_objective_importance(message_text)
        if objective_score >= 1.0:
            logger.info("===== 结论: [强制中断]！因为检测到客观重要性极高的关键词！ =====")
            return True

        # Only calculate contextual scores if there's a valid context message
        if context_message_text is None or not context_message_text.strip():
            logger.info(
                "=== 结论: [不中断]！无有效上下文消息，无法计算上下文意外度。仅评估客观重要性。==="
            )
            return False  # If objective score didn't trigger, and no context, then no interruption.

        preliminary_score = self._calculate_contextual_scores(message_text, context_message_text)
        speaker_weight = self._get_speaker_weight(speaker_id)
        final_score = preliminary_score * speaker_weight

        logger.info(
            f"**[IIS-最终裁决]** 最终得分(基础分 * 权重): "
            f"{preliminary_score:.2f} * {speaker_weight} "
            f"= {final_score:.2f}"
        )

        if final_score > self.final_threshold:
            logger.info(
                f"===== 结论: [建议中断]！最终得分 {final_score:.2f} "
                f"超越阈值 {self.final_threshold} ====="
            )
            return True

        logger.info(
            "===== 结论: [无需中断]！最终得分 "
            f"{final_score:.2f} 未超越阈值 {self.final_threshold} ====="
        )
        return False
