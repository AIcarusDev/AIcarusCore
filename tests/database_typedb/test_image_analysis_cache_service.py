import pytest
from src.services.database.services import MediaCacheService


@pytest.mark.asyncio
async def test_save_and_get_media_cache(
    media_cache_service: MediaCacheService,
) -> None:
    """Test saving and retrieving media cache data.

    Parameters
    ----------
    media_cache_service : MediaCacheService
        The media cache service instance for testing.
    """
    service = media_cache_service
    media_hash, analysis_result, version, ttl = (
        "hash_for_cache_test",
        {"description": "一只猫"},
        "v1.0",
        60,
    )
    assert await service.get_analysis_by_hash(media_hash, version, ttl) is None
    assert await service.save_analysis(media_hash, analysis_result, version) is True
    assert await service.get_analysis_by_hash(media_hash, version, ttl) == analysis_result
    assert await service.get_analysis_by_hash(media_hash, "v1.1", ttl) is None
    assert await service.get_analysis_by_hash(media_hash, version, -1) is None
