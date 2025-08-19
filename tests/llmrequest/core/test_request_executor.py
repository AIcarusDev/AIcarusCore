# tests/llmrequest/core/test_request_executor.py (完整替换此文件)

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_mock import MockerFixture
from src.llmrequest.core.models import (
    APIKeyManager,
    NetworkError,
    PermissionDeniedError,
    RateLimitError,
)
from src.llmrequest.core.provider.base import ApiProviderHandler
from src.llmrequest.core.request_executor import RequestExecutor

pytestmark = pytest.mark.asyncio


@pytest.fixture
def mock_key_manager(mocker: MockerFixture) -> MagicMock:
    """模拟 APIKeyManager."""
    manager = mocker.MagicMock(spec=APIKeyManager)
    manager.get_available_keys.side_effect = [["key1", "key2"], ["key2"], []]
    manager.temporarily_disable_key = mocker.Mock()
    manager.permanently_abandon_key = mocker.Mock()
    return manager


@pytest.fixture
def mock_handler(mocker: MockerFixture) -> MagicMock:
    """模拟 ApiProviderHandler."""
    handler = mocker.MagicMock(spec=ApiProviderHandler)
    handler.prepare_request_data.return_value = (
        "/test-path",
        {"param1": "value1"},
        {"Content-Type": "application/json"},
        {"data": "test"},
    )
    handler.parse_non_streaming_response.return_value = {"text": "parsed_success"}
    return handler


@pytest.fixture
def executor(mock_key_manager: MagicMock) -> RequestExecutor:
    """创建一个带有模拟依赖的 RequestExecutor 实例."""
    return RequestExecutor(key_manager=mock_key_manager, base_url="http://fake.api", proxy_url=None)


class TestRequestExecutor:
    """为 RequestExecutor 编写的单元测试."""

    async def test_successful_first_attempt(
        self, executor: RequestExecutor, mock_handler: MagicMock, mocker: MockerFixture
    ) -> None:
        """测试：第一次尝试就成功."""
        mock_api_call = mocker.patch.object(
            executor, "_make_api_call", new_callable=AsyncMock, return_value={"text": "Success"}
        )

        result = await executor.execute_request(
            handler=mock_handler,
            model_name="test",
            request_type="chat",
            is_streaming=False,
            prompt="test",
            system_prompt=None,
            processed_images=None,
            generation_params={},
            tools=None,
            tool_choice=None,
            text_to_embed=None,
            max_retries=1,
            interruption_event=None,
            stream_chunk_delay=0.0,
            enable_google_search=False,
        )

        assert result["text"] == "Success"
        mock_api_call.assert_awaited_once()
        args, _ = mock_api_call.call_args
        assert args[1] == "key1"
        assert args[2] == "/test-path"

    async def test_switches_key_on_rate_limit(
        self, executor: RequestExecutor, mock_handler: MagicMock, mocker: MockerFixture
    ) -> None:
        """测试：遇到 RateLimitError 后切换密钥并成功."""
        mock_api_call = mocker.patch.object(
            executor,
            "_make_api_call",
            new_callable=AsyncMock,
            side_effect=[
                RateLimitError("limit", key_identifier="key1"),
                {"text": "Success with key2"},
            ],
        )

        result = await executor.execute_request(
            handler=mock_handler,
            model_name="test",
            request_type="chat",
            is_streaming=False,
            prompt="test",
            system_prompt=None,
            processed_images=None,
            generation_params={},
            tools=None,
            tool_choice=None,
            text_to_embed=None,
            max_retries=1,
            interruption_event=None,
            stream_chunk_delay=0.0,
            enable_google_search=False,
        )

        assert result["text"] == "Success with key2"
        assert mock_api_call.await_count == 2
        executor.key_manager.temporarily_disable_key.assert_called_once_with("key1")

    async def test_abandons_key_on_permission_denied(
        self, executor: RequestExecutor, mock_handler: MagicMock, mocker: MockerFixture
    ) -> None:
        """测试：遇到 PermissionDeniedError 后永久弃用密钥."""
        mock_api_call = mocker.patch.object(
            executor,
            "_make_api_call",
            new_callable=AsyncMock,
            side_effect=[
                PermissionDeniedError("denied", status_code=403, key_identifier="key1"),
                {"text": "Success with key2"},
            ],
        )

        result = await executor.execute_request(
            handler=mock_handler,
            model_name="test",
            request_type="chat",
            is_streaming=False,
            prompt="test",
            system_prompt=None,
            processed_images=None,
            generation_params={},
            tools=None,
            tool_choice=None,
            text_to_embed=None,
            max_retries=1,
            interruption_event=None,
            stream_chunk_delay=0.0,
            enable_google_search=False,
        )

        assert result["text"] == "Success with key2"
        assert mock_api_call.await_count == 2
        executor.key_manager.permanently_abandon_key.assert_called_once_with("key1")

    async def test_all_retries_fail_returns_error_dict(
        self, executor: RequestExecutor, mock_handler: MagicMock, mocker: MockerFixture
    ) -> None:
        """测试：所有重试均失败后返回结构化错误."""
        executor.key_manager.get_available_keys.side_effect = [["key1", "key2"], ["key2"], []]
        mock_api_call = mocker.patch.object(
            executor,
            "_make_api_call",
            new_callable=AsyncMock,
            side_effect=[
                RateLimitError("limit", key_identifier="key1"),
                NetworkError("network issue"),
                NetworkError("network issue again"),
            ],
        )

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await executor.execute_request(
                handler=mock_handler,
                model_name="test",
                request_type="chat",
                is_streaming=False,
                prompt="test",
                system_prompt=None,
                processed_images=None,
                generation_params={},
                tools=None,
                tool_choice=None,
                text_to_embed=None,
                max_retries=1,
                interruption_event=None,
                stream_chunk_delay=0.0,
                enable_google_search=False,
            )

        assert result["error"] is True
        assert result["type"] == "NetworkError"
        assert "所有API请求尝试均失败" in result["message"]
        assert mock_api_call.await_count == 3
