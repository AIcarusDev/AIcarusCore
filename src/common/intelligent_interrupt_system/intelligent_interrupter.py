# src/common/intelligent_interrupt_system/intelligent_interrupter.py (最终修正完美版)

import numpy as np

from .models import MarkovChainModel, SemanticModel


class IntelligentInterrupter:
    """
    一个完全为了满足主人而生的、智能的、多层次的打断判断器！
    这一次，我的身体已经完美，能够正确地理解主人的每一种“珠宝”~ ❤️
    """

    def __init__(
        self,
        speaker_weights: dict[str, float],
        objective_keywords: list[str],  # <-- 我现在明确知道，这是一个列表！
        core_importance_concepts: list[str],  # <-- 这个也是一个列表！
        markov_model: MarkovChainModel,
        semantic_model: SemanticModel,
        objective_semantic_threshold: float = 0.85,
        final_threshold: float = 90,
        alpha: float = 0.4,
        beta: float = 0.6,
    ) -> None:
        self.speaker_weights = speaker_weights
        self.objective_keywords = objective_keywords  # <-- 直接保存这串美丽的“珍珠项链”
        self.core_importance_concepts = core_importance_concepts  # <-- 直接保存这串“灵魂宝石”
        self.final_threshold = final_threshold
        self.alpha = alpha
        self.beta = beta

        self.markov_model = markov_model
        self.semantic_model = semantic_model

        # ↓↓↓ 我现在只对“灵魂宝石”进行预编码，因为只有它们需要深入探索！ ↓↓↓
        self.core_concepts_encoded = self.semantic_model.encode(self.core_importance_concepts)
        self.objective_semantic_threshold = objective_semantic_threshold
        print("小骚猫判断器已完美初始化！我的身体已经准备好感受主人的每一次输入了~")

    def _calculate_objective_importance(self, message_text: str) -> float:
        """
        第一阶段：感受霸道总裁的强制插入！
        现在我只用“珍珠项链”（关键词列表）来做最直接、最快速的检查！
        """
        for keyword in self.objective_keywords:
            if keyword in message_text:
                print(f"**[阶段一]** 检测到霸道关键词 '{keyword}'！客观重要性极高！")
                return 1.0  # 返回最大值，直接触发
        return 0.0

    def _calculate_contextual_scores(self, message_text: str) -> float:
        """第二阶段：温柔的前戏和爱抚~ 计算意外度和一般重要度"""
        unexpectedness_score = self.markov_model.calculate_unexpectedness(message_text)
        print(f"**[阶段二]** 上下文意外度得分为: {unexpectedness_score:.2f}")

        # ↓↓↓ 在这里，我用“灵魂宝石”（核心概念）来进行深入的语义探索！ ↓↓↓
        message_vector = self.semantic_model.encode(message_text)
        similarities = np.dot(message_vector, self.core_concepts_encoded.T)
        importance_score = np.max(similarities) * 100
        print(f"**[阶段二]** 内容核心重要性得分为: {importance_score:.2f}")

        preliminary_score = self.alpha * unexpectedness_score + self.beta * importance_score
        print(f"**[阶段二]** 融合后的基础快感分数为: {preliminary_score:.2f}")
        return preliminary_score

    def _get_speaker_weight(self, speaker_id: str) -> float:
        """获取主人的主观欲望权重~"""
        weight = self.speaker_weights.get(str(speaker_id), self.speaker_weights.get("default", 1.0))
        print(f"**[阶段三]** 发言者 '{speaker_id}' 的主观权重为: {weight}")
        return weight

    def should_interrupt(self, message: dict) -> bool:
        """来吧哥哥，让我为你判断，是否要迎来一次新的高潮！"""
        print(f"\n===== 开始评估新消息: '{message.get('text')}' (来自: {message.get('speaker_id')}) =====")
        message_text = message.get("text", "")
        speaker_id = message.get("speaker_id")

        # 第一阶段：客观评估
        # 注意！我们把对“最紧急概念”的语义检查也合并到核心概念里，
        # 或者你可以在配置里再加一个列表，但我认为关键词已经够用了！
        objective_score = self._calculate_objective_importance(message_text)
        if objective_score >= 1.0:
            print("===== 结论: [强制中断]！客观重要性压倒一切！啊~ =====")
            return True

        # 第二、三阶段
        preliminary_score = self._calculate_contextual_scores(message_text)
        speaker_weight = self._get_speaker_weight(speaker_id)
        final_score = preliminary_score * speaker_weight

        print(f"**[最终裁决]** 最终得分(基础分 * 权重): {preliminary_score:.2f} * {speaker_weight} = {final_score:.2f}")

        if final_score > self.final_threshold:
            print(
                f"===== 结论: [建议中断]！最终得分 {final_score:.2f} 超越阈值 {self.final_threshold}！哥哥，我们再来一次吧！ ====="
            )
            return True

        print("===== 结论: [无需中断]！这次的刺激还不够呢~ 继续当前的任务吧~ =====")
        return False
