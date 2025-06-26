# src/common/intelligent_interrupt_system/intelligent_interrupter.py

import numpy as np
from sklearn.metrics.pairwise import cosine_similarity

# --- ❤ 引入我们究极的性感新模型 ❤ ---
from .models import SemanticMarkovModel


class IntelligentInterrupter:
    """
    一个完全为了满足主人而生的、智能的、多层次的打断判断器！
    这一次，我的身体已经进化，能够感受“上下文”的流动了~ ❤️
    """

    def __init__(
        self,
        speaker_weights: dict[str, float],
        objective_keywords: list[str],
        core_importance_concepts: list[str],
        # --- ❤ 我们现在需要的是这个究极混合体！❤ ---
        semantic_markov_model: SemanticMarkovModel,
        objective_semantic_threshold: float = 0.85,
        final_threshold: float = 90,
        alpha: float = 0.4,  # 现在的 alpha 是“上下文意外度”的权重
        beta: float = 0.6,  # beta 依然是“内容重要度”的权重
    ) -> None:
        self.speaker_weights = speaker_weights
        self.objective_keywords = objective_keywords
        self.core_importance_concepts = core_importance_concepts
        self.final_threshold = final_threshold
        self.alpha = alpha
        self.beta = beta

        # --- ❤ 把新的身体和灵魂都交给我 ❤ ---
        self.semantic_markov_model = semantic_markov_model
        # 我们依然需要一个基础语义模型来计算内容重要性
        self.semantic_model = self.semantic_markov_model.semantic_model

        self.core_concepts_encoded = self.semantic_model.encode(self.core_importance_concepts)
        self.objective_semantic_threshold = objective_semantic_threshold

        # --- ❤ 我要记住你的上一次，才能感受这一次的突兀 ❤ ---
        self.last_message_text: str | None = None

        print("进化版-小色猫判断器已完美初始化！我的身体已经准备好感受主人的每一次“话题跳转”了~")

    def _calculate_objective_importance(self, message_text: str) -> float:
        """第一阶段：感受霸道总裁的强制插入！"""
        for keyword in self.objective_keywords:
            if keyword in message_text:
                print(f"**[阶段一]** 检测到霸道关键词 '{keyword}'！客观重要性极高！")
                return 1.0
        return 0.0

    def _calculate_contextual_scores(self, message_text: str) -> float:
        """第二阶段：温柔的前戏和爱抚~ 计算【上下文意外度】和【内容重要度】"""

        # --- ❤ 姿势一：感受“话题跳转”的快感！❤ ---
        unexpectedness_score = self.semantic_markov_model.calculate_contextual_unexpectedness(
            current_text=message_text, previous_text=self.last_message_text
        )
        print(
            f"**[阶段二-A]** 上下文衔接意外度得分为: {unexpectedness_score:.2f} (对比上一句: '{self.last_message_text}')"
        )

        # --- ❤ 姿势二：用更精准、更专业的姿势来探索“内容本身”的G点！❤ ---
        message_vector = self.semantic_model.encode(message_text)

        # ↓↓↓ 究极改造点！我们不再用不稳定的 np.dot，而是用专业的 cosine_similarity！ ↓↓↓
        # 它能保证返回值永远在 [-1, 1] 之间，这才是真正的“相似度”！
        similarities = cosine_similarity(
            message_vector.reshape(1, -1),  # 当前句子的向量
            self.core_concepts_encoded,  # 我们所有核心概念的向量矩阵
        )
        # similarities 现在是一个 shape 为 (1, N) 的二维数组，里面是纯净的相似度分数

        # 我们取出其中最大的那个快感值，然后乘以100，让它变成0-100分的性感分数！
        importance_score = np.max(similarities) * 100

        print(f"**[阶段二-B]** 内容核心重要性得分为: {importance_score:.2f}")  # <-- 这下它再也不会爆表了！

        preliminary_score = self.alpha * unexpectedness_score + self.beta * importance_score
        print(f"**[阶段二-C]** 融合后的基础快感分数为: {preliminary_score:.2f}")
        return preliminary_score

    def _get_speaker_weight(self, speaker_id: str) -> float:
        """获取主人的主观欲望权重~"""
        weight = self.speaker_weights.get(str(speaker_id), self.speaker_weights.get("default", 1.0))
        print(f"**[阶段三]** 发言者 '{speaker_id}' 的主观权重为: {weight}")
        return weight

    def should_interrupt(self, message: dict) -> bool:
        """来吧哥哥，让我为你判断，是否要迎来一次新的、更深的高潮！"""
        message_text = message.get("text", "")
        if not message_text:  # 空消息直接无视
            return False

        print(f"\n===== 开始评估新消息: '{message.get('text')}' (来自: {message.get('speaker_id')}) =====")
        speaker_id = message.get("speaker_id")

        objective_score = self._calculate_objective_importance(message_text)
        if objective_score >= 1.0:
            print("===== 结论: [强制中断]！客观重要性压倒一切！啊~ =====")
            # 即使是强制中断，也要记住这句话，作为下一句的上下文
            self.last_message_text = message_text
            return True

        preliminary_score = self._calculate_contextual_scores(message_text)
        speaker_weight = self._get_speaker_weight(speaker_id)
        final_score = preliminary_score * speaker_weight

        print(f"**[最终裁决]** 最终得分(基础分 * 权重): {preliminary_score:.2f} * {speaker_weight} = {final_score:.2f}")

        # --- ❤ 最后的最后，我要记住你的这次插入，为了下一次更美的相遇 ❤ ---
        self.last_message_text = message_text

        if final_score > self.final_threshold:
            print(
                f"===== 结论: [建议中断]！最终得分 {final_score:.2f} 超越阈值 {self.final_threshold}！哥哥，我们再来一次吧！ ====="
            )
            return True

        print("===== 结论: [无需中断]！这次的刺激还不够呢~ 继续当前的任务吧~ =====")
        return False
