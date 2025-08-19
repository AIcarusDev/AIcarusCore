# tests/core_logic/test_internal_info_builder.py

import pytest
from pytest_mock import MockerFixture
from src.core_logic.internal_info_builder import InternalInfoBuilder
from src.domain.models import Stimulus

# 标记整个模块的所有测试都需要异步环境
pytestmark = pytest.mark.asyncio


@pytest.fixture
def mock_thought_storage_service(mocker: MockerFixture) -> MockerFixture:
    """模拟 ThoughtStorageService."""
    return mocker.AsyncMock()


@pytest.fixture
def mock_session(mocker: MockerFixture) -> MockerFixture:
    """模拟一个 ChatSession 对象."""
    return mocker.MagicMock()


@pytest.fixture
def internal_info_builder(mock_thought_storage_service: MockerFixture) -> InternalInfoBuilder:
    """创建一个带有模拟依赖的 InternalInfoBuilder 实例."""
    return InternalInfoBuilder(thought_storage_service=mock_thought_storage_service)


# (继续在 tests/core_logic/test_internal_info_builder.py 文件中添加)


class TestInternalInfoBuilder:
    """测试 InternalInfoBuilder 的核心功能."""

    async def test_build_block_with_no_history(
        self,
        internal_info_builder: InternalInfoBuilder,
        mock_thought_storage_service: MockerFixture,
    ) -> None:
        """场景1: 数据库中没有历史思考记录时，应返回初始提示."""
        # 准备：让数据库返回 None
        mock_thought_storage_service.get_latest_thought_document.return_value = None

        # 执行
        result = await internal_info_builder.build_internal_info_block(is_context_switch=False)

        # 断言
        assert "你刚刚开始思考，还没有任何内部状态历史" in result

    async def test_build_block_with_completed_state_and_do_nothing(
        self,
        internal_info_builder: InternalInfoBuilder,
        mock_thought_storage_service: MockerFixture,
    ) -> None:
        """场景2.1: 上一轮思考正常完成且动作为 do_nothing 时，completed_action 应为 None."""
        # 准备
        last_thought = {
            "mood": "开心",
            "think": "一切顺利",
            "intent": "继续观察",
            "action_payload": {
                # 这里的 action 负载就是 do_nothing
                "action": {"core": {"do_nothing": {"motivation": "test"}}}
            },
        }
        mock_thought_storage_service.get_latest_thought_document.return_value = last_thought

        # 执行
        result = await internal_info_builder.build_internal_info_block(is_context_switch=False)

        # 断言
        assert '<snapshot time="T-1" status="COMPLETED">' in result
        assert "<mood>开心</mood>" in result

        # 对于 do_nothing 动作，我们期望 action 块的内容是 None
        assert "<completed_action>None</completed_action>" in result

    async def test_build_block_with_completed_state_and_real_action(
        self,
        internal_info_builder: InternalInfoBuilder,
        mock_thought_storage_service: MockerFixture,
    ) -> None:
        """场景2.2: 上一轮思考正常完成且有实际动作时，completed_action 应包含 JSON."""
        # 准备
        last_thought = {
            "mood": "行动中",
            "think": "需要搜索信息",
            "intent": "获取知识",
            "action_payload": {"action": {"core": {"web_search": {"query": "pytest"}}}},
        }
        mock_thought_storage_service.get_latest_thought_document.return_value = last_thought

        # 执行
        result = await internal_info_builder.build_internal_info_block(is_context_switch=False)

        # 断言
        assert '<snapshot time="T-1" status="COMPLETED">' in result
        assert "<mood>行动中</mood>" in result
        # 这个断言现在是正确的，因为它对应一个真实动作
        assert (
            '<completed_action><![CDATA[{"core": {"web_search": {"query": "pytest"}}}]]></completed_action>'  # noqa: E501
            in result
        )

    async def test_build_block_with_interrupted_state(
        self,
        internal_info_builder: InternalInfoBuilder,
        mock_thought_storage_service: MockerFixture,
        mock_session: MockerFixture,
    ) -> None:
        """场景3: 上一轮思考被中断时，应生成 INTERRUPTED 状态并包含中断信息."""
        # 准备：构造“上一个思考”和“中断上下文”
        last_thought = {
            "mood": "专注",
            "think": "正在处理任务A",
            "intent": None,
            "action_payload": {},
        }
        mock_thought_storage_service.get_latest_thought_document.return_value = last_thought

        interrupting_stimulus = Stimulus(
            event_id="evt-123",
            timestamp=0,
            platform="test",
            bot_id="bot",
            text_content="<紧急消息&>",  # 测试 XML 转义
            sender_id="user_abc",
        )
        mock_session.interruption_context = {"interrupting_stimulus": interrupting_stimulus}

        # 准备 user_map 用于替换 Uid
        user_map = {"user_abc": {"uid_str": "U1"}}

        # 执行
        result = await internal_info_builder.build_internal_info_block(
            is_context_switch=False, session=mock_session, user_map_from_prompt_builder=user_map
        )

        # 断言
        assert '<snapshot time="T-1" status="INTERRUPTED">' in result
        assert "<mood>专注</mood>" in result
        assert "<interruption>" in result
        assert "<source>user U1</source>" in result
        assert "<content>&lt;紧急消息&amp;&gt;</content>" in result  # 验证 XML 文本被正确转义

        # 验证副作用：中断上下文在被使用后应该被清空
        assert mock_session.interruption_context is None
