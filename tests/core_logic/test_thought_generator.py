import pytest
from pytest_mock import MockerFixture  # <-- 修正点 1
from src.core_logic.thought_generator import ThoughtGenerator


@pytest.mark.asyncio
async def test_generate_thought_success(mocker: MockerFixture) -> None:
    """测试 ThoughtGenerator 在 LLM 客户端成功返回时能否正确解析."""
    # 1. 准备一个假的、模拟的 LLM 客户端
    # 我们不需要一个完整的客户端实例，只需要一个能被 patch 的对象
    mock_llm_client = mocker.Mock()

    # 2. 定义 LLM API 的假返回数据
    fake_llm_response = {
        "error": None,
        "text": """
        ```json
        {
            "internal_state": {
                "mood": "好奇",
                "think": "看起来一切正常，我应该做什么呢？",
                "goal": "检查系统状态"
            },
            "action": {
                "core": {
                    "do_nothing": {
                        "motivation": "暂时没有需要处理的事情。"
                    }
                }
            }
        }
        ```
        """,
    }

    # 3. 使用 mocker 来 "patch" (替换) LLM 客户端的 make_llm_request 方法
    # 当它被调用时，我们让它返回预设的假数据
    # 注意：因为 make_llm_request 是一个 async 方法，我们需要使用 AsyncMock
    mock_llm_client.make_llm_request = mocker.AsyncMock(return_value=fake_llm_response)

    # 4. 创建被测试的 ThoughtGenerator 实例，并传入我们模拟的客户端
    thought_generator = ThoughtGenerator(llm_client=mock_llm_client)

    # 5. 调用被测试的方法
    result = await thought_generator.generate_thought(
        system_prompt="sys", user_prompt="user", image_inputs=[], response_schema={}
    )

    # 6. 断言结果是否符合预期
    assert result is not None
    assert result["internal_state"]["mood"] == "好奇"
    assert "do_nothing" in result["action"]["core"]

    # 7. 验证我们的模拟方法是否确实被调用了
    mock_llm_client.make_llm_request.assert_awaited_once()
