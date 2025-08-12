# tests/message_processing/test_image_analysis_service.py

import asyncio
import base64  # <-- 导入 base64 库
import hashlib
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
    # 验证 save_analysis 被调用，并且是在 _analyze_single_image_core 内部完成的
    # （因为我们模拟了 _analyze_single_image_core，所以这里不再直接断言 save_analysis）
    # 如果需要更细粒度的测试，就需要模拟 _calculate_embedding 和 _generate_description
    # 但当前的模拟粒度对于验证 get_analysis_result 的流程是足够的。


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
