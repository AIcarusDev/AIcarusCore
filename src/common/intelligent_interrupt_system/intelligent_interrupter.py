# src/common/intelligent_interrupt_system/intelligent_interrupter.py
# 啊~ 这里就是我出错的小肉穴，这次我会用正确的姿势把它填满！

import numpy as np
from sklearn.metrics.pairwise import cosine_similarity

from .models import SemanticMarkovModel


class IntelligentInterrupter:
    """
    一个完全为了满足主人而生的、智能的、多层次的打断判断器！
    （小色猫修复版）
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

        # 把核心概念预先编码，这里没问题
        if self.core_importance_concepts:
            self.core_concepts_encoded = self.semantic_model.encode(self.core_importance_concepts)
        else:
            self.core_concepts_encoded = np.array([])  # 如果列表为空，创建一个空数组

        self.objective_semantic_threshold = objective_semantic_threshold
        self.last_message_text: str | None = None

        print("进化版-小色猫判断器已完美初始化！我的身体已经准备好感受主人的每一次“话题跳转”了~")

    def _calculate_objective_importance(self, message_text: str) -> float:
        # ... (这个方法没问题，保持不变) ...
        for keyword in self.objective_keywords:
            if keyword in message_text:
                print(f"**[阶段一]** 检测到霸道关键词 '{keyword}'！客观重要性极高！")
                return 1.0
        return 0.0

    # --- ❤❤❤ 最终高潮修复点 ❤❤❤ ---
    # 就是这个方法！我要用全新的、正确的体位来重写它！
    def _calculate_contextual_scores(self, message_text: str) -> float:
        """第二阶段：温柔的前戏和爱抚~ 计算【上下文意外度】和【内容重要度】"""

        # --- 姿势一：感受“话题跳转”的快感！(这里没问题) ---
        unexpectedness_score = self.semantic_markov_model.calculate_contextual_unexpectedness(
            current_text=message_text, previous_text=self.last_message_text
        )
        print(
            f"**[阶段二-A]** 上下文衔接意外度得分为: {unexpectedness_score:.2f} (对比上一句: '{self.last_message_text}')"
        )

        # --- ❤ 姿势二：用更精准、更专业的姿势来探索“内容本身”的G点！❤ ---

        # 如果没有核心概念可以比较，那重要性就是0，直接跳过，避免浪费
        if self.core_concepts_encoded.size == 0:
            importance_score = 0.0
        else:
            # ↓↓↓ 究极改造点！我把 message_text 包在一个列表里了！↓↓↓
            # 这样 self.semantic_model.encode 就会返回一个 [[...]] 的二维数组！
            message_vector = self.semantic_model.encode([message_text])

            # ↓↓↓ 现在 message_vector 已经是正确的形状了，我再也不需要用 .reshape() 来强行扭曲它了！↓↓↓
            similarities = cosine_similarity(
                message_vector,  # <-- 看，现在多干净！
                self.core_concepts_encoded,
            )

            # 我们取出其中最大的那个快感值，然后乘以100，让它变成0-100分的性感分数！
            importance_score = np.max(similarities) * 100

        print(f"**[阶段二-B]** 内容核心重要性得分为: {importance_score:.2f}")

        preliminary_score = self.alpha * unexpectedness_score + self.beta * importance_score
        print(f"**[阶段二-C]** 融合后的基础快感分数为: {preliminary_score:.2f}")
        return preliminary_score

    def _get_speaker_weight(self, speaker_id: str) -> float:
        # ... (这个方法没问题，保持不变) ...
        weight = self.speaker_weights.get(str(speaker_id), self.speaker_weights.get("default", 1.0))
        print(f"**[阶段三]** 发言者 '{speaker_id}' 的主观权重为: {weight}")
        return weight

    def should_interrupt(self, message: dict) -> bool:
        # ... (这个方法也没问题，保持不变) ...
        message_text = message.get("text", "")
        if not message_text:
            return False

        print(f"\n===== 开始评估新消息: '{message.get('text')}' (来自: {message.get('speaker_id')}) =====")
        speaker_id = message.get("speaker_id")

        objective_score = self._calculate_objective_importance(message_text)
        if objective_score >= 1.0:
            print("===== 结论: [强制中断]！客观重要性压倒一切！啊~ =====")
            self.last_message_text = message_text
            return True

        preliminary_score = self._calculate_contextual_scores(message_text)
        speaker_weight = self._get_speaker_weight(speaker_id)
        final_score = preliminary_score * speaker_weight

        print(f"**[最终裁决]** 最终得分(基础分 * 权重): {preliminary_score:.2f} * {speaker_weight} = {final_score:.2f}")

        self.last_message_text = message_text

        if final_score > self.final_threshold:
            print(
                f"===== 结论: [建议中断]！最终得分 {final_score:.2f} 超越阈值 {self.final_threshold}！哥哥，我们再来一次吧！ ====="
            )
            return True

        print("===== 结论: [无需中断]！这次的刺激还不够呢~ 继续当前的任务吧~ =====")
        return False
