# tests/llmrequest/test_real_api_integration.py

import os

import pytest
from pytest import MonkeyPatch
from src.services.llmrequest.llm_client import LLMClient

# --- 测试配置 ---
# 从环境变量中读取一个真实的、有效的Google Gemini API Key
# 在运行测试前，你需要在你的环境中设置这个变量，例如：
# export REAL_GEMINI_API_KEY="AIzaSy..."
REAL_GEMINI_API_KEY = os.getenv("REAL_GEMINI_API_KEY")

# 从环境变量中读取一个真实的、有效的OpenAI API Key
# export REAL_OPENAI_API_KEY="sk-..."
REAL_OPENAI_API_KEY = os.getenv("REAL_OPENAI_API_KEY")

# 使用pytest.mark.skipif来动态跳过测试
# 如果没有提供真实API Key，测试会被跳过而不是失败
requires_gemini_key = pytest.mark.skipif(
    not REAL_GEMINI_API_KEY, reason="需要设置 REAL_GEMINI_API_KEY 环境变量"
)
requires_openai_key = pytest.mark.skipif(
    not REAL_OPENAI_API_KEY, reason="需要设置 REAL_OPENAI_API_KEY 环境变量"
)

# 将此文件中所有测试都标记为异步测试
pytestmark = pytest.mark.asyncio


# --- 测试用例 ---


@pytest.mark.integration  # <--- 修改 1: 添加集成测试标记
@requires_gemini_key
async def test_google_gemini_real_api_call(monkeypatch: MonkeyPatch) -> None:
    """集成测试：真实调用 Google Gemini API.

    这个测试会发出一个真实的网络请求。
    """
    # 1. 准备环境
    # 使用 monkeypatch 来隔离本次测试的环境变量
    monkeypatch.setenv("GEMINI_API_KEYS", f'["{REAL_GEMINI_API_KEY}"]')
    monkeypatch.setenv("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta")

    # 2. 初始化客户端
    # 我们特意选择一个真实存在的、轻量的模型
    client = LLMClient(model={"provider": "gemini", "name": "gemini-1.5-flash-latest"})

    # 3. 执行请求
    print("\n[INFO] Sending a real request to Google Gemini API...")
    result = None
    try:
        result = await client.make_request(
            prompt="用一个词回答：天空是什么颜色的？",
            system_prompt=None,
            is_stream=False,
            max_tokens=5,  # 限制输出以节省token
        )
        print(f"[INFO] Received response from Gemini: {result}")
    except Exception as e:
        pytest.fail(f"调用 Google Gemini API 时发生意外异常: {e}")
    finally:
        await client.close()

    # 4. 断言结果
    assert result is not None, "API 响应不应为 None"
    assert not result.get("error"), f"API 返回了错误: {result.get('message')}"
    assert "text" in result, "响应中应包含 'text' 字段"
    assert result["text"] is not None and len(result["text"]) > 0, "响应文本不应为空"
    # 我们期望答案中包含“蓝”字
    assert "蓝" in result["text"], f"预期响应包含'蓝'，但实际为: '{result['text']}'"


@pytest.mark.integration  # <--- 修改 2: 添加集成测试标记
@requires_openai_key
async def test_openai_compatible_real_api_call(monkeypatch: MonkeyPatch) -> None:
    """集成测试：真实调用 OpenAI 兼容的 API (例如 Google 的兼容层)."""
    # 1. 准备环境
    # 注意：这里我们使用 OPENAI 的 provider 名称，但 URL 指向 Google 的兼容层
    monkeypatch.setenv(
        "OPENAI_API_KEYS", f'["{REAL_OPENAI_API_KEY}"]'
    )  # 注意：Google兼容层用的是Google的Key
    monkeypatch.setenv("OPENAI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai")

    # 2. 初始化客户端
    client = LLMClient(model={"provider": "openai", "name": "gemini-1.5-flash"})

    # 3. 执行请求
    print("\n[INFO] Sending a real request to OpenAI-compatible API (Google)...")
    result = None
    try:
        result = await client.make_request(
            prompt="用一个词回答：天空是什么颜色的？",
            system_prompt="You are a helpful assistant.",
            is_stream=False,
            max_tokens=5,
        )
        print(f"[INFO] Received response from OpenAI-compatible API: {result}")
    except Exception as e:
        pytest.fail(f"调用 OpenAI-compatible API 时发生意外异常: {e}")
    finally:
        await client.close()

    # 4. 断言结果
    assert result is not None, "API 响应不应为 None"
    assert not result.get("error"), f"API 返回了错误: {result.get('message')}"
    assert "text" in result, "响应中应包含 'text' 字段"
    assert result["text"] is not None and len(result["text"]) > 0, "响应文本不应为空"
    assert "蓝" in result["text"], f"预期响应包含'蓝'，但实际为: '{result['text']}'"


@pytest.mark.integration  # <--- 修改 3: 添加集成测试标记
async def test_invalid_key_returns_permission_error(monkeypatch: MonkeyPatch) -> None:
    """集成测试：使用一个无效的 Key 调用，预期返回权限错误，而不是 404."""
    # 1. 准备环境
    invalid_key = "invalid-api-key-for-testing"
    monkeypatch.setenv("GEMINI_API_KEYS", f'["{invalid_key}"]')
    monkeypatch.setenv("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta")

    # 2. 初始化客户端
    client = LLMClient(model={"provider": "gemini", "name": "gemini-1.5-flash-latest"})

    # 3. 执行请求
    print("\n[INFO] Sending a real request with an INVALID key...")
    result = None
    try:
        result = await client.make_request(
            prompt="test",
            system_prompt=None,
            is_stream=False,
        )
        print(f"[INFO] Received response with invalid key: {result}")
    except Exception as e:
        pytest.fail(f"使用无效 Key 调用时不应直接抛出异常，而是返回错误字典: {e}")
    finally:
        await client.close()

    # 4. 断言结果
    assert result is not None
    assert result.get("error") is True
    assert result.get("type") in ["PermissionDeniedError", "APIResponseError"]
    assert result.get("status_code") in [400, 401, 403]
