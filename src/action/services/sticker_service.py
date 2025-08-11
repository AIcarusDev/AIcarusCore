# src/action/services/sticker_service.py
import asyncio
import base64
import mimetypes
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

import aiohttp
from src.common.create_grid import create_sticker_grid
from src.common.custom_logging.logging_config import get_logger
from src.common.image_utils import calculate_perceptual_hash
from src.config import config
from src.config.config_paths import PROJECT_ROOT
from src.database import EventStorageService
from src.database.services.sticker_storage_service import StickerStorageService

if TYPE_CHECKING:
    from src.database.services.sticker_storage_service import StickerStorageService

logger = get_logger(__name__)


class StickerService:
    """一个专门处理所有与表情包管理相关的业务逻辑的服务."""

    def __init__(
        self,
        sticker_storage_service: "StickerStorageService",
        event_storage_service: "EventStorageService",
    ) -> None:
        self.sticker_storage_service = sticker_storage_service
        self.event_storage_service = event_storage_service
        self._stickers_dir = Path(config.runtime_environment.stickers_dir)
        self._initialize_directories()
        logger.info("StickerService 已初始化。")

    def _initialize_directories(self) -> None:
        """初始化所有需要的目录."""
        self._stickers_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"表情包目录已确认: {self._stickers_dir}")

    async def manage_stickers(self, platform_id: str, params: dict) -> str:
        """表情包管理动作的总入口和分发器.

        这是从 ActionHandler 委托过来的主方法.
        """
        sub_command = next(
            (cmd for cmd in ["add", "remove", "edit_impression"] if cmd in params), None
        )
        if not sub_command:
            return "错误：manage_stickers 指令缺少有效的子命令 (add/remove/edit_impression)。"

        result_message = ""
        try:
            if sub_command == "add":
                result_message = await self.add_sticker(platform_id, params["add"])
            elif sub_command == "remove":
                result_message = await self.remove_sticker(platform_id, params["remove"])
            elif sub_command == "edit_impression":
                result_message = await self.edit_impression(platform_id, params["edit_impression"])
        except Exception as e:
            logger.error(f"处理 manage_stickers.{sub_command} 时发生意外错误: {e}", exc_info=True)
            result_message = f"错误：执行 {sub_command} 操作时发生内部错误。"

        await self.regenerate_sticker_grid(platform_id)
        return result_message

    async def add_sticker(self, platform_id: str, params: dict) -> str:
        """处理添加表情包的逻辑 (已重构，加入查重)."""
        image_hash = params.get("image_hash")
        impression = params.get("impression")
        if not image_hash or not impression:
            return "错误：添加表情包缺少 image_hash 或 impression。"

        event_doc = await self.event_storage_service.find_event_by_image_hash(image_hash)
        if not event_doc:
            return f"错误：找不到哈希值为 '{image_hash}' 的图片来源。"

        image_seg = next(
            (
                seg
                for seg in event_doc.get("content", [])
                if seg.get("data", {}).get("hash") == image_hash
            ),
            None,
        )
        if not image_seg:
            return f"错误：在事件 '{event_doc['_key']}' 中无法定位哈希为 '{image_hash}' 的图片段。"

        img_data = image_seg.get("data", {})
        img_url = img_data.get("url")
        img_b64 = img_data.get("base64")

        if not img_url and not img_b64:
            return "错误：图片来源中既没有URL也没有Base64数据。"

        try:
            if img_b64:
                image_bytes = base64.b64decode(img_b64)
            else:
                async with aiohttp.ClientSession() as session, session.get(img_url) as response:
                    response.raise_for_status()
                    image_bytes = await response.read()

            perceptual_hash = calculate_perceptual_hash(image_bytes)
            if not perceptual_hash:
                return "错误：无法计算图片的感知哈希，无法添加。"

            similar_sticker = await self.sticker_storage_service.find_similar_sticker_by_phash(
                platform_id, perceptual_hash
            )
            if similar_sticker:
                return (
                    f"操作完成：这个表情包看起来和已有的表情包 '{similar_sticker['sticker_id']}' "
                    f"非常相似，无需重复添加。"
                )

            mime_type = img_data.get("mime_type", "image/jpeg")
            extension = mimetypes.guess_extension(mime_type) or ".jpg"
            if extension == ".jpe":
                extension = ".jpg"

            new_filename = f"sticker_{uuid.uuid4().hex}{extension}"
            save_path = self._stickers_dir / new_filename
            with open(save_path, "wb") as f:
                f.write(image_bytes)

            sticker_doc = await self.sticker_storage_service.add_sticker(
                platform_id,
                new_filename,
                impression,
                image_hash,
                perceptual_hash,  # 传递pHash
            )
            if not sticker_doc:
                save_path.unlink(missing_ok=True)
                return "错误：将表情包元数据存入数据库时失败。"

            return (
                f"成功！表情包 '{sticker_doc.sticker_id}' 已添加到你的收藏，"
                f"印象是：“{impression}”。"
            )
        except Exception as e:
            logger.error(f"添加表情包 (hash: {image_hash}) 过程出错: {e}", exc_info=True)
            return "错误：处理图片数据或保存文件时发生错误。"

    async def run_garbage_collection(self) -> dict[str, int]:
        """执行表情包垃圾回收，清理文件系统中存在但数据库中无记录的孤儿文件."""
        logger.info("开始执行表情包目录的垃圾回收...")
        try:
            # 1. 获取数据库中所有已注册的文件名
            all_db_stickers = await self.sticker_storage_service.get_all_stickers(
                platform="qq"
            )  # 假设目前只有qq
            registered_filenames = {sticker["filename"] for sticker in all_db_stickers}

            # 2. 获取文件系统中所有实际存在的文件名
            if not self._stickers_dir.exists():
                logger.warning("表情包目录不存在，无需进行垃圾回收。")
                return {"scanned": 0, "deleted": 0}

            disk_files = {
                f.name
                for f in self._stickers_dir.iterdir()
                if f.is_file() and f.name.startswith("sticker_")
            }

            # 3. 找出差集，即孤儿文件
            orphan_files = disk_files - registered_filenames

            deleted_count = 0
            if not orphan_files:
                logger.info("文件系统与数据库记录一致，没有发现孤儿表情包文件。")
            else:
                logger.warning(f"发现 {len(orphan_files)} 个孤儿表情包文件，准备清理...")
                for filename in orphan_files:
                    try:
                        (self._stickers_dir / filename).unlink()
                        logger.info(f"  - 已删除孤儿文件: {filename}")
                        deleted_count += 1
                    except OSError as e:
                        logger.error(f"  - 删除文件 {filename} 失败: {e}")

            summary = {"scanned": len(disk_files), "deleted": deleted_count}
            logger.info(
                f"表情包垃圾回收完成。扫描文件: {summary['scanned']}, "
                f"删除孤儿文件: {summary['deleted']}."
            )
            return summary
        except Exception as e:
            logger.error(f"执行表情包垃圾回收时发生严重错误: {e}", exc_info=True)
            return {"scanned": -1, "deleted": -1}  # 返回错误标记

    async def remove_sticker(self, platform_id: str, params: dict) -> str:
        """处理移除表情包的逻辑."""
        sticker_id = params.get("sticker_id")
        if not sticker_id:
            return "错误：移除表情包缺少 sticker_id。"

        sticker_doc = await self.sticker_storage_service.get_sticker_by_id(platform_id, sticker_id)
        if not sticker_doc:
            return f"操作完成，但表情包 '{sticker_id}' 本来就不在你的收藏中。"

        filepath = self._stickers_dir / sticker_doc["filename"]
        filepath.unlink(missing_ok=True)

        if await self.sticker_storage_service.remove_sticker(platform_id, sticker_id):
            return f"成功！已从你的收藏中移除表情包 '{sticker_id}'。"
        else:
            return f"错误：从数据库移除表情包 '{sticker_id}' 时失败。"

    async def edit_impression(self, platform_id: str, params: dict) -> str:
        """处理编辑表情包印象的逻辑."""
        sticker_id = params.get("sticker_id")
        new_impression = params.get("new_impression")
        if not sticker_id or not new_impression:
            return "错误：编辑印象缺少 sticker_id 或 new_impression。"

        if await self.sticker_storage_service.edit_impression(
            platform_id, sticker_id, new_impression
        ):
            return f"成功！表情包 '{sticker_id}' 的印象已更新为：“{new_impression}”。"
        else:
            return f"错误：更新表情包 '{sticker_id}' 的印象时失败，可能该表情包不存在。"

    async def regenerate_sticker_grid(self, platform_id: str) -> None:
        """获取最新的表情包元数据，并调用缩略图生成函数."""
        logger.info(f"正在为平台 '{platform_id}' 触发表情包缩略图重新生成...")
        try:
            all_stickers_meta = await self.sticker_storage_service.get_all_stickers(platform_id)
            if not all_stickers_meta:
                logger.info(f"平台 '{platform_id}' 没有任何表情包，无需生成缩略图。")
                preview_path = self._stickers_dir / f"{platform_id}_stickers_preview.jpg"
                preview_path.unlink(missing_ok=True)
                return

            output_path = self._stickers_dir / f"{platform_id}_stickers_preview.jpg"
            config_dict = {
                "thumbnail_size": (150, 150),
                "columns": 5,
                "spacing": 20,
                "margin": 40,
                "background_color": "#FFFFFF",
                "font_path": str(PROJECT_ROOT / "asset/font/MAPLEMONO-NF-CN-SEMIBOLD.TTF"),
                "font_size": 24,
                "label_color": "#333333",
                "label_spacing": 10,
            }
            await asyncio.to_thread(
                create_sticker_grid, self._stickers_dir, all_stickers_meta, output_path, config_dict
            )
        except Exception as e:
            logger.error(
                f"为平台 '{platform_id}' 重新生成表情包缩略图时发生严重错误: {e}", exc_info=True
            )

    # +++ 新增的公共方法 +++
    async def get_sticker_file_path(self, platform_id: str, sticker_id: str) -> Path | None:
        """根据平台和表情包ID，获取其在文件系统中的完整路径.

        这是提供给 MessageBuilder 等外部模块使用的安全接口.
        """
        sticker_doc = await self.sticker_storage_service.get_sticker_by_id(platform_id, sticker_id)
        if not sticker_doc:
            logger.error(
                f"StickerService: 找不到平台 '{platform_id}' 编号为 '{sticker_id}' 的表情包。"
            )
            return None

        filename = sticker_doc.get("filename")
        if not filename:
            logger.error(f"StickerService: 表情包 '{sticker_id}' 在数据库中缺少文件名。")
            return None

        return self._stickers_dir / filename
