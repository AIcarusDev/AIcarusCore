# src/common/intelligent_interrupt_system/intelligent_interrupter.py
from typing import TYPE_CHECKING, Optional

import numpy as np
from sklearn.metrics.pairwise import cosine_similarity
from src.common.custom_logging.logging_config import get_logger

from .models import SemanticMarkovModel

if TYPE_CHECKING:
    from src.domain.models import Stimulus


logger = get_logger(__name__)


class IntelligentInterrupter:
    """这是一个基于事件向量和领域模型的智能中断器."""

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

    def _calculate_contextual_scores(
        self, current_vector: list[float], context_vector: list[float] | None
    ) -> tuple[float, float]:
        """计算上下文意外度和内容核心重要性分数.

        Returns:
            一个元组 (意外度分数, 内容重要性分数)。
        """
        unexpectedness_score = self.semantic_markov_model.calculate_contextual_unexpectedness(
            current_vector=current_vector, previous_vector=context_vector
        )
        logger.info(
            f"**[IIS-阶段二-A]** 上下文衔接【事件】意外度得分为: {unexpectedness_score:.2f}"
        )

        if self.core_concepts_encoded.size == 0:
            importance_score = 0.0
            logger.debug("**[IIS-阶段二-B]** 无核心重要概念，内容重要性得分为 0.0。")
        else:
            similarities = cosine_similarity(
                np.array([current_vector]),
                self.core_concepts_encoded,
            )
            importance_score = np.max(similarities) * 100
            logger.info(f"**[IIS-阶段二-B]** 内容核心重要性得分为: {importance_score:.2f}")

        return unexpectedness_score, importance_score

    def _get_speaker_weight(self, speaker_id: str) -> float:
        weight = self.speaker_weights.get(speaker_id, self.speaker_weights.get("default", 1.0))
        logger.info(f"**[IIS-阶段三]** 发言者 '{speaker_id}' 的主观权重为: {weight}")
        return weight

    def should_interrupt(
        self, new_stimulus: "Stimulus", context_stimulus: Optional["Stimulus"]
    ) -> bool:
        """判断是否需要中断。现在接收完整的 Stimulus 领域模型对象."""
        if new_stimulus.embedding is None:
            logger.warning(
                f"事件 {new_stimulus.event_id} 缺少向量，无法进行上下文意外度评估。跳过中断判断。"
            )
            return False

        if context_stimulus and context_stimulus.embedding is None:
            logger.warning(f"上下文事件 {context_stimulus.event_id} 缺少向量，将作为无上下文处理。")
            context_stimulus = None

        message_text = new_stimulus.text_content
        speaker_id = new_stimulus.sender_id

        context_text_for_log = context_stimulus.text_content if context_stimulus else "无"

        logger.info(
            f"===== 开始评估新事件: '{message_text[:50]}...' "
            f"(来自: {speaker_id}) ====="
            f"当前对比上下文事件: '{context_text_for_log[:50]}...'"
        )

        if not message_text:
            logger.info("===== 结论: [不中断]！新事件无文本内容。=====")
            return False

        # --- 阶段一：客观重要性检查 (霸道规则) ---
        objective_score = self._calculate_objective_importance(message_text)
        if objective_score >= 1.0:
            logger.info("===== 结论: [强制中断]！因为检测到客观重要性极高的关键词！ =====")
            return True

        # --- 阶段二：上下文与内容评估 ---
        current_vector = new_stimulus.embedding
        context_vector = context_stimulus.embedding if context_stimulus else None

        if context_vector is None:
            logger.info("=== 结论: [不中断]！无有效上下文事件向量，无法计算上下文意外度。===")
            return False

        unexpectedness_score, importance_score = self._calculate_contextual_scores(
            current_vector, context_vector
        )

        # 增加第二层快速通道：如果内容本身极端重要，直接中断
        if importance_score >= (self.objective_semantic_threshold * 100):
            logger.info(
                f"===== 结论: [强制中断]！内容核心重要性得分 {importance_score:.2f} "
                f"超越了客观语义阈值 {self.objective_semantic_threshold * 100}！ ====="
            )
            return True

        # --- 阶段三：权重加成与最终裁决 ---
        preliminary_score = self.alpha * unexpectedness_score + self.beta * importance_score
        logger.info(f"**[IIS-阶段二-C]** 融合后的基础快感分数为: {preliminary_score:.2f}")

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
