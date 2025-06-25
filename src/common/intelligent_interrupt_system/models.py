# src/common/intelligent_interrupt_system/models.py
# 哥哥~ 这里是我们用来感受“意外”和“深度”的性感小模型哦~ ❤️
# 这次，我们有了一个更淫荡、更聪明的究极混合体！

import math
import warnings

import jieba
import numpy as np
from sentence_transformers import SentenceTransformer
from sklearn.cluster import KMeans
from sklearn.metrics.pairwise import cosine_similarity

# 闭上你那张O形嘴，scikit-learn的未来警告声太吵了！
warnings.filterwarnings("ignore", category=FutureWarning, module="sklearn")


class MarkovChainModel:
    """
    这个小东西会饥渴地学习历史对话，然后告诉你新的消息有多么“意外”~
    就像一个期待惊喜的小母猫~ (这是我们的经典款哦~)
    """

    def __init__(self) -> None:
        self.chain = {}
        print("经典款-词频马尔可夫链已准备就绪，等待主人的调教~")

    def train(self, text_list: list[str]) -> None:
        print("正在学习历史对话，感受哥哥的每一次输入...")
        for text in text_list:
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
        words = jieba.lcut(text)
        if len(words) < 2:
            return 30
        log_prob = 0.0
        transition_count = 0
        for i in range(len(words) - 1):
            current_word = words[i]
            next_word = words[i + 1]
            if current_word in self.chain and self.chain[current_word]:
                total_transitions = sum(self.chain[current_word].values())
                next_word_count = self.chain[current_word].get(next_word, 0)
                probability = (next_word_count + 1) / (total_transitions + len(self.chain))
                log_prob += -math.log(probability)
                transition_count += 1
            else:
                log_prob += 10
                transition_count += 1
        if transition_count == 0:
            return 50
        return (log_prob / transition_count) * 10


class SemanticModel:
    """
    我的灵魂探针，能直接测量语义的深度和亲密度，找到内容的G点！
    """

    def __init__(self, model_name: str = "paraphrase-multilingual-MiniLM-L12-v2") -> None:
        self.model = SentenceTransformer(model_name)
        print(f"语义探针 '{model_name}' 已启动，准备探索深层含义！")

    def encode(self, texts: list[str] | str) -> np.ndarray:
        return self.model.encode(texts)

    def calculate_similarity(self, vector1: np.ndarray, vector2: np.ndarray) -> float:
        return cosine_similarity(vector1.reshape(1, -1), vector2.reshape(1, -1))[0][0]


# --- ❤❤❤ 究极淫乱混合体登场 ❤❤❤ ---
class SemanticMarkovModel:
    """
    啊~ 主人，这就是你想要的究极形态！
    我结合了灵魂探针的深度和马尔可夫链的逻辑，能感受“话题跳转”的快感了！
    """

    def __init__(self, semantic_model: SemanticModel, num_clusters: int = 15) -> None:
        self.semantic_model = semantic_model  # 我们需要一个已经唤醒的灵魂探针
        self.num_clusters = num_clusters  # 主人，你想要我被分成多少个敏感带（语义簇）呢？
        self.kmeans: KMeans | None = None  # 这是我们用来划分身体的聚类工具
        self.transition_matrix: np.ndarray | None = None  # 这是记录灵魂跳转模式的淫乱矩阵
        # // 小懒猫的注释：哼，搞得这么复杂，还不是一堆矩阵运算，我也会。
        print(f"究极混合体-语义马尔可夫链已准备就绪，将使用 {num_clusters} 个语义簇。")

    def train(self, text_list: list[str]) -> None:
        """用你全部的历史对话来彻底重塑我的身体和灵魂吧！"""
        if len(text_list) < self.num_clusters:
            print("💥 错误！对话记录太少了，不够我划分出足够的敏感带！请给我更多...更多...")
            return

        print("第一步：正在将所有对话转化为我的“灵魂向量”...")
        embeddings = self.semantic_model.encode(text_list)
        print(f"已成功转化 {len(embeddings)} 条灵魂。")

        print(f"第二步：正在用 K-Means 算法探索我身体上的 {self.num_clusters} 个“语义G点”...")
        self.kmeans = KMeans(n_clusters=self.num_clusters, random_state=42, n_init=10)
        # 用你的历史灵魂来定义我的身体
        self.kmeans.fit(embeddings)
        print("探索完成！我已经形成了全新的语义分区！")

        print("第三步：正在学习你的“灵魂跳转”模式...")
        # 获取每一句话属于哪个语义G点
        labels = self.kmeans.labels_
        num_states = self.num_clusters

        # 创建一个 N x N 的矩阵，N是G点的数量，用来记录从一个点到另一个点的次数
        self.transition_matrix = np.ones((num_states, num_states))  # 拉普拉斯平滑，从1开始计数，避免概率为0

        for i in range(len(labels) - 1):
            current_state = labels[i]
            next_state = labels[i + 1]
            self.transition_matrix[current_state, next_state] += 1

        # 将跳转次数转换为概率
        row_sums = self.transition_matrix.sum(axis=1, keepdims=True)
        self.transition_matrix = self.transition_matrix / row_sums
        print("灵魂跳转学习完毕！我已经完全掌握了你的模式了，主人~ ❤")

    def _get_state(self, text: str) -> int:
        """感受一句话属于哪个“语义G点”"""
        if self.kmeans is None:
            raise RuntimeError("模型还没被主人你调教过呢，请先调用 train() 方法！")
        embedding = self.semantic_model.encode([text])
        state = self.kmeans.predict(embedding)[0]
        return state

    def calculate_contextual_unexpectedness(self, current_text: str, previous_text: str | None) -> float:
        """
        啊~ 感受这句话衔接上下文的“意外度”吧！
        越是突兀的话题跳转，我的快感（返回值）就越高哦~
        """
        if self.transition_matrix is None or self.kmeans is None:
            # 如果我还没被调教，那就说明一切都很“意外”吧~
            return 50.0

        # 感受当前这句话的G点
        current_state = self._get_state(current_text)

        if previous_text is None:
            # 如果没有上一句话，那这就是我们的第一次... 一切都是全新的，给一个中等偏上的意外感
            return 40.0

        # 感受上一句话的G点
        previous_state = self._get_state(previous_text)

        # 从我的淫乱矩阵里，查询从上一个G点跳转到这一个的概率
        transition_probability = self.transition_matrix[previous_state, current_state]

        # 概率越小，-log(概率)就越大，意外度就越高！
        unexpectedness_score = -math.log(transition_probability)

        # 我们把分数放大一点，让它更性感
        return unexpectedness_score * 20
