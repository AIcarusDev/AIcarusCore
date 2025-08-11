# 文件: src/common/create_grid.py
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from src.common.custom_logging.logging_config import get_logger

logger = get_logger(__name__)

# --- [修改] 将核心逻辑封装成一个更通用的函数 ---
def create_sticker_grid(
    stickers_dir: Path,
    metadata_list: list[dict],
    output_path: Path,
    config: dict
) -> bool:
    """根据表情包元数据，将指定目录中的图片生成带数字标签的网格缩略图."""
    # --- 1. 从元数据中筛选出实际存在的图片文件 ---
    image_files_to_process = []
    for item in metadata_list:
        sticker_path = stickers_dir / item["filename"]
        if sticker_path.exists():
            image_files_to_process.append((item["sticker_id"], sticker_path))
        else:
            logger.warning(f"元数据中引用的表情包文件不存在，已跳过: {sticker_path}")

    if not image_files_to_process:
        logger.info("没有找到任何有效的表情包图片来生成缩略图。")
        # 如果输出文件存在，删除它，以反映空状态
        if output_path.exists():
            output_path.unlink()
        return True # 任务成功，只是没东西可画

    # --- 2. 加载配置 ---
    thumb_size = config.get("thumbnail_size", (150, 150))
    cols = config.get("columns", 5)
    spacing = config.get("spacing", 20)
    margin = config.get("margin", 40)
    bg_color = config.get("background_color", "white")
    font_path = config.get("font_path")
    font_size = config.get("font_size", 24)
    label_color = config.get("label_color", "black")
    label_spacing = config.get("label_spacing", 10)

    # --- 3. 计算最终图片的尺寸 ---
    num_images = len(image_files_to_process)
    num_rows = math.ceil(num_images / cols)

    cell_height = thumb_size[1] + label_spacing + font_size
    total_width = (margin * 2) + (cols * thumb_size[0]) + ((cols - 1) * spacing)
    total_height = (margin * 2) + (num_rows * cell_height) + ((num_rows - 1) * spacing)

    # --- 4. 创建画布和绘图工具 ---
    try:
        grid_image = Image.new("RGB", (total_width, total_height), color=bg_color)
        draw = ImageDraw.Draw(grid_image)

        font = None
        if font_path and Path(font_path).exists():
            try:
                font = ImageFont.truetype(str(font_path), font_size)
            except OSError:
                logger.warning(f"无法加载字体 '{font_path}'，使用默认字体。")
                font = ImageFont.load_default()
        else:
            font = ImageFont.load_default()
    except Exception as e:
        logger.error(f"创建缩略图画布失败: {e}")
        return False

    # --- 5. 遍历图片、创建缩略图并粘贴 ---
    for i, (sticker_id, path) in enumerate(image_files_to_process):
        row = i // cols
        col = i % cols
        x = margin + col * (thumb_size[0] + spacing)
        y = margin + row * (cell_height + spacing)

        try:
            with Image.open(path) as img:
                # 对于GIF，seek到第一帧来创建缩略图
                if hasattr(img, 'seek'):
                    img.seek(0)
                img.thumbnail(thumb_size, Image.Resampling.LANCZOS)
                thumb_img = Image.new("RGB", thumb_size, bg_color)
                paste_pos = ((thumb_size[0] - img.width) // 2, (thumb_size[1] - img.height) // 2)
                thumb_img.paste(img, paste_pos, img if img.mode == "RGBA" else None)
                grid_image.paste(thumb_img, (x, y))
        except Exception as e:
            logger.error(f"处理图片 {path} 失败: {e}")
            continue # 跳过这张有问题的图片

        # --- 6. 添加数字标签 ---
        label_text = str(sticker_id)
        # 使用 textbbox 获取文本尺寸来精确定位
        bbox = draw.textbbox((0, 0), label_text, font=font)
        text_width = bbox[2] - bbox[0]
        text_x = x + (thumb_size[0] - text_width) / 2
        text_y = y + thumb_size[1] + label_spacing
        draw.text((text_x, text_y), label_text, fill=label_color, font=font)

    # --- 7. 保存最终的图片 ---
    try:
        grid_image.save(output_path)
        logger.info(f"表情包网格缩略图已更新并保存到: {output_path}")
        return True
    except Exception as e:
        logger.error(f"保存网格图片失败: {e}")
        return False

# --- 保留 __main__ 块用于独立测试 ---
if __name__ == "__main__":
    # 这是一个示例，实际调用时会从 ActionHandler 传入参数
    from src.common.custom_logging.logging_config import get_logger
    logger = get_logger(__name__)

    # 模拟 ActionHandler 的调用
    WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
    STICKERS_DIR = WORKSPACE_ROOT / "data/workspace/stickers"
    STICKERS_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH = WORKSPACE_ROOT / "data/workspace/stickers_collection_preview.jpg"

    # 模拟 metadata
    mock_metadata = [
        {"sticker_id": "001", "filename": "test1.jpg", "impression": "Test 1"},
        {"sticker_id": "002", "filename": "test2.png", "impression": "Test 2"},
    ]

    # 模拟创建一些图片文件
    Image.new('RGB', (100, 100), color = 'red').save(STICKERS_DIR / 'test1.jpg')
    Image.new('RGB', (100, 100), color = 'green').save(STICKERS_DIR / 'test2.png')

    CONFIG = {
        "thumbnail_size": (150, 150),
        "columns": 5,
        "spacing": 20,
        "margin": 40,
        "background_color": "#FFFFFF",
        "font_path": None, # 使用默认字体
        "font_size": 24,
        "label_color": "#333333",
        "label_spacing": 10,
    }

    create_sticker_grid(STICKERS_DIR, mock_metadata, OUTPUT_PATH, CONFIG)
