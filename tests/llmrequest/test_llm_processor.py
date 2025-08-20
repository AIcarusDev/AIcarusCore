# tests/llmrequest/test_llm_processor.py

import pytest
from pytest_mock import MockerFixture

# 导入我们需要测试的目标类
from src.llmrequest.llm_processor import Client as ProcessorClient

# 标记此文件中所有测试都为异步
pytestmark = pytest.mark.asyncio


@pytest.fixture
def mock_config(mocker: MockerFixture) -> MockerFixture:
    """
    一个用于模拟全局 config 对象的 fixture。
    通过 mocker.patch，我们可以精确控制测试期间 config 的行为。
    """
    # 我们 patch llm_processor 模块中导入的 config 对象
    mock = mocker.patch("src.llmrequest.llm_processor.config")
    return mock


@pytest.fixture
def mock_underlying_llm_client_class(mocker: MockerFixture) -> MockerFixture:
    """
    一个关键的 fixture，它模拟了整个 UnderlyingLLMClient *类*。
    这使我们能够：
    1. 控制由这个类创建的所有实例的行为。
    2. 检查这个类的构造函数(__init__)被调用了多少次，以及用了什么参数。
    """
    # patch llm_processor 模块中导入的 UnderlyingLLMClient 类
    mock_class = mocker.patch("src.llmrequest.llm_processor.UnderlyingLLMClient")

    # 当这个模拟类被实例化时 (e.g., UnderlyingLLMClient(...))，
    # 我们让它返回一个 mock 实例。
    mock_instance = mock_class.return_value

    # --- [核心修改] ---
    # 将 mock 的方法名从 'make_llm_request' 更正为 'make_request'
    mock_instance.make_request = mocker.AsyncMock()
    # --- [修改结束] ---

    # 返回这个类的 mock，以便我们可以检查它的调用情况
    return mock_class


@pytest.fixture
def processor_client(
    mock_underlying_llm_client_class: MockerFixture,
) -> ProcessorClient:
    """
    创建一个被测对象 ProcessorClient 的实例。
    它的依赖 UnderlyingLLMClient 已经被我们的 mock_underlying_llm_client_class fixture 替换掉了。
    """
    return ProcessorClient(
        model={"provider": "test_provider", "name": "test-main-model"}
    )


class TestLLMProcessorClientFallback:
    """专门测试模型升避（Fallback）功能的测试类。"""

    async def test_fallback_is_triggered_on_empty_response(
        self,
        processor_client: ProcessorClient,
        mock_config: MockerFixture,
        mock_underlying_llm_client_class: MockerFixture,
    ) -> None:
        """
        测试核心场景：当主模型返回空文本且配置了备用模型时，应触发升避逻辑。
        """
        # 1. 准备 (Arrange)
        mock_config.test_function.fallback_model_name = "gpt-4o-fallback"
        mock_instance = mock_underlying_llm_client_class.return_value

        # --- [核心修改] ---
        # 配置 'make_request' 的行为
        mock_instance.make_request.side_effect = [
            {"text": "  ", "error": None},
            {"text": "Fallback success!", "error": None},
        ]
        # --- [修改结束] ---

        # 2. 执行 (Act)
        result = await processor_client.make_llm_request(
            prompt="test", is_stream=False
        )

        # 3. 断言 (Assert)
        assert result["text"] == "Fallback success!"
        assert mock_underlying_llm_client_class.call_count == 2
        
        # --- [核心修改] ---
        # 验证 'make_request' 被调用了两次
        assert mock_instance.make_request.call_count == 2
        # --- [修改结束] ---

        primary_call_args = mock_underlying_llm_client_class.call_args_list[0]
        fallback_call_args = mock_underlying_llm_client_class.call_args_list[1]

        assert primary_call_args.kwargs["model"]["name"] == "test-main-model"
        assert fallback_call_args.kwargs["model"]["name"] == "gpt-4o-fallback"

    async def test_fallback_not_triggered_if_no_fallback_model_configured(
        self,
        processor_client: ProcessorClient,
        mock_config: MockerFixture,
        mock_underlying_llm_client_class: MockerFixture,
    ) -> None:
        """
        测试场景：即使主模型返回空文本，但如果没有配置备用模型，则不应触发升避。
        """
        mock_config.test_function.fallback_model_name = ""
        mock_instance = mock_underlying_llm_client_class.return_value
        
        # --- [核心修改] ---
        # 配置 'make_request' 的返回值
        mock_instance.make_request.return_value = {"text": "  ", "error": None}
        # --- [修改结束] ---

        result = await processor_client.make_llm_request(
            prompt="test", is_stream=False
        )

        assert result["text"].strip() == ""
        mock_underlying_llm_client_class.assert_called_once()
        
        # --- [核心修改] ---
        # 验证 'make_request' 被调用了一次
        mock_instance.make_request.assert_awaited_once()
        # --- [修改结束] ---

    async def test_fallback_not_triggered_if_primary_model_succeeds_with_text(
        self,
        processor_client: ProcessorClient,
        mock_config: MockerFixture,
        mock_underlying_llm_client_class: MockerFixture,
    ) -> None:
        """
        测试场景：如果主模型成功返回了非空文本，则不应触发升避。
        """
        mock_config.test_function.fallback_model_name = "gpt-4o-fallback"
        mock_instance = mock_underlying_llm_client_class.return_value
        
        # --- [核心修改] ---
        # 配置 'make_request' 的返回值
        mock_instance.make_request.return_value = {
            "text": "Primary success!",
            "error": None,
        }
        # --- [修改结束] ---

        result = await processor_client.make_llm_request(
            prompt="test", is_stream=False
        )

        assert result["text"] == "Primary success!"
        mock_underlying_llm_client_class.assert_called_once()
        
        # --- [核心修改] ---
        # 验证 'make_request' 被调用了一次
        mock_instance.make_request.assert_awaited_once()
        # --- [修改结束] ---

    async def test_fallback_not_triggered_on_api_error(
        self,
        processor_client: ProcessorClient,
        mock_config: MockerFixture,
        mock_underlying_llm_client_class: MockerFixture,
    ) -> None:
        """
        测试场景：如果主模型调用时发生API错误，则不应触发升避，应直接返回错误。
        """
        mock_config.test_function.fallback_model_name = "gpt-4o-fallback"
        mock_instance = mock_underlying_llm_client_class.return_value
        
        # --- [核心修改] ---
        # 配置 'make_request' 的返回值
        mock_instance.make_request.return_value = {
            "text": None,
            "error": True,
            "message": "API key invalid",
        }
        # --- [修改结束] ---

        result = await processor_client.make_llm_request(
            prompt="test", is_stream=False
        )

        assert result["error"] is True
        assert result["message"] == "API key invalid"
        mock_underlying_llm_client_class.assert_called_once()
        
        # --- [核心修改] ---
        # 验证 'make_request' 被调用了一次
        mock_instance.make_request.assert_awaited_once()
        # --- [修改结束] ---