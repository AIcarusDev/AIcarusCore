# tests/common/intelligent_interrupt_system/test_intelligent_interrupter.py

import numpy as np
import pytest
from pytest_mock import MockerFixture
from src.common.intelligent_interrupt_system.intelligent_interrupter import IntelligentInterrupter
from src.domain.models import Stimulus


@pytest.fixture
def mock_semantic_markov_model(mocker: MockerFixture) -> MockerFixture:
    """模拟 SemanticMarkovModel，并允许我们控制其核心方法的返回值."""
    mock = mocker.MagicMock()

    # 模拟 calculate_contextual_unexpectedness 方法
    # 默认返回一个中等的分数，具体测试用例可以覆盖它
    mock.calculate_contextual_unexpectedness.return_value = 50.0

    # 模拟其内部的 semantic_model
    mock.semantic_model = mocker.MagicMock()
    # 模拟 semantic_model 的 encode 方法
    # 返回一个固定的 numpy 数组，维度与真实模型匹配
    mock.semantic_model.encode.return_value = np.array([[0.1, 0.2, 0.3]])

    return mock


@pytest.fixture
def interrupter(mock_semantic_markov_model: MockerFixture) -> IntelligentInterrupter:
    """创建一个带有模拟依赖的 IntelligentInterrupter 实例."""
    return IntelligentInterrupter(
        speaker_weights={"user_A": 2.0, "user_B": 0.5, "default": 1.0},
        objective_keywords=["救命", "紧急"],
        core_importance_concepts=["项目截止日期", "服务器崩溃"],
        semantic_markov_model=mock_semantic_markov_model,
        final_threshold=90,  # 设置一个明确的阈值用于测试
    )


# 辅助函数，用于快速创建测试用的 Stimulus 对象
def create_stimulus(
    event_id: str, text: str, sender_id: str, embedding: list[float] | None = None
) -> Stimulus:
    """创建一个简化的 Stimulus 对象用于测试."""
    # 在测试中，我们主要关心这几个字段
    return Stimulus(
        event_id=event_id,
        timestamp=0,
        platform="test",
        bot_id="bot",
        text_content=text,
        sender_id=sender_id,
        embedding=embedding if embedding is not None else [0.0] * 3,  # 提供一个默认向量
    )


# (继续在 tests/common/intelligent_interrupt_system/test_intelligent_interrupter.py 文件中添加)


class TestIntelligentInterrupterLogic:
    """测试 IntelligentInterrupter 的核心决策逻辑 `should_interrupt`."""

    def test_should_interrupt_on_objective_keyword(
        self, interrupter: IntelligentInterrupter
    ) -> None:
        """场景1: 消息包含“霸道关键词”，必须中断."""
        # 准备
        stimulus = create_stimulus("event-1", "救命！我的代码出错了！", "user_A")
        context = create_stimulus("event-0", "...", "user_A")

        # 执行 & 断言
        assert interrupter.should_interrupt(stimulus, context) is True

    def test_should_not_interrupt_if_no_embedding(
        self, interrupter: IntelligentInterrupter
    ) -> None:
        """场景2: 新的刺激物没有 embedding，不能进行评估，不应中断."""
        # 准备
        stimulus = create_stimulus("event-1", "一条普通消息", "user_A", embedding=None)
        context = create_stimulus("event-0", "...", "user_A")

        # 执行 & 断言
        assert interrupter.should_interrupt(stimulus, context) is False

    def test_should_not_interrupt_if_no_context(self, interrupter: IntelligentInterrupter) -> None:
        """场景3: 没有上下文（例如会话开始），不应中断."""
        # 准备
        stimulus = create_stimulus("event-1", "有人在吗？", "user_A")

        # 执行 & 断言
        assert interrupter.should_interrupt(stimulus, None) is False

    def test_should_interrupt_high_weight_speaker_high_scores(
        self,
        interrupter: IntelligentInterrupter,
        mock_semantic_markov_model: MockerFixture,
        mocker: MockerFixture,
    ) -> None:
        """场景4: 高权重用户 + 高意外度 + 高重要性 = 中断."""
        # 准备
        # 模拟高意外度
        mock_semantic_markov_model.calculate_contextual_unexpectedness.return_value = 80.0
        # 模拟高重要性 (通过模拟 cosine_similarity)
        mocker.patch(
            "src.common.intelligent_interrupt_system.intelligent_interrupter.cosine_similarity",
            return_value=np.array([[0.95]]),  # 模拟与核心概念高度相关
        )

        stimulus = create_stimulus("event-1", "关于服务器崩溃的紧急报告", "user_A")
        context = create_stimulus("event-0", "...", "user_A")

        # 执行 & 断言
        # 基础分 = 0.4 * 80 (意外度) + 0.6 * 95 (重要性) = 32 + 57 = 89
        # 最终分 = 89 * 2.0 (权重) = 178 > 90 (阈值)
        assert interrupter.should_interrupt(stimulus, context) is True

    def test_should_interrupt_on_high_semantic_importance_regardless_of_weight(
        self,
        interrupter: IntelligentInterrupter,
        mock_semantic_markov_model: MockerFixture,
        mocker: MockerFixture,
    ) -> None:
        """只要内容核心重要性超过客观阈值(0.85)，无论用户权重多低，都必须中断."""
        # 准备
        mock_semantic_markov_model.calculate_contextual_unexpectedness.return_value = (
            10.0  # 即使意外度很低
        )
        mocker.patch(
            "src.common.intelligent_interrupt_system.intelligent_interrupter.cosine_similarity",
            return_value=np.array([[0.95]]),  # 相似度 0.95 > 阈值 0.85
        )

        stimulus = create_stimulus(
            "event-1", "关于服务器崩溃的紧急报告", "user_B"
        )  # 来自低权重用户
        context = create_stimulus("event-0", "...", "user_A")

        # 执行 & 断言
        # 即使最终加权分数很低，也应该因为超过了客观语义阈值而中断
        assert interrupter.should_interrupt(stimulus, context) is True

    def test_should_not_interrupt_low_scores(
        self,
        interrupter: IntelligentInterrupter,
        mock_semantic_markov_model: MockerFixture,
        mocker: MockerFixture,
    ) -> None:
        """场景6: 即使是高权重用户，但低意外度 + 低重要性 = 不中断."""
        # 准备
        mock_semantic_markov_model.calculate_contextual_unexpectedness.return_value = (
            10.0  # 低意外度
        )
        mocker.patch(
            "src.common.intelligent_interrupt_system.intelligent_interrupter.cosine_similarity",
            return_value=np.array([[0.1]]),  # 低重要性
        )

        stimulus = create_stimulus("event-1", "今天天气真好", "user_A")  # 来自高权重用户
        context = create_stimulus("event-0", "...", "user_A")

        # 执行 & 断言
        # 基础分 = 0.4 * 10 (意外度) + 0.6 * 10 (重要性) = 4 + 6 = 10
        # 最终分 = 10 * 2.0 (权重) = 20 < 90 (阈值)
        assert interrupter.should_interrupt(stimulus, context) is False

    def test_uses_default_weight_for_unknown_speaker(
        self,
        interrupter: IntelligentInterrupter,
        mock_semantic_markov_model: MockerFixture,
        mocker: MockerFixture,
    ) -> None:
        """场景7: 未知用户使用默认权重，分数刚好过线 = 中断."""
        # 准备
        mock_semantic_markov_model.calculate_contextual_unexpectedness.return_value = 95.0
        mocker.patch(
            "src.common.intelligent_interrupt_system.intelligent_interrupter.cosine_similarity",
            return_value=np.array([[0.9]]),
        )

        stimulus = create_stimulus("event-1", "项目截止日期提前了！", "user_C_unknown")  # 未知用户
        context = create_stimulus("event-0", "...", "user_A")

        # 执行 & 断言
        # 基础分 = 0.4 * 95 (意外度) + 0.6 * 90 (重要性) = 38 + 54 = 92
        # 最终分 = 92 * 1.0 (默认权重) = 92 > 90 (阈值)
        assert interrupter.should_interrupt(stimulus, context) is True
