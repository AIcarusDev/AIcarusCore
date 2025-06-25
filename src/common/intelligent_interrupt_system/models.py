# models.py
# 哥哥~ 这里是我们用来感受“意外”和“深度”的性感小模型哦~ ❤️

import math

import jieba
import numpy as np
from sentence_transformers import SentenceTransformer

# ↓↓↓ 就是这里！把这个性感的工具请进来！ ↓↓↓
from sklearn.metrics.pairwise import cosine_similarity


class MarkovChainModel:
    """
    这个小东西会饥渴地学习历史对话，然后告诉你新的消息有多么“意外”~
    就像一个期待惊喜的小母猫~
    """

    def __init__(self) -> None:
        # 用一个嵌套的字典来储存我们学习到的对话“体位”
        self.chain = {}
        # // 小懒猫的注释：哼，这么简单的结构，我一秒就能写出来。
        print("马尔可夫链模型已准备就绪，等待主人的调教~")

    def train(self, text_list: list[str]) -> None:
        """用历史消息来喂饱我，让我学习哥哥的说话习惯~"""
        print("正在学习历史对话，感受哥哥的每一次输入...")
        for text in text_list:
            # 用我们的小舌头(jieba)把句子切开
            words = jieba.lcut(text)
            if len(words) < 2:
                continue
            for i in range(len(words) - 1):
                current_word = words[i]
                next_word = words[i + 1]
                if current_word not in self.chain:
                    self.chain[current_word] = {}
                if next_word not in self.chain[current_word]:
                    self.chain[current_word][next_word] = 0
                self.chain[current_word][next_word] += 1
        print("学习完毕！我已经熟悉哥哥的模式了~")

    def calculate_unexpectedness(self, text: str) -> float:
        """计算这句话的“意外度”，越意外，得分越高哦~"""
        words = jieba.lcut(text)
        if len(words) < 2:
            return 30  # 太短的消息本身就有点“意外”或“无聊”，给个中等分数

        log_prob = 0.0
        transition_count = 0

        for i in range(len(words) - 1):
            current_word = words[i]
            next_word = words[i + 1]

            if current_word in self.chain and self.chain[current_word]:
                total_transitions = sum(self.chain[current_word].values())
                next_word_count = self.chain[current_word].get(next_word, 0)

                # 拉普拉斯平滑，防止概率为0，避免除零的尴尬~
                probability = (next_word_count + 1) / (total_transitions + len(self.chain))
                log_prob += -math.log(probability)
                transition_count += 1
            else:
                # 如果这个词从未出现过，那它本身就是个巨大的意外！
                log_prob += 10  # 给予一个较高的惩罚值
                transition_count += 1

        if transition_count == 0:
            return 50  # 无法计算，给予一个较高的意外分

        # 标准化意外度得分，乘以10使其范围更可用
        return (log_prob / transition_count) * 10


class SemanticModel:
    """
    我的灵魂探针，能直接测量语义的深度和亲密度，找到内容的G点！
    """

    def __init__(self, model_name: str = "paraphrase-multilingual-MiniLM-L12-v2") -> None:
        # 加载一个强大的预训练模型，它懂很多语言和姿势哦~
        self.model = SentenceTransformer(model_name)
        print(f"语义探针 '{model_name}' 已启动，准备探索深层含义！")

    def encode(self, texts: list[str]) -> np.ndarray:
        """将文字转换成能被我感知的“精神向量”"""
        return self.model.encode(texts)

    def calculate_similarity(self, vector1: np.ndarray, vector2: np.ndarray) -> float:
        """计算两个“精神”有多么贴近~ 返回0到1之间的亲密度"""
        # 现在，这里的 cosine_similarity 已经被正确地定义了，可以尽情地使用了~
        return cosine_similarity(vector1.reshape(1, -1), vector2.reshape(1, -1))[0][0]
