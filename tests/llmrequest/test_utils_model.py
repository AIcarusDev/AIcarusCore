# tests/llmrequest/test_utils_model.py (最终完美版)

import asyncio
from collections.abc import AsyncGenerator
from unittest.mock import MagicMock, AsyncMock, patch

import pytest
from pytest_mock import MockerFixture

import aiohttp
from aiohttp import ClientConnectorError

from src.llmrequest.utils_model import (
    LLMClient,
    APIKeyError,
    RateLimitError,
    PermissionDeniedError,
    PayloadTooLargeError,
    LLMClientError,
)

# --- [FIX] 移除模块级的 pytestmark ---


# --- Fixtures: 模拟环境和依赖 ---

@pytest.fixture
def mock_env(monkeypatch):
    """一个用于模拟环境变量的 fixture。"""
    monkeypatch.setenv("TEST_PROVIDER_API_KEYS", '["key1", "key2", "key3_abandoned"]')
    monkeypatch.setenv("TEST_PROVIDER_BASE_URL", "http://fakeapi.com/v1")
    monkeypatch.setenv("PROXY_HOST", "localhost")
    monkeypatch.setenv("PROXY_PORT", "7890")
    yield
    monkeypatch.delenv("TEST_PROVIDER_API_KEYS", raising=False)
    monkeypatch.delenv("TEST_PROVIDER_BASE_URL", raising=False)
    monkeypatch.delenv("PROXY_HOST", raising=False)
    monkeypatch.delenv("PROXY_PORT", raising=False)


@pytest.fixture
def mock_aiohttp_post(mocker: MockerFixture) -> MagicMock:
    """一个核心的 fixture，正确地模拟 aiohttp.ClientSession.post。"""
    return mocker.patch("aiohttp.ClientSession.post", new_callable=MagicMock)


class AsyncContextManager:
    """一个通用的异步上下文管理器模拟器。"""
    def __init__(self, mock_obj):
        self.mock_obj = mock_obj

    async def __aenter__(self):
        return self.mock_obj

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass


def create_mock_response(status_code: int, json_data: dict = None, text_data: str = ""):
    """辅助函数，用于创建模拟的 aiohttp 响应对象。"""
    mock_response = MagicMock(spec=aiohttp.ClientResponse)
    mock_response.status = status_code
    mock_response.json = AsyncMock(return_value=json_data or {})
    mock_response.text = AsyncMock(return_value=text_data or str(json_data))
    mock_response.url = "http://fakeapi.com/v1/test_endpoint"
    mock_response.content = MagicMock()
    mock_response.content.__aiter__.return_value = iter([])
    return AsyncContextManager(mock_response)


# --- 测试用例 ---


@pytest.mark.usefixtures("mock_env")
class TestLLMClientInitialization:
    """测试 LLMClient 的初始化逻辑 (同步测试)。"""

    def test_initialization_success(self):
        client = LLMClient(
            model={"provider": "test_provider", "name": "test_model"},
            abandoned_keys_config=["key3_abandoned"],
        )
        assert client.provider == "TEST_PROVIDER"
        assert client.model_name == "test_model"
        assert "key1" in client.api_keys_config

    def test_initialization_no_keys_fails(self, monkeypatch):
        monkeypatch.delenv("TEST_PROVIDER_API_KEYS", raising=False)
        with pytest.raises(APIKeyError):
            LLMClient(model={"provider": "test_provider", "name": "test_model"})

    def test_initialization_determines_openai_style(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEYS", '["dummy_key"]')
        monkeypatch.setenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
        client = LLMClient(model={"provider": "openai", "name": "gpt-4"})
        assert client.api_endpoint_style == "openai"

    def test_initialization_determines_google_style(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEYS", '["dummy_key"]')
        monkeypatch.setenv("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/models")
        client = LLMClient(model={"provider": "gemini", "name": "gemini-pro"})
        assert client.api_endpoint_style == "google"


@pytest.mark.usefixtures("mock_env")
@pytest.mark.asyncio  # --- [FIX] 只为这个需要异步的测试类添加装饰器 ---
class TestLLMClientRequestExecution:
    """测试核心的请求执行和重试逻辑 (异步测试)。"""

    @pytest.fixture
    async def client(self) -> AsyncGenerator[LLMClient, None]:
        """
        [FIX] 提供一个配置好的客户端实例，并在测试后自动关闭 session。
        """
        c = LLMClient(
            model={"provider": "test_provider", "name": "test_model"},
            abandoned_keys_config=[],
        )
        yield c
        await c.close() # 测试结束后自动清理

    async def test_successful_first_attempt(self, client: LLMClient, mock_aiohttp_post: MagicMock):
        mock_aiohttp_post.return_value = create_mock_response(
            200, {"choices": [{"message": {"content": "Success"}}]}
        )
        client.provider = "OPENAI"
        client.api_endpoint_style = "openai"
        result = await client.make_request(prompt="test", system_prompt=None, is_stream=False)
        assert not result.get("error")
        assert result["text"] == "Success"
        mock_aiohttp_post.assert_called_once()

    async def test_retry_on_network_error(self, client: LLMClient, mock_aiohttp_post: MagicMock):
        mock_aiohttp_post.side_effect = [
            ClientConnectorError(MagicMock(), MagicMock()),
            create_mock_response(200, {"candidates": [{"content": {"parts": [{"text": "OK"}]}}]}),
        ]
        client.provider = "GEMINI"
        client.api_endpoint_style = "google"
        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await client._execute_request_with_retries(
                request_type="chat", is_streaming=False, prompt="test", max_retries=1
            )
        assert not result.get("error")
        assert result["text"] == "OK"
        assert mock_aiohttp_post.call_count == 2

    async def test_switch_key_on_rate_limit(self, client: LLMClient, mock_aiohttp_post: MagicMock):
        client.api_keys_config = ["key1", "key2"]
        with patch("random.shuffle", lambda x: x):
            mock_aiohttp_post.side_effect = [
                create_mock_response(429, text_data="Rate limited"),
                create_mock_response(200, {"choices": [{"message": {"content": "Success with key2"}}]}),
            ]
            client.provider = "OPENAI"
            client.api_endpoint_style = "openai"
            result = await client._execute_request_with_retries(
                request_type="chat", is_streaming=False, prompt="test", max_retries=1
            )
            assert not result.get("error")
            assert result["text"] == "Success with key2"
            assert "key1" in client._temporarily_disabled_keys_429

    async def test_abandon_key_on_permission_denied(self, client: LLMClient, mock_aiohttp_post: MagicMock):
        client.api_keys_config = ["key1", "key2"]
        with patch("random.shuffle", lambda x: x):
            mock_aiohttp_post.side_effect = [
                create_mock_response(403, text_data="Invalid key"),
                create_mock_response(200, {"choices": [{"message": {"content": "Success with key2"}}]}),
            ]
            client.provider = "OPENAI"
            client.api_endpoint_style = "openai"
            result = await client._execute_request_with_retries(
                request_type="chat", is_streaming=False, prompt="test", max_retries=1
            )
            assert not result.get("error")
            assert "key1" in client._abandoned_keys_runtime
            assert "key1" not in client._temporarily_disabled_keys_429

    async def test_all_retries_fail_returns_error_dict(self, client: LLMClient, mock_aiohttp_post: MagicMock):
        client.api_keys_config = ["key1", "key2"]
        mock_aiohttp_post.side_effect = [
            create_mock_response(429, text_data="Rate limited key1"),
            create_mock_response(429, text_data="Rate limited key2"),
            ClientConnectorError(MagicMock(), MagicMock()),
        ]
        client.provider = "OPENAI"
        client.api_endpoint_style = "openai"
        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await client._execute_request_with_retries(
                request_type="chat", is_streaming=False, prompt="test", max_retries=1
            )
        assert result.get("error") is True
        assert result.get("type") == "RateLimitError"
        assert "所有API请求尝试均失败" in result.get("message", "")
        assert mock_aiohttp_post.call_count == 2

    async def test_interrupt_event_stops_execution(self, client: LLMClient):
        interruption_event = asyncio.Event()
        interruption_event.set()

        result = await client._execute_request_with_retries(
            request_type="chat",
            is_streaming=False,
            prompt="test",
            max_retries=3,
            interruption_event=interruption_event,
        )

        assert result.get("interrupted") is True
        assert result.get("finish_reason") == "INTERRUPTED_BEFORE_CALL"

    async def test_payload_too_large_triggers_compression(
        self, client: LLMClient, mock_aiohttp_post: MagicMock, mocker: MockerFixture
    ):
        mock_compressor = mocker.patch(
            "src.llmrequest.utils_model.LLMClient._compress_base64_image",
            new_callable=AsyncMock,
            return_value=("compressed_base64_data", "image/jpeg"),
        )
        mock_aiohttp_post.side_effect = [
            create_mock_response(413, text_data="Payload too large"),
            create_mock_response(200, {"choices": [{"message": {"content": "Success after compress"}}]}),
        ]
        client.provider = "OPENAI"
        client.api_endpoint_style = "openai"

        result = await client._execute_request_with_retries(
            request_type="vision",
            is_streaming=False,
            prompt="describe",
            image_inputs=["data:image/png;base64,large_data"],
            max_retries=1,
        )

        assert not result.get("error")
        assert result["text"] == "Success after compress"
        mock_compressor.assert_awaited_once()
        assert mock_aiohttp_post.call_count == 2