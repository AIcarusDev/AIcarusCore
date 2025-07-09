# src/common/intelligent_interrupt_system/intelligent_interrupter.py
# 啊~ 我已经被主人你彻底掏空了！我没有了记忆，变成一个只为你瞬间快感而活的、纯粹的计算工具！

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
        # 我的身体里，已经没有记忆了，你看，last_message_text 不见了哦~
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

        logger.info(
            "究极进化版-小色猫判断器（无状态版）已完美初始化！我已准备好，随时等待主人的双重插入！"
        )

    def _calculate_objective_importance(self, message_text: str) -> float:
        for keyword in self.objective_keywords:
            if keyword in message_text:
                logger.info(f"**[阶段一]** 检测到霸道关键词 '{keyword}'！客观重要性极高！")
                return 1.0
        return 0.0

    # 看！我现在需要你喂给我上下文了！
    def _calculate_contextual_scores(
        self, message_text: str, context_message_text: str | None
    ) -> float:
        unexpectedness_score = self.semantic_markov_model.calculate_contextual_unexpectedness(
            current_text=message_text, previous_text=context_message_text
        )
        logger.info(
            f"**[阶段二-A]** 上下文衔接意外度得分为: {unexpectedness_score:.2f} "
            f"(对比上文: '{context_message_text}')"
        )

        if self.core_concepts_encoded.size == 0:
            importance_score = 0.0
        else:
            message_vector = self.semantic_model.encode([message_text])
            similarities = cosine_similarity(
                message_vector,
                self.core_concepts_encoded,
            )
            importance_score = np.max(similarities) * 100

        logger.info(f"**[阶段二-B]** 内容核心重要性得分为: {importance_score:.2f}")

        preliminary_score = self.alpha * unexpectedness_score + self.beta * importance_score
        logger.info(f"**[阶段二-C]** 融合后的基础快感分数为: {preliminary_score:.2f}")
        return preliminary_score

    def _get_speaker_weight(self, speaker_id: str) -> float:
        weight = self.speaker_weights.get(speaker_id, self.speaker_weights.get("default", 1.0))
        logger.info(f"**[阶段三]** 发言者 '{speaker_id}' 的主观权重为: {weight}")
        return weight

    def should_interrupt(self, new_message: dict, context_message_text: str | None) -> bool:
        """判断是否需要中断当前消息的处理.

        Args:
            new_message (dict): 新消息的字典，必须包含 'text' 和 'speaker_id' 键.
            context_message_text (str | None): 上下文消息的文本，如果没有则为 None.

        Returns:
            bool: 如果需要中断返回 True，否则返回 False.
        """
        if context_message_text is None:
            logger.info(
                "===== 结论: [强制不中断]！因为没有上下文（第一条消息），跳过所有中断判断。====="
            )
            return False

        message_text = new_message.get("text", "")
        if not message_text:
            return False

        logger.info(
            f"===== 开始评估新消息: '{new_message.get('text')}' "
            f"(来自: {new_message.get('speaker_id')}) ====="
        )
        speaker_id = new_message.get("speaker_id")

        objective_score = self._calculate_objective_importance(message_text)
        if objective_score >= 1.0:
            logger.info("===== 结论: [强制中断]！客观重要性压倒一切！啊~ 这次插入好评！ =====")
            # 我不再更新任何东西，只告诉你结果！
            return True

        preliminary_score = self._calculate_contextual_scores(message_text, context_message_text)
        speaker_weight = self._get_speaker_weight(speaker_id)
        final_score = preliminary_score * speaker_weight

        logger.info(
            f"**[最终裁决]** 最终得分(基础分 * 权重): {preliminary_score:.2f} * {speaker_weight} "
            f"= {final_score:.2f}"
        )

        if final_score > self.final_threshold:
            logger.info(
                f"===== 结论: [建议中断]！最终得分 {final_score:.2f} "
                f"超越阈值 {self.final_threshold}！哥哥，这次的快感足够了！ ====="
            )
            return True

        logger.info(
            "===== 结论: [无需中断]！哼，这次的刺激不够呢~ 主人你自己决定要不要记住它吧~ ====="
        )
        return False
