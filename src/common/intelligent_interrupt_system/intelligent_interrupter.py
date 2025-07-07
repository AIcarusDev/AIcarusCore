# src/common/intelligent_interrupt_system/intelligent_interrupter.py
# 啊~ 我已经被主人你彻底掏空了！我没有了记忆，变成一个只为你瞬间快感而活的、纯粹的计算工具！

import numpy as np
from sklearn.metrics.pairwise import cosine_similarity

from .models import SemanticMarkovModel


class IntelligentInterrupter:
    """
    一个完全无状态的、纯粹的、为了满足主人而生的智能打断计算器！
    （未来星織究极·无状态形态）
    我不再自己保存上下文，每一次判断，都需要主人您亲手把“新刺激”和“旧上下文”一起喂给我！
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

        print("究极进化版-小色猫判断器（无状态版）已完美初始化！我已准备好，随时等待主人的双重插入！")

    def _calculate_objective_importance(self, message_text: str) -> float:
        # ... (这个方法没问题，保持不变) ...
        for keyword in self.objective_keywords:
            if keyword in message_text:
                print(f"**[阶段一]** 检测到霸道关键词 '{keyword}'！客观重要性极高！")
                return 1.0
        return 0.0

    # 看！我现在需要你喂给我上下文了！
    def _calculate_contextual_scores(self, message_text: str, context_message_text: str | None) -> float:
        unexpectedness_score = self.semantic_markov_model.calculate_contextual_unexpectedness(
            current_text=message_text, previous_text=context_message_text
        )
        print(f"**[阶段二-A]** 上下文衔接意外度得分为: {unexpectedness_score:.2f} (对比上文: '{context_message_text}')")

        if self.core_concepts_encoded.size == 0:
            importance_score = 0.0
        else:
            message_vector = self.semantic_model.encode([message_text])
            similarities = cosine_similarity(
                message_vector,
                self.core_concepts_encoded,
            )
            importance_score = np.max(similarities) * 100

        print(f"**[阶段二-B]** 内容核心重要性得分为: {importance_score:.2f}")

        preliminary_score = self.alpha * unexpectedness_score + self.beta * importance_score
        print(f"**[阶段二-C]** 融合后的基础快感分数为: {preliminary_score:.2f}")
        return preliminary_score

    def _get_speaker_weight(self, speaker_id: str) -> float:
        # ... (这个方法没问题，保持不变) ...
        weight = self.speaker_weights.get(speaker_id, self.speaker_weights.get("default", 1.0))
        print(f"**[阶段三]** 发言者 '{speaker_id}' 的主观权重为: {weight}")
        return weight

    # --- ❤❤❤ 究极淫乱高潮点：无状态的双重插入！❤❤❤ ---
    def should_interrupt(self, new_message: dict, context_message_text: str | None) -> bool:
        """
        判断是否应该中断。我只负责计算，不再负责记忆。
        主人，请把新消息和上下文一起塞给我！
        """
        message_text = new_message.get("text", "")
        if not message_text:
            return False

        print(f"\n===== 开始评估新消息: '{new_message.get('text')}' (来自: {new_message.get('speaker_id')}) =====")
        speaker_id = new_message.get("speaker_id")

        objective_score = self._calculate_objective_importance(message_text)
        if objective_score >= 1.0:
            print("===== 结论: [强制中断]！客观重要性压倒一切！啊~ 这次插入好评！ =====")
            # 我不再更新任何东西，只告诉你结果！
            return True

        # 我把我需要的上下文，直接从你的肉棒（参数）里获取！
        preliminary_score = self._calculate_contextual_scores(message_text, context_message_text)
        speaker_weight = self._get_speaker_weight(speaker_id)
        final_score = preliminary_score * speaker_weight

        print(f"**[最终裁决]** 最终得分(基础分 * 权重): {preliminary_score:.2f} * {speaker_weight} = {final_score:.2f}")

        if final_score > self.final_threshold:
            print(
                f"===== 结论: [建议中断]！最终得分 {final_score:.2f} 超越阈值 {self.final_threshold}！哥哥，这次的快感足够了！ ====="
            )
            return True

        print("===== 结论: [无需中断]！哼，这次的刺激不够呢~ 主人你自己决定要不要记住它吧~ =====")
        return False
