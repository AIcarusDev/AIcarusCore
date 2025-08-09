# tests/core_logic/test_thought_generator.py

import pytest
from pytest_mock import MockerFixture
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


@pytest.mark.asyncio
async def test_generate_thought_llm_error(mocker: MockerFixture) -> None:
    """测试当 LLM 客户端返回错误时，generate_thought 返回 None."""
    # 1. 准备一个模拟的 LLM 客户端
    mock_llm_client = mocker.Mock()

    # 2. 定义一个包含错误的假返回数据
    fake_llm_response = {"error": "Simulated API Error", "text": None}

    # 3. Patch make_llm_request 方法
    mock_llm_client.make_llm_request = mocker.AsyncMock(return_value=fake_llm_response)
    thought_generator = ThoughtGenerator(llm_client=mock_llm_client)
    result = await thought_generator.generate_thought(
        system_prompt="sys", user_prompt="user", image_inputs=[], response_schema={}
    )
    assert result is None
    mock_llm_client.make_llm_request.assert_awaited_once()


@pytest.mark.asyncio
async def test_generate_thought_invalid_internal_state_structure(mocker: MockerFixture) -> None:
    """测试当 internal_state 的值不是一个字典时，generate_thought 返回 None."""
    mock_llm_client = mocker.Mock()
    # 这个JSON在语法上是正确的，但在结构上是错误的
    fake_llm_response = {
        "error": None,
        "text": """
        ```json
        {
            "internal_state": "this is not a dict"
        }
        ```
        """,
    }
    mock_llm_client.make_llm_request = mocker.AsyncMock(return_value=fake_llm_response)

    thought_generator = ThoughtGenerator(llm_client=mock_llm_client)

    result = await thought_generator.generate_thought(
        system_prompt="sys", user_prompt="user", image_inputs=[], response_schema={}
    )

    # 因为我们加强了验证，现在这里应该断言为 None
    assert result is None
    mock_llm_client.make_llm_request.assert_awaited_once()


@pytest.mark.asyncio
async def test_generate_thought_truly_malformed_json(mocker: MockerFixture) -> None:
    """测试当 LLM 返回语法错误的 JSON 时，generate_thought 返回 None."""
    mock_llm_client = mocker.Mock()
    # 这个JSON在语法上就是错误的（缺少逗号）
    fake_llm_response = {
        "error": None,
        "text": """
        ```json
        {
            "internal_state": {"mood": "happy"}
            "action": {}
        }
        ```
        """,
    }
    mock_llm_client.make_llm_request = mocker.AsyncMock(return_value=fake_llm_response)
    thought_generator = ThoughtGenerator(llm_client=mock_llm_client)

    result = await thought_generator.generate_thought(
        system_prompt="sys", user_prompt="user", image_inputs=[], response_schema={}
    )

    assert result is None
    mock_llm_client.make_llm_request.assert_awaited_once()


@pytest.mark.asyncio
async def test_generate_thought_unexpected_structure(mocker: MockerFixture) -> None:
    """测试当 LLM 返回的 JSON 结构缺少 internal_state 键时，generate_thought 返回 None."""
    mock_llm_client = mocker.Mock()
    fake_llm_response = {
        "error": None,
        "text": """
        ```json
        {
            "a_different_key": "some value",
            "another_key": {}
        }
        ```
        """,
    }
    mock_llm_client.make_llm_request = mocker.AsyncMock(return_value=fake_llm_response)
    thought_generator = ThoughtGenerator(llm_client=mock_llm_client)

    result = await thought_generator.generate_thought(
        system_prompt="sys", user_prompt="user", image_inputs=[], response_schema={}
    )

    assert result is None
    mock_llm_client.make_llm_request.assert_awaited_once()
