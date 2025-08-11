# src/common/image_utils.py
import io

from imagehash import phash
from PIL import Image
from src.common.custom_logging.logging_config import get_logger

logger = get_logger(__name__)


def calculate_perceptual_hash(image_bytes: bytes) -> str | None:
    """计算图片的感知哈希 (pHash).

    pHash 对图片的缩放、旋转和轻微的颜色变化不敏感.
    """
    try:
        image = Image.open(io.BytesIO(image_bytes))
        # 使用 phash 算法，可以根据需要换成 dhash 或 ahash
        perceptual_hash = phash(image)
        return str(perceptual_hash)
    except Exception as e:
        logger.error(f"计算图片感知哈希时失败: {e}")
        return None


def compare_phashes(hash1: str, hash2: str, tolerance: int = 5) -> bool:
    """比较两个感知哈希字符串的汉明距离.

    Args:
        hash1 (str): 第一个哈希值.
        hash2 (str): 第二个哈希值.
        tolerance (int): 相似度容忍度。汉明距离小于等于此值被认为是相似图片.
                         默认值 5 是一个比较常用的阈值.

    Returns:
        bool: 如果图片相似则返回 True.
    """
    if len(hash1) != len(hash2):
        return False

    # 将十六进制字符串转换为整数进行汉明距离计算
    h1 = int(hash1, 16)
    h2 = int(hash2, 16)

    # 计算汉明距离
    distance = bin(h1 ^ h2).count("1")

    return distance <= tolerance
