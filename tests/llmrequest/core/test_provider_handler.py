# tests/llmrequest/core/test_provider_handler.py

import re

import pytest
from src.llmrequest.core.provider_handler import GoogleApiHandler, OpenAIApiHandler

# --- Fixtures ---


@pytest.fixture
def image_placeholder_pattern() -> re.Pattern:
    """提供一个编译好的正则表达式模式."""
    return re.compile(r"\[(图片|动画表情|GIF)_(\d+)]")


@pytest.fixture
def google_handler(image_placeholder_pattern: re.Pattern) -> GoogleApiHandler:
    """提供一个 GoogleApiHandler 实例."""
    return GoogleApiHandler(image_placeholder_pattern)


@pytest.fixture
def openai_handler(image_placeholder_pattern: re.Pattern) -> OpenAIApiHandler:
    """提供一个 OpenAIApiHandler 实例."""
    return OpenAIApiHandler(image_placeholder_pattern)


# --- Test Cases ---


class TestGoogleApiHandler:
    """测试 GoogleApiHandler 的请求准备逻辑."""

    def test_prepare_chat_request_path(self, google_handler: GoogleApiHandler) -> None:
        """验证为 Google chat 请求生成的路径是否正确."""
        path, _, _, _ = google_handler.prepare_request_data(
            model_name="gemini-1.5-pro",
            request_type="chat",
            is_streaming=False,
            prompt="hello",
            system_prompt=None,
            processed_images=None,
            final_generation_config={},
            tools=None,
            tool_choice=None,
            text_to_embed=None,
            enable_google_search=False,
        )
        assert path == "/models/gemini-1.5-pro:generateContent"

    def test_prepare_embedding_request_path(self, google_handler: GoogleApiHandler) -> None:
        """验证为 Google embedding 请求生成的路径是否正确."""
        path, _, _, _ = google_handler.prepare_request_data(
            model_name="text-embedding-004",
            request_type="embedding",
            is_streaming=False,
            prompt=None,
            system_prompt=None,
            processed_images=None,
            final_generation_config={},
            tools=None,
            tool_choice=None,
            text_to_embed="some text",
            enable_google_search=False,
        )
        assert path == "/models/text-embedding-004:embedContent"

    def test_prepare_request_with_system_prompt(self, google_handler: GoogleApiHandler) -> None:
        """验证系统提示是否被正确地放入 payload."""
        _, _, _, payload = google_handler.prepare_request_data(
            model_name="gemini-1.5-pro",
            request_type="chat",
            is_streaming=False,
            prompt="hello",
            system_prompt="You are a helpful assistant.",
            processed_images=None,
            final_generation_config={},
            tools=None,
            tool_choice=None,
            text_to_embed=None,
            enable_google_search=False,
        )
        assert "system_instruction" in payload
        assert payload["system_instruction"]["parts"][0]["text"] == "You are a helpful assistant."


class TestOpenAIApiHandler:
    """测试 OpenAIApiHandler 的请求准备逻辑."""

    def test_prepare_chat_request_path(self, openai_handler: OpenAIApiHandler) -> None:
        """验证为 OpenAI chat 请求生成的路径是否正确."""
        path, _, _, _ = openai_handler.prepare_request_data(
            model_name="gpt-4o",
            request_type="chat",
            is_streaming=False,
            prompt="hello",
            system_prompt=None,
            processed_images=None,
            final_generation_config={},
            tools=None,
            tool_choice=None,
            text_to_embed=None,
            enable_google_search=False,
        )
        assert path == "/chat/completions"

    def test_prepare_request_with_tools(self, openai_handler: OpenAIApiHandler) -> None:
        """验证工具是否被正确地放入 payload."""
        tools = [{"type": "function", "function": {"name": "get_weather"}}]
        _, _, _, payload = openai_handler.prepare_request_data(
            model_name="gpt-4o",
            request_type="tool_call",
            is_streaming=False,
            prompt="What's the weather?",
            system_prompt=None,
            processed_images=None,
            final_generation_config={},
            tools=tools,
            tool_choice="auto",
            text_to_embed=None,
            enable_google_search=False,
        )
        assert "tools" in payload
        assert payload["tools"][0]["function"]["name"] == "get_weather"
        assert payload["tool_choice"] == "auto"
