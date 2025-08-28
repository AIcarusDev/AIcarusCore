import pytest
from src.services.database.services import ImageAnalysisCacheService


@pytest.mark.asyncio
async def test_save_and_get_analysis_cache(
    image_analysis_cache_service: ImageAnalysisCacheService,
) -> None:
    """Test saving and retrieving image analysis cache data.

    Parameters
    ----------
    image_analysis_cache_service : ImageAnalysisCacheService
        The image analysis cache service instance for testing.
    """
    service = image_analysis_cache_service
    image_hash, analysis_result, version, ttl = (
        "hash_for_cache_test",
        {"description": "一只猫"},
        "v1.0",
        60,
    )
    assert await service.get_analysis_by_hash(image_hash, version, ttl) is None
    assert await service.save_analysis(image_hash, analysis_result, version) is True
    assert await service.get_analysis_by_hash(image_hash, version, ttl) == analysis_result
    assert await service.get_analysis_by_hash(image_hash, "v1.1", ttl) is None
    assert await service.get_analysis_by_hash(image_hash, version, -1) is None
