# tools/web_searcher.py
from typing import Any

from src.common.custom_logging.logging_config import get_logger

from .search.search_service import search_service_instance  # 导入搜索服务实例

logger = get_logger(__name__)


async def search_web(query: str, max_results: int = 5) -> list[dict[str, Any]]:
    """使用系统集成的搜索服务执行网络搜索操作.

    本函数作为搜索功能的入口接口，负责请求分发和结果返回
    """
    logger.info(f"接收到搜索请求，已转发至搜索服务处理: {query}")

    # 直接把活儿交给我们的后宫总管
    results = await search_service_instance.search(query, max_results)

    # 可以在这里对最终结果做最后的处理，或者直接返回
    return results
