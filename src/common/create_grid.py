# src/common/create_grid.py
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont
from src.common.custom_logging.logging_config import get_logger

logger = get_logger(__name__)


@dataclass
class GridConfig:
    """用于存储网格生成配置的数据类."""

    thumbnail_size: tuple[int, int]
    columns: int
    spacing: int
    margin: int
    background_color: str
    font_path: str
    font_size: int
    label_color: str
    label_spacing: int

    @classmethod
    def from_dict(cls, config: dict[str, Any]) -> "GridConfig":
        """从原始配置字典创建 GridConfig 实例."""
        return cls(
            thumbnail_size=config.get("thumbnail_size", (150, 150)),
            columns=config.get("columns", 5),
            spacing=config.get("spacing", 20),
            margin=config.get("margin", 40),
            background_color=config.get("background_color", "white"),
            font_path=config.get("font_path"),
            font_size=config.get("font_size", 24),
            label_color=config.get("label_color", "black"),
            label_spacing=config.get("label_spacing", 10),
        )


def _filter_valid_sticker_files(
    metadata_list: list[dict], stickers_dir: Path
) -> list[tuple[str, Path]]:
    """从元数据中筛选出实际存在的表情包文件."""
    valid_files = []
    for item in metadata_list:
        sticker_path = stickers_dir / item["filename"]
        if sticker_path.exists():
            valid_files.append((item["sticker_id"], sticker_path))
        else:
            logger.warning(f"元数据中引用的表情包文件不存在，已跳过: {sticker_path}")
    return valid_files


def _calculate_grid_dimensions(num_images: int, config: GridConfig) -> tuple[int, int, int]:
    """根据图片数量和配置计算最终网格图片的尺寸."""
    num_rows = math.ceil(num_images / config.columns)
    cell_height = config.thumbnail_size[1] + config.label_spacing + config.font_size
    total_width = (
        (config.margin * 2)
        + (config.columns * config.thumbnail_size[0])
        + ((config.columns - 1) * config.spacing)
    )
    total_height = (
        (config.margin * 2) + (num_rows * cell_height) + ((num_rows - 1) * config.spacing)
    )
    return total_width, total_height, cell_height


def _initialize_canvas_and_font(
    width: int, height: int, config: GridConfig
) -> tuple[Image.Image, ImageDraw.ImageDraw, ImageFont.FreeTypeFont]:
    """创建并返回画布、绘图对象和字体对象."""
    grid_image = Image.new("RGB", (width, height), color=config.background_color)
    draw = ImageDraw.Draw(grid_image)
    font = ImageFont.load_default()
    if config.font_path and Path(config.font_path).exists():
        try:
            font = ImageFont.truetype(str(config.font_path), config.font_size)
        except OSError:
            logger.warning(f"无法加载字体 '{config.font_path}'，使用默认字体。")
    return grid_image, draw, font


def _draw_single_sticker(
    grid_image: Image.Image,
    draw: ImageDraw.ImageDraw,
    font: ImageFont.FreeTypeFont,
    config: GridConfig,
    sticker_id: str,
    path: Path,
    index: int,
    cell_height: int,
) -> None:
    """在画布上绘制单个表情包缩略图及其标签."""
    row = index // config.columns
    col = index % config.columns
    x = config.margin + col * (config.thumbnail_size[0] + config.spacing)
    y = config.margin + row * (cell_height + config.spacing)

    # 绘制缩略图
    try:
        with Image.open(path) as img:
            if hasattr(img, "seek"):
                img.seek(0)
            img = img.convert("RGBA")
            img.thumbnail(config.thumbnail_size, Image.Resampling.LANCZOS)

            thumb_canvas = Image.new("RGBA", config.thumbnail_size, (0, 0, 0, 0))
            paste_pos = (
                (config.thumbnail_size[0] - img.width) // 2,
                (config.thumbnail_size[1] - img.height) // 2,
            )
            thumb_canvas.paste(img, paste_pos, img)
            grid_image.paste(thumb_canvas, (x, y), thumb_canvas)
    except Exception as e:
        logger.error(f"处理图片 {path} 失败: {e}")
        return

    # 绘制标签
    label_text = str(sticker_id)
    bbox = draw.textbbox((0, 0), label_text, font=font)
    text_width = bbox[2] - bbox[0]
    text_x = x + (config.thumbnail_size[0] - text_width) / 2
    text_y = y + config.thumbnail_size[1] + config.label_spacing
    draw.text((text_x, text_y), label_text, fill=config.label_color, font=font)


def create_sticker_grid(
    stickers_dir: Path, metadata_list: list[dict], output_path: Path, config: dict
) -> bool:
    """协调者函数：根据表情包元数据生成带标签的网格缩略图."""
    image_files_to_process = _filter_valid_sticker_files(metadata_list, stickers_dir)

    # 守卫子句：如果没有有效的图片，则提前返回
    if not image_files_to_process:
        logger.info("没有找到任何有效的表情包图片来生成缩略图。")
        if output_path.exists():
            output_path.unlink()
        return True

    try:
        grid_config = GridConfig.from_dict(config)
        total_width, total_height, cell_height = _calculate_grid_dimensions(
            len(image_files_to_process), grid_config
        )
        grid_image, draw, font = _initialize_canvas_and_font(total_width, total_height, grid_config)

        for i, (sticker_id, path) in enumerate(image_files_to_process):
            _draw_single_sticker(
                grid_image, draw, font, grid_config, sticker_id, path, i, cell_height
            )

        grid_image.save(output_path)
        logger.info(f"表情包网格缩略图已更新并保存到: {output_path}")
        return True
    except Exception as e:
        logger.error(f"生成或保存网格图片时发生未知错误: {e}", exc_info=True)
        return False
