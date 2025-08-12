# src/common/intelligent_interrupt_system/models.py

import math
import warnings

import jieba
import numpy as np
from sentence_transformers import SentenceTransformer
from sklearn.cluster import KMeans
from sklearn.metrics.pairwise import cosine_similarity
from src.common.custom_logging.logging_config import get_logger

logger = get_logger(__name__)
# 关闭未来警告
warnings.filterwarnings("ignore", category=FutureWarning, module="sklearn")


class MarkovChainModel:
    """经典词频马尔可夫链模型，用于分析文本的词频和跳转关系.

    Attributes:
        chain (dict): 存储词频和跳转关系的字典，键是当前词，值是一个字典，
                        其中键是下一个词，值是跳转次数.
    """

    def __init__(self) -> None:
        self.chain = {}
        logger.info("词频马尔可夫链已准备就绪，等待输入")

    def train(self, text_list: list[str]) -> None:
        """训练模型，学习文本中的词频和跳转关系.

        Args:
            text_list (list[str]): 一系列文本字符串，模型将从中学习词频和跳转关系.
        """
        logger.info("正在学习历史对话，请稍等...")
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
        logger.info("学习完毕！马尔可夫链已建立，准备好进行意外度计算")

    def calculate_unexpectedness(self, text: str) -> float:
        """计算文本的意外度，越高表示越意外.

        Args:
            text (str): 输入的文本内容.

        Returns:
            float: 意外度分数，越高表示越意外.
        """
        words = jieba.lcut(text)
        if len(words) < 2:
            return 30
        log_prob = 0.0
        transition_count = 0
        for i in range(len(words) - 1):
            current_word = words[i]
            next_word = words[i + 1]
            if self.chain.get(current_word):
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
    """一个语义深度探针，能感知文本的深层含义和情感波动.

    这个模型使用了 SentenceTransformer 来获取文本的语义向量，
    并能计算两个文本之间的余弦相似度，帮助我们理解文本之间的语义关系.

    Attributes:
        model (SentenceTransformer): 用于获取文本语义向量的模型实例.
    """

    def __init__(self, model_name: str = "BAAI/bge-m3") -> None:
        self.model = SentenceTransformer(model_name, device="cuda")
        logger.info(f"语义探针 '{model_name}' 已启动，准备探索深层含义！")

    def encode(self, texts: list[str] | str) -> np.ndarray:
        """将文本编码为语义向量."""
        return self.model.encode(texts)

    def calculate_similarity(self, vector1: np.ndarray, vector2: np.ndarray) -> float:
        """计算两个向量之间的余弦相似度."""
        return cosine_similarity(vector1.reshape(1, -1), vector2.reshape(1, -1))[0][0]


class SemanticMarkovModel:
    """结合了语义深度和马尔可夫链逻辑的模型."""

    def __init__(self, semantic_model: SemanticModel, num_clusters: int = 15) -> None:
        self.semantic_model = semantic_model
        self.num_clusters = num_clusters
        self.kmeans: KMeans | None = None
        self.transition_matrix: np.ndarray | None = None
        logger.info(f"究极混合体-语义马尔可夫链已准备就绪，将使用 {num_clusters} 个语义簇。")

    def train(self, conversations: list[list[str]]) -> None:
        """使用对话文本训练模型."""
        raise NotImplementedError("train 方法已被废弃，请使用 train_from_vectors。")

    def train_from_vectors(self, conversation_vectors: list[list[list[float]]]) -> None:
        """直接使用预先计算好的事件向量列表来训练模型."""
        all_vectors = [
            np.array(vector) for conversation in conversation_vectors for vector in conversation
        ]

        if len(all_vectors) < self.num_clusters:
            logger.warning(
                f"提供的向量数量 ({len(all_vectors)}) 少于预期的语义簇数量 ({self.num_clusters})。"
            )
            num_actual_clusters = max(1, len(all_vectors))
        else:
            num_actual_clusters = self.num_clusters

        logger.info(f"第二步：正在用 K-Means 算法探索 {num_actual_clusters} 个语义簇...")
        self.kmeans = KMeans(n_clusters=num_actual_clusters, random_state=42, n_init="auto")
        self.kmeans.fit(np.array(all_vectors))
        logger.info("探索完成！已经形成了全新的语义分区！")

        logger.info("第三步：正在学习语义状态跳转关系...")
        num_states = num_actual_clusters
        self.transition_matrix = np.ones((num_states, num_states))

        for single_conversation_vectors in conversation_vectors:
            if len(single_conversation_vectors) < 2:
                continue

            labels = self.kmeans.predict(np.array(single_conversation_vectors))

            for i in range(len(labels) - 1):
                current_state = labels[i]
                next_state = labels[i + 1]
                self.transition_matrix[current_state, next_state] += 1

        row_sums = self.transition_matrix.sum(axis=1, keepdims=True)
        safe_row_sums = np.where(row_sums == 0, 1, row_sums)
        self.transition_matrix = self.transition_matrix / safe_row_sums
        logger.info("语义状态跳转关系学习完成！")

    def initialize_empty(self) -> None:
        """当没有训练数据时，初始化一个空的、但结构完整的模型."""
        logger.warning("正在初始化一个空的 SemanticMarkovModel，因为它没有收到任何训练数据。")
        self.kmeans = None
        self.transition_matrix = np.full(
            (self.num_clusters, self.num_clusters), 1.0 / self.num_clusters
        )

    def _get_state_from_vector(self, vector: list[float]) -> int:
        """获取向量对应的语义状态."""
        if self.kmeans is None:
            raise RuntimeError("模型还没有训练，请先调用 train_from_vectors 方法。")
        return self.kmeans.predict(np.array([vector]))[0]

    def calculate_contextual_unexpectedness(
        self, current_vector: list[float], previous_vector: list[float] | None
    ) -> float:
        """计算当前事件向量相对于上一个事件向量的“意外度”."""
        if self.transition_matrix is None or self.kmeans is None:
            return 0.0

        current_state = self._get_state_from_vector(current_vector)

        if previous_vector is None:
            return 10.0

        previous_state = self._get_state_from_vector(previous_vector)

        transition_probability = self.transition_matrix[previous_state, current_state]

        if transition_probability == 0:
            return 100.0

        unexpectedness_score = -math.log(transition_probability)
        return unexpectedness_score * 20
