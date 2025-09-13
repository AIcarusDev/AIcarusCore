import pytest
from src.services.database.services import EntityGraphService, StickerStorageService


@pytest.mark.asyncio
async def test_sticker_lifecycle(
    sticker_storage_service: StickerStorageService, entity_graph_service: EntityGraphService
) -> None:
    """测试表情包的完整生命周期：添加、查找、编辑、移除."""
    sticker_service = sticker_storage_service
    platform_id = "test_platform"

    # 准备平台实体
    await entity_graph_service.get_or_create_platform_entity(platform_id, "Test Platform")

    # 1. 添加表情包
    added_sticker = await sticker_service.add_sticker(
        platform_id=platform_id,
        filename="smile.gif",
        impression="开心",
        source_image_hash="hash1",
        perceptual_hash="phash1",
    )
    assert added_sticker is not None
    # 验证返回的 sticker_id 是格式化后的字符串
    assert added_sticker["sticker_id"] == "001"

    # 2. 获取所有表情包，验证添加成功
    all_stickers = await sticker_service.get_all_stickers(platform_id)
    assert len(all_stickers) == 1
    assert all_stickers[0]["filename"] == "smile.gif"
    assert all_stickers[0]["sticker_id"] == "001"  # 验证 get_all 返回的也是格式化字符串

    # 3. 编辑印象
    assert await sticker_service.edit_impression(platform_id, "001", "非常开心") is True

    # 4. 验证印象已更新
    updated_stickers = await sticker_service.get_all_stickers(platform_id)
    assert updated_stickers[0]["impression"] == "非常开心"

    # 5. 移除表情包
    assert await sticker_service.remove_sticker(platform_id, "001") is True

    # 6. 验证已移除
    final_stickers = await sticker_service.get_all_stickers(platform_id)
    assert len(final_stickers) == 0
