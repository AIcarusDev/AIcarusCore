# tests/llmrequest/test_llm_client.py (最终完美版)

import os
from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock

import pytest
from pytest import MonkeyPatch
from pytest_mock import MockerFixture
from src.services.llmrequest.core.models import (
    APIKeyError,
)
from src.services.llmrequest.llm_client import LLMClient

# --- Fixtures ---


@pytest.fixture
def mock_env(monkeypatch: MonkeyPatch) -> Generator[None, None, None]:
    """一个用于模拟环境变量的 fixture."""
    monkeypatch.setenv("TEST_PROVIDER_API_KEYS", '["key1", "key2", "key3"]')
    monkeypatch.setenv("TEST_PROVIDER_BASE_URL", "http://fakeapi.com/v1")
    monkeypatch.setenv("PROXY_HOST", "localhost")
    monkeypatch.setenv("PROXY_PORT", "7890")
    yield


# --- 测试用例 ---


@pytest.mark.usefixtures("mock_env")
class TestLLMClientInitialization:
    """测试 LLMClient 初始化和组件组装."""

    def test_initialization_success(self) -> None:
        """测试正常初始化，并验证内部组件被正确创建."""
        client = LLMClient(
            model={"provider": "test_provider", "name": "test_model"},
            proxy_host=os.getenv("PROXY_HOST"),
            proxy_port=int(os.getenv("PROXY_PORT")),
        )
        assert client.provider == "TEST_PROVIDER"
        assert client.model_name == "test_model"
        assert client.base_url == "http://fakeapi.com/v1"
        assert client.proxy_url == "http://localhost:7890"  # 验证代理被正确设置
        assert client.request_executor is not None
        assert client.media_processor is not None
        assert client.handler is not None

    def test_initialization_no_keys_fails(self, monkeypatch: MonkeyPatch) -> None:
        """测试缺少 API 密钥时初始化失败."""
        monkeypatch.delenv("TEST_PROVIDER_API_KEYS", raising=False)
        with pytest.raises(APIKeyError):
            LLMClient(model={"provider": "test_provider", "name": "test_model"})

    def test_initialization_determines_openai_style(self, monkeypatch: MonkeyPatch) -> None:
        """测试能否根据 provider 名称正确判断为 openai 风格."""
        monkeypatch.setenv("OPENAI_API_KEYS", '["dummy_key"]')
        monkeypatch.setenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
        client = LLMClient(model={"provider": "openai", "name": "gpt-4"})
        assert client.handler.__class__.__name__ == "OpenAIApiHandler"

    def test_initialization_determines_google_style(self, monkeypatch: MonkeyPatch) -> None:
        """测试能否根据 provider 名称正确判断为 google 风格."""
        monkeypatch.setenv("GEMINI_API_KEYS", '["dummy_key"]')
        monkeypatch.setenv(
            "GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/models"
        )
        client = LLMClient(model={"provider": "gemini", "name": "gemini-pro"})
        assert client.handler.__class__.__name__ == "GoogleApiHandler"


@pytest.mark.asyncio
@pytest.mark.usefixtures("mock_env")
class TestLLMClientRequestFlow:
    """测试 LLMClient 的请求流程，重点在于它如何正确地委托给内部组件."""

    @pytest.fixture
    async def client(self) -> AsyncGenerator[LLMClient, None]:
        """提供一个配置好的客户端实例，并在测试后自动关闭 session.

        现在它会从环境变量中读取代理信息并传入构造函数。
        """
        c = LLMClient(
            model={"provider": "test_provider", "name": "test_model"},
            proxy_host=os.getenv("PROXY_HOST"),
            proxy_port=int(os.getenv("PROXY_PORT")),
        )
        yield c
        await c.close()

    async def test_make_request_delegates_to_executor(
        self, client: LLMClient, mocker: MockerFixture
    ) -> None:
        """测试 make_request 是否正确地调用了 RequestExecutor."""
        mock_executor_execute = mocker.patch(
            "src.llmrequest.core.request_executor.RequestExecutor.execute_request",
            new_callable=AsyncMock,
            return_value={"text": "Success from executor"},
        )

        result = await client.make_request(prompt="test", system_prompt=None, is_stream=False)

        assert result["text"] == "Success from executor"
        mock_executor_execute.assert_awaited_once()
        call_args, call_kwargs = mock_executor_execute.call_args
        assert call_kwargs.get("request_type") == "chat"
        assert call_kwargs.get("prompt") == "test"

    async def test_make_request_with_images_calls_media_processor(
        self, client: LLMClient, mocker: MockerFixture
    ) -> None:
        """测试带有图片的请求是否会先调用 MediaProcessor."""
        mock_media_processor = mocker.patch(
            "src.llmrequest.core.media_processor.MediaProcessor.process_media_inputs",
            new_callable=AsyncMock,
            return_value=[{"b64_data": "processed_data", "mime_type": "image/jpeg"}],
        )
        mock_executor_execute = mocker.patch(
            "src.llmrequest.core.request_executor.RequestExecutor.execute_request",
            new_callable=AsyncMock,
            return_value={"text": "Success with image"},
        )

        await client.make_request(
            prompt="describe this",
            system_prompt=None,
            is_stream=False,
            is_multimodal=True,
            image_inputs=["fake_image_data"],
        )

        # 验证 MediaProcessor 被调用，这次 proxy_url 应该是正确的
        mock_media_processor.assert_awaited_once_with(
            ["fake_image_data"], None, "http://localhost:7890"
        )

        call_args, call_kwargs = mock_executor_execute.call_args
        assert call_kwargs.get("processed_images") == [
            {"b64_data": "processed_data", "mime_type": "image/jpeg"}
        ]
