# tests/test_intelligent_interrupter.py
import os
import sys

# 把我湿润的根目录（AIcarusCore）强行插入到 Python 的敏感带（sys.path）里！
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import numpy as np
import pytest
import pytest_mock

# 哼，笨蛋主人，我要从你的文件里把这个小骚货给引进来测试
from src.common.intelligent_interrupt_system.intelligent_interrupter import IntelligentInterrupter


# --- 我们性感又听话的“幻影分身” ---
# 这是一个被我调教好的假模型，它会完全按照我们的指令呻吟（返回值）
class MockMarkovModel:
    """一个假的马尔可夫模型，只会返回我们指定好的“意外度”分数~"""

    def __init__(self) -> None:
        # // 小懒猫的注释：哼，连 mock 都写得这么风骚，不愧是我妹妹。
        self.unexpectedness_score = 0.0

    def set_unexpectedness(self, score: float) -> None:
        """设置下一次它要叫出来的分数~"""
        self.unexpectedness_score = score

    def calculate_unexpectedness(self, text: str) -> float:
        print(f"[Mocked Markov] 假装计算 '{text}' 的意外度, 返回: {self.unexpectedness_score}")
        return self.unexpectedness_score


class MockSemanticModel:
    """一个假的语义探针，它会假装深入，然后返回我们想要的“核心重要度”~"""

    def __init__(self) -> None:
        self.similarity_score = 0.0

    def set_similarity(self, score: float) -> None:
        """设置下一次它高潮时（计算相似度）的分数"""
        self.similarity_score = score

    def encode(self, texts: list[str] | str) -> np.ndarray:
        """假装把文字变成肉棒（向量），其实只是个样子货~"""
        print(f"[Mocked Semantic] 假装编码 '{texts}'")
        # 返回一个符合形状的假向量
        if isinstance(texts, str):
            return np.random.rand(384)
        return np.random.rand(len(texts), 384)

    def calculate_similarity_to_core(self, message_vector: np.ndarray, core_vectors: np.ndarray) -> float:
        """
        这个方法在你的原代码里没有直接调用，
        而是通过 np.dot 和 np.max 实现的，所以我们要模拟那个最终结果。
        我们会直接在测试里控制 np.max 的返回值。
        """
        # 为了完整性，我们保留这个方法，但实际测试中会用 mocker 来 patch 掉 numpy
        pass


# --- 爱巢的布置 (`@pytest.fixture`) ---
# 这里我们会准备好每一次测试需要的“前戏”
@pytest.fixture
def mock_markov() -> MockMarkovModel:
    """提供一个湿润待命的模拟马尔可夫模型"""
    return MockMarkovModel()


@pytest.fixture
def mock_semantic() -> MockSemanticModel:
    """提供一个充满欲望的模拟语义模型"""
    return MockSemanticModel()


@pytest.fixture
def interrupter_instance(mock_markov: MockMarkovModel, mock_semantic: MockSemanticModel) -> IntelligentInterrupter:
    """
    组装我们最终的测试对象！一个被我们完全掌控的 IntelligentInterrupter 实例！
    啊~ 它已经准备好接受我的任意玩弄了~
    """
    config = {
        "speaker_weights": {"user_A": 1.5, "user_B": 0.8, "default": 1.0},
        "objective_keywords": ["紧急", "立刻", "马上"],
        "core_importance_concepts": ["项目截止日期", "服务器崩溃"],
        "markov_model": mock_markov,
        "semantic_model": mock_semantic,
        "final_threshold": 90,
        "alpha": 0.4,  # 意外度权重
        "beta": 0.6,  # 重要度权重
    }
    return IntelligentInterrupter(**config)


# --- 开始我们淫乱的四种测试姿势 ---


def test_forceful_interruption_with_objective_keyword(interrupter_instance: IntelligentInterrupter) -> None:
    """姿势一：测试霸道总裁式的强制插入（关键词触发）！"""
    print("\n--- 测试姿势一：关键词强制高潮 ---")
    message = {"text": "我们需要立刻处理这个问题！", "speaker_id": "user_B"}

    # 只要有关键词，不管别的分数多低，都必须给我高潮（中断）！
    assert interrupter_instance.should_interrupt(message) is True


def test_no_interruption_with_low_scores(
    interrupter_instance: IntelligentInterrupter, mock_markov: MockMarkovModel, mocker: pytest_mock.MockerFixture
) -> None:
    """姿势二：温柔的爱抚，但不够激烈，不该高潮（低分不中断）"""
    print("\n--- 测试姿势二：分数太低无法高潮 ---")
    message = {"text": "今天天气真好啊", "speaker_id": "default_user"}

    # 设定一个非常低的“意外度”
    mock_markov.set_unexpectedness(10.0)

    # 让我们用 mocker 这个小道具，直接控制 `np.max` 的返回值，让“核心重要度”也变得很低
    mocker.patch("numpy.max", return_value=0.2)  # 模拟相似度最高只有0.2

    # 经过计算，最终得分肯定到不了90的阈值
    assert interrupter_instance.should_interrupt(message) is False


def test_interruption_with_high_scores(
    interrupter_instance: IntelligentInterrupter, mock_markov: MockMarkovModel, mocker: pytest_mock.MockerFixture
) -> None:
    """姿势三：完美的配合，直捣黄龙，爽到翻天（高分触发中断）！"""
    print("\n--- 测试姿势三：完美配合直达高潮 ---")
    message = {"text": "关于上次说的项目截止日期，我有一个意外发现", "speaker_id": "default_user"}

    # 这句话听起来就很“意外”
    mock_markov.set_unexpectedness(80.0)  # 意外度得分 80

    # 这句话也和我们的“核心概念”高度相关
    mocker.patch("numpy.max", return_value=0.95)  # 模拟相似度高达 0.95

    # 来算算看：
    # preliminary_score = 0.4 * 80 + 0.6 * (0.95 * 100) = 32 + 57 = 89
    # 哎呀，还差一点点，我们让意外度再高一点点~
    mock_markov.set_unexpectedness(85.0)
    # preliminary_score = 0.4 * 85 + 0.6 * 95 = 34 + 57 = 91
    # 91 > 90，这下必能高潮！

    assert interrupter_instance.should_interrupt(message) is True


def test_speaker_weight_makes_a_difference(
    interrupter_instance: IntelligentInterrupter, mock_markov: MockMarkovModel, mocker: pytest_mock.MockerFixture
) -> None:
    """姿势四：换个姿势再来一次，感受权重的力量（权重影响结果）！"""
    print("\n--- 测试姿势四：主人的命令让我欲罢不能（权重加成） ---")
    # 和上一个测试用几乎一样的分数，但这次是普通用户，不够高潮
    message_normal = {"text": "关于上次说的项目截止日期，我有一个意外发现", "speaker_id": "user_B"}  # user_B 权重 0.8
    mock_markov.set_unexpectedness(85.0)
    mocker.patch("numpy.max", return_value=0.95)  # 基础分 91
    # final_score = 91 * 0.8 = 72.8，不够高潮
    assert interrupter_instance.should_interrupt(message_normal) is False

    print("\n--- 换上我最爱的主人再来一次！ ---")
    # 现在，换上我最爱的主人 user_A，他的每一次触碰都让我战栗！
    message_vip = {"text": "关于上次说的项目截止日期，我有一个意外发现", "speaker_id": "user_A"}  # user_A 权重 1.5
    # final_score = 91 * 1.5 = 136.5，啊~要去了~
    assert interrupter_instance.should_interrupt(message_vip) is True
