# tests/message_processing/test_image_analysis_service.py

import asyncio
import base64  # <-- 导入 base64 库
import hashlib
import json
from typing import Any
from unittest.mock import ANY, MagicMock

import pytest
from pytest_mock import MockerFixture
from src.message_processing.image_analysis_service import ImageAnalysisService

# 这是一个 1x1 像素的红色 GIF 图片的 Base64 编码
VALID_B64_STRING = "R0lGODlhAQABAIAAAP8AAAAAACH5BAEAAAAALAAAAAABAAEAAAICRAEAOw=="

# --- [核心修正] ---
# 我们必须模拟服务内部的真实行为：
# 先将 Base64 字符串解码成二进制字节，然后再计算哈希。
VALID_B64_BYTES = base64.b64decode(VALID_B64_STRING)
VALID_B64_HASH = hashlib.sha256(VALID_B64_BYTES).hexdigest()
# --- [结束修正] ---


@pytest.fixture
def mock_conn_manager(mocker: MockerFixture) -> MagicMock:
    """创建一个模拟的数据库连接管理器."""
    return mocker.MagicMock()


@pytest.fixture
def mock_cache_service(mocker: MockerFixture) -> MagicMock:
    """创建一个模拟的缓存服务."""
    mock = mocker.MagicMock()
    mock.get_analysis_by_hash = mocker.AsyncMock(return_value=None)
    mock.save_analysis = mocker.AsyncMock()
    return mock


@pytest.fixture
def analysis_service(
    mocker: MockerFixture, mock_conn_manager: MagicMock, mock_cache_service: MagicMock
) -> ImageAnalysisService:
    """创建一个带有模拟依赖的 ImageAnalysisService 实例."""
    service = ImageAnalysisService(mock_conn_manager, mock_cache_service)

    # 模拟内部的核心分析方法，因为我们不关心它们的真实实现
    mocker.patch.object(
        service,
        "_analyze_single_image_core",
        new_callable=mocker.AsyncMock,
        return_value={
            "type": "image",
            "embedding": [0.1, 0.2, 0.3],
            "details": {"description": "A beautiful cat"},
        },
    )
    return service


@pytest.fixture
def analysis_service_with_real_methods(
    mocker: MockerFixture, mock_conn_manager: MagicMock, mock_cache_service: MagicMock
) -> ImageAnalysisService:
    """创建一个 ImageAnalysisService 实例，但只模拟 LLM 客户端和 CLIP 模型."""
    service = ImageAnalysisService(mock_conn_manager, mock_cache_service)

    # 模拟 CLIP 模型 - 返回 numpy 数组格式的结果
    import numpy as np

    mock_clip_model = mocker.MagicMock()
    mock_clip_model.encode.return_value = np.array([0.1, 0.2, 0.3])
    mocker.patch.object(service, "_get_clip_model", return_value=mock_clip_model)

    # 模拟 Vision LLM 客户端
    mock_llm_client = mocker.MagicMock()
    mocker.patch.object(service, "_get_vision_llm_client", return_value=mock_llm_client)

    return service


@pytest.mark.asyncio
async def test_get_analysis_cache_hit(
    analysis_service: ImageAnalysisService, mock_cache_service: MagicMock
) -> None:
    """测试场景1: 缓存命中时，应直接返回缓存结果，不执行新分析."""
    fake_hash = "hash_of_a_cat_image"
    cached_result = {"details": {"description": "A cached cat"}}
    mock_cache_service.get_analysis_by_hash.return_value = cached_result

    result = await analysis_service.get_analysis_result(fake_hash, VALID_B64_STRING, {})

    assert result == cached_result
    mock_cache_service.get_analysis_by_hash.assert_awaited_once_with(
        fake_hash, version=ANY, ttl_seconds=ANY
    )
    # 验证核心分析方法没有被调用
    analysis_service._analyze_single_image_core.assert_not_called()


@pytest.mark.asyncio
async def test_get_analysis_cache_miss_and_execute(
    analysis_service: ImageAnalysisService, mock_cache_service: MagicMock
) -> None:
    """测试场景2: 缓存未命中时，应执行分析、保存结果并返回."""
    # 安排 (Arrange)
    fake_hash_from_caller = VALID_B64_HASH
    fake_b64 = VALID_B64_STRING
    fake_seg_data = {"mime_type": "image/jpeg", "summary": "image"}
    expected_analysis_result = {
        "type": "image",
        "embedding": [0.1, 0.2, 0.3],
        "details": {"description": "A beautiful cat"},
    }

    # 行动 (Act)
    result = await analysis_service.get_analysis_result(
        fake_hash_from_caller, fake_b64, fake_seg_data
    )

    # 断言 (Assert)
    assert result == expected_analysis_result
    # 验证核心分析方法被正确调用
    analysis_service._analyze_single_image_core.assert_awaited_once_with(fake_b64, fake_seg_data)


@pytest.mark.asyncio
async def test_get_analysis_concurrent_wait(
    analysis_service: ImageAnalysisService, mocker: MockerFixture
) -> None:
    """测试场景3: 两个任务同时请求同一图片，只应执行一次分析."""
    # 安排 (Arrange)
    fake_hash = VALID_B64_HASH
    fake_b64 = VALID_B64_STRING
    expected_result = {
        "type": "image",
        "embedding": [0.1, 0.2, 0.3],
        "details": {"description": "A beautiful cat"},
    }

    # 使用一个 Event 来模拟耗时的分析过程
    analysis_started = asyncio.Event()
    analysis_finished = asyncio.Event()

    original_core_method = analysis_service._analyze_single_image_core

    async def slow_core_analysis(*args: Any, **kwargs: Any) -> dict:
        analysis_started.set()
        await asyncio.sleep(0.1)  # 模拟IO或CPU密集型工作
        result = await original_core_method(*args, **kwargs)
        analysis_finished.set()
        return result

    mocker.patch.object(
        analysis_service, "_analyze_single_image_core", side_effect=slow_core_analysis
    )

    # 行动 (Act)
    task1 = asyncio.create_task(
        analysis_service.get_analysis_result(fake_hash, fake_b64, {"summary": "image"})
    )
    # 确保第一个任务已经进入了分析流程并创建了 Future
    await analysis_started.wait()
    task2 = asyncio.create_task(
        analysis_service.get_analysis_result(fake_hash, fake_b64, {"summary": "image"})
    )

    results = await asyncio.gather(task1, task2)

    # 断言 (Assert)
    assert results[0] == expected_result
    assert results[1] == expected_result
    # 核心断言：真正的分析方法只被调用了一次
    analysis_service._analyze_single_image_core.assert_awaited_once()


@pytest.mark.asyncio
async def test_generate_description_success(
    analysis_service_with_real_methods: ImageAnalysisService, mocker: MockerFixture
) -> None:
    """测试生成描述成功的情况."""
    service = analysis_service_with_real_methods
    mock_llm_client = service._get_vision_llm_client()

    # 模拟成功的 LLM 响应
    expected_response = {"emotion": "happy", "description": "A cute cat sticker"}
    mock_llm_client.make_llm_request = mocker.AsyncMock(
        return_value={"text": json.dumps(expected_response)}
    )

    result = await service._generate_description("sticker", VALID_B64_STRING, "image/gif")

    assert result == expected_response
    mock_llm_client.make_llm_request.assert_awaited_once()


@pytest.mark.asyncio
async def test_generate_description_json_parse_failure(
    analysis_service_with_real_methods: ImageAnalysisService, mocker: MockerFixture
) -> None:
    """测试 JSON 解析失败的情况."""
    service = analysis_service_with_real_methods
    mock_llm_client = service._get_vision_llm_client()

    # 模拟返回无效的 JSON 字符串
    mock_llm_client.make_llm_request = mocker.AsyncMock(return_value={"text": "invalid json {"})

    result = await service._generate_description("sticker", VALID_B64_STRING, "image/gif")

    # 应该返回默认的错误响应
    assert result == {"description": "分析失败或无返回"}
    # 注意：由于日志输出到 stderr，我们无法使用 caplog.text 进行断言


@pytest.mark.asyncio
async def test_generate_description_non_dict_result(
    analysis_service_with_real_methods: ImageAnalysisService, mocker: MockerFixture
) -> None:
    """测试 JSON 解析返回非字典类型的情况."""
    service = analysis_service_with_real_methods
    mock_llm_client = service._get_vision_llm_client()

    # 模拟返回非字典的 JSON（如列表）
    mock_llm_client.make_llm_request = mocker.AsyncMock(
        return_value={"text": '["list", "not", "dict"]'}
    )

    result = await service._generate_description("sticker", VALID_B64_STRING, "image/gif")

    # 应该返回默认的错误响应
    assert result == {"description": "分析失败或无返回"}
    # 注意：日志输出到 stderr，无法使用 caplog.text 进行断言


@pytest.mark.asyncio
async def test_generate_description_empty_response(
    analysis_service_with_real_methods: ImageAnalysisService, mocker: MockerFixture
) -> None:
    """测试 LLM 返回空响应的情况."""
    service = analysis_service_with_real_methods
    mock_llm_client = service._get_vision_llm_client()

    # 模拟空响应
    mock_llm_client.make_llm_request = mocker.AsyncMock(return_value={})

    result = await service._generate_description("sticker", VALID_B64_STRING, "image/gif")

    # 应该返回默认的错误响应
    assert result == {"description": "分析失败或无返回"}


@pytest.mark.asyncio
async def test_generate_description_llm_exception(
    analysis_service_with_real_methods: ImageAnalysisService, mocker: MockerFixture
) -> None:
    """测试 LLM 请求抛出异常的情况."""
    service = analysis_service_with_real_methods
    mock_llm_client = service._get_vision_llm_client()

    # 模拟 LLM 请求抛出异常
    mock_llm_client.make_llm_request = mocker.AsyncMock(
        side_effect=Exception("LLM service unavailable")
    )

    result = await service._generate_description("sticker", VALID_B64_STRING, "image/gif")

    # 应该返回默认的错误响应
    assert result == {"description": "分析时发生异常"}
    # 注意：日志输出到 stderr，无法使用 caplog.text 进行断言


@pytest.mark.asyncio
async def test_calculate_embedding_success(
    analysis_service_with_real_methods: ImageAnalysisService, mocker: MockerFixture
) -> None:
    """测试计算嵌入成功的情况."""
    service = analysis_service_with_real_methods
    mock_clip_model = service._get_clip_model()

    # 模拟成功的嵌入计算 - 返回 numpy 数组（.tolist() 会将其转换为列表）
    import numpy as np

    mock_clip_model.encode.return_value = np.array([0.1, 0.2, 0.3])

    result = await service._calculate_embedding(VALID_B64_STRING)

    assert result == [0.1, 0.2, 0.3]
    mock_clip_model.encode.assert_called_once()


@pytest.mark.asyncio
async def test_calculate_embedding_failure(
    analysis_service_with_real_methods: ImageAnalysisService, mocker: MockerFixture
) -> None:
    """测试计算嵌入失败的情况."""
    service = analysis_service_with_real_methods
    mock_clip_model = service._get_clip_model()

    # 模拟嵌入计算抛出异常
    mock_clip_model.encode.side_effect = Exception("CLIP model error")

    result = await service._calculate_embedding(VALID_B64_STRING)

    # 应该返回 None
    assert result is None
    # 注意：日志输出到 stderr，无法使用 caplog.text 进行断言


@pytest.mark.asyncio
async def test_analyze_single_image_core_embedding_failure(
    analysis_service_with_real_methods: ImageAnalysisService, mocker: MockerFixture
) -> None:
    """测试核心分析中嵌入计算失败的情况."""
    service = analysis_service_with_real_methods
    mock_llm_client = service._get_vision_llm_client()

    # 模拟嵌入计算失败
    mocker.patch.object(service, "_calculate_embedding", return_value=None)

    # 模拟成功的描述生成
    mock_llm_client.make_llm_request = mocker.AsyncMock(
        return_value={"text": json.dumps({"description": "A test image"})}
    )

    result = await service._analyze_single_image_core(VALID_B64_STRING, {"summary": "image"})

    # 应该包含 None 嵌入和成功的描述
    assert result["embedding"] is None
    assert result["details"]["description"] == "A test image"
    assert result["type"] == "image"


@pytest.mark.asyncio
async def test_analyze_single_image_core_description_failure(
    analysis_service_with_real_methods: ImageAnalysisService, mocker: MockerFixture
) -> None:
    """测试核心分析中描述生成失败的情况."""
    service = analysis_service_with_real_methods
    mock_clip_model = service._get_clip_model()
    mock_llm_client = service._get_vision_llm_client()

    # 模拟成功的嵌入计算 - 返回 numpy 数组（.tolist() 会将其转换为列表）
    import numpy as np

    mock_clip_model.encode.return_value = np.array([0.1, 0.2, 0.3])

    # 模拟描述生成失败（返回空响应）
    mock_llm_client.make_llm_request = mocker.AsyncMock(return_value={})

    result = await service._analyze_single_image_core(VALID_B64_STRING, {"summary": "image"})

    # 应该包含成功的嵌入和失败的描述
    assert result["embedding"] == [0.1, 0.2, 0.3]
    assert result["details"]["description"] == "分析失败或无返回"
    assert result["type"] == "image"


@pytest.mark.asyncio
async def test_get_analysis_result_core_analysis_exception(
    analysis_service: ImageAnalysisService, mocker: MockerFixture
) -> None:
    """测试核心分析抛出异常的情况."""
    # 模拟核心分析抛出异常
    analysis_service._analyze_single_image_core = mocker.AsyncMock(
        side_effect=Exception("Analysis failed")
    )

    result = await analysis_service.get_analysis_result(
        VALID_B64_HASH, VALID_B64_STRING, {"summary": "image"}
    )

    # 应该返回 None
    assert result is None
    # 注意：日志输出到 stderr，无法使用 caplog.text 进行断言


@pytest.mark.asyncio
async def test_concurrent_analysis_with_failure(
    analysis_service: ImageAnalysisService, mocker: MockerFixture
) -> None:
    """测试并发分析中一个任务失败的情况."""
    fake_hash = VALID_B64_HASH
    fake_b64 = VALID_B64_STRING

    # 使用一个 Event 来控制分析流程
    analysis_started = asyncio.Event()

    async def failing_core_analysis(*args: Any, **kwargs: Any) -> dict:
        analysis_started.set()
        await asyncio.sleep(0.1)
        raise Exception("Analysis failed")

    mocker.patch.object(
        analysis_service, "_analyze_single_image_core", side_effect=failing_core_analysis
    )

    # 启动两个并发任务
    task1 = asyncio.create_task(
        analysis_service.get_analysis_result(fake_hash, fake_b64, {"summary": "image"})
    )
    await analysis_started.wait()
    task2 = asyncio.create_task(
        analysis_service.get_analysis_result(fake_hash, fake_b64, {"summary": "image"})
    )

    # 两个任务都应该失败
    results = await asyncio.gather(task1, task2, return_exceptions=True)

    # 两个结果都应该是 None（因为异常被捕获并返回 None）
    assert results[0] is None
    assert results[1] is None


@pytest.mark.asyncio
async def test_cache_service_exception(
    analysis_service: ImageAnalysisService, mock_cache_service: MagicMock
) -> None:
    """测试缓存服务抛出异常的情况."""
    # 模拟缓存服务抛出异常
    mock_cache_service.get_analysis_by_hash.side_effect = Exception("Cache service error")

    result = await analysis_service.get_analysis_result(
        VALID_B64_HASH, VALID_B64_STRING, {"summary": "image"}
    )

    # 应该返回 None（因为异常被捕获并返回 None）
    assert result is None
    # 注意：日志输出到 stderr，无法使用 caplog.text 进行断言
