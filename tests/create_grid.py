import math
import os

from PIL import Image, ImageDraw, ImageFont


def create_image_grid(input_dir, output_path, config) -> None:  # noqa: ANN001
    """将指定目录中的图片生成带数字标签的网格缩略图.

    :param input_dir: 包含源图片的目录路径。
    :param output_path: 输出图片的保存路径。
    :param config: 包含各种配置选项的字典。
    """
    # --- 1. 获取并筛选图片文件 ---
    try:
        # 获取所有文件名并排序，确保每次执行顺序一致
        filenames = sorted(os.listdir(input_dir))
        # 只保留常见的图片格式文件
        image_paths = [
            os.path.join(input_dir, f) for f in filenames
            if f.lower().endswith(('.png', '.jpg', '.jpeg', '.bmp', '.gif'))
        ]
    except FileNotFoundError:
        print(f"错误：输入目录 '{input_dir}' 不存在。请创建该目录并放入图片。")
        return

    if not image_paths:
        print(f"在目录 '{input_dir}' 中没有找到任何图片。")
        return

    # --- 2. 加载配置 ---
    thumb_size = config.get('thumbnail_size', (150, 150))
    cols = config.get('columns', 5)
    spacing = config.get('spacing', 20)
    margin = config.get('margin', 40)
    bg_color = config.get('background_color', 'white')
    font_path = config.get('font_path', None)
    font_size = config.get('font_size', 24)
    label_color = config.get('label_color', 'black')
    label_spacing = config.get('label_spacing', 10)

    # --- 3. 计算最终图片的尺寸 ---
    num_images = len(image_paths)
    num_rows = math.ceil(num_images / cols)

    cell_height = thumb_size[1] + label_spacing + font_size
    total_width = (margin * 2) + (cols * thumb_size[0]) + ((cols - 1) * spacing)
    total_height = (margin * 2) + (num_rows * cell_height) + ((num_rows - 1) * spacing)

    # --- 4. 创建画布（白板）和绘图工具 ---
    grid_image = Image.new('RGB', (total_width, total_height), color=bg_color)
    draw = ImageDraw.Draw(grid_image)

    # --- 修复部分：更稳健地加载字体 ---
    # 只有在提供了有效的字体路径时才尝试加载
    font = None
    if font_path and os.path.exists(font_path):
        try:
            font = ImageFont.truetype(font_path, font_size)
            print(f"成功加载字体: {font_path}")
        except OSError:
            print(f"警告：无法加载指定的字体文件 '{font_path}'。将使用默认字体。")
            font = ImageFont.load_default()
    else:
        # 如果 font_path 为 None 或路径不存在，直接使用默认字体
        print("警告：未提供有效字体路径，将使用Pillow的默认字体。")
        font = ImageFont.load_default()
    # --- 修复结束 ---

    # --- 5. 遍历图片、创建缩略图并粘贴到画布上 ---
    print(f"开始处理 {num_images} 张图片...")
    for i, path in enumerate(image_paths):
        row = i // cols
        col = i % cols
        x = margin + col * (thumb_size[0] + spacing)
        y = margin + row * (cell_height + spacing)

        with Image.open(path) as img:
            img.thumbnail(thumb_size, Image.Resampling.LANCZOS)
            thumb_img = Image.new('RGB', thumb_size, bg_color)
            paste_pos = ((thumb_size[0] - img.width) // 2, (thumb_size[1] - img.height) // 2)
            thumb_img.paste(img, paste_pos, img if img.mode == 'RGBA' else None)
            grid_image.paste(thumb_img, (x, y))

        # --- 6. 在缩略图下方添加数字标签 ---
        label_text = str(i + 1)
        text_x = x + thumb_size[0] / 2
        text_y = y + thumb_size[1] + label_spacing
        draw.text((text_x, text_y), label_text, fill=label_color, font=font, anchor='mt')
        print(f"  已添加第 {i+1} 张图片：{os.path.basename(path)}")

    # --- 7. 保存最终的图片 ---
    grid_image.save(output_path)
    print(f"\n成功！网格图片已保存到: {output_path}")

if __name__ == '__main__':
    # --- 用户配置区 ---
    INPUT_DIRECTORY = "images"
    OUTPUT_FILENAME = "image_grid_with_labels.jpg"

    CONFIG = {
        'thumbnail_size': (200, 200), # 缩略图大小
        'columns': 6, # 列数
        'spacing': 25, # 间距
        'margin': 50, # 边距
        'background_color': '#FFFFFF', # 背景色
        'font_path': None,  # 设置为 None 或一个无效路径会使用默认字体
        'font_size': 50, # 字体大小
        'label_color': '#333333', # 标签颜色
        'label_spacing': 15 # 标签与缩略图之间的间距
    }

    if not os.path.exists(INPUT_DIRECTORY):
        os.makedirs(INPUT_DIRECTORY)
        print(f"已创建目录 '{INPUT_DIRECTORY}'。请将你的图片文件放入此目录中，然后重新运行脚本。")
    else:
        create_image_grid(INPUT_DIRECTORY, OUTPUT_FILENAME, CONFIG)

