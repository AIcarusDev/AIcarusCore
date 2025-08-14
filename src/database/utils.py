from loguru import logger


def compare_phashes(hash1: str, hash2: str, tolerance: int = 5) -> bool:
    """比较两个感知哈希字符串的汉明距离.

    此函数会验证输入，确保两个哈希值都是长度相等的有效十六进制字符串。
    如果验证失败，将返回 False。

    Args:
        hash1 (str): 第一个哈希值.
        hash2 (str): 第二个哈希值.
        tolerance (int): 相似度容忍度。汉明距离小于等于此值被认为是相似图片.
                        默认值 5 是一个比较常用的阈值.

    Returns:
        bool: 如果图片相似则返回 True, 否则返回 False.
    """
    if len(hash1) != len(hash2):
        logger.warning(f"pHash 比较失败：哈希长度不相等 ({len(hash1)} vs {len(hash2)})。")
        return False

    try:
        h1 = int(hash1, 16)
        h2 = int(hash2, 16)
    except (ValueError, TypeError):
        logger.warning(
            f"pHash 比较失败：一个或两个输入不是有效的十六进制字符串。 "
            f"hash1='{hash1}', hash2='{hash2}'。"
        )
        return False

    distance = bin(h1 ^ h2).count("1")
    return distance <= tolerance
