# tools/web_searcher.py
import asyncio
from typing import Any, List
from src.common.custom_logging.logger_manager import get_logger
from .search.search_service import search_service_instance # 导入我们的后宫总管

logger = get_logger("AIcarusCore.tools.web_searcher")

async def search_web(query: str, max_results: int = 5) -> List[dict[str, Any]]:
    """
    使用我们强大而可靠的搜索服务执行网络搜索。
    这个函数现在是高潮的入口，它只管喊开始，具体动作由 search_service 完成。
    """
    logger.info(f"工具层收到搜索请求，转交给搜索服务: {query}")
    
    # 直接把活儿交给我们的后宫总管
    results = await search_service_instance.search(query, max_results)
    
    # 可以在这里对最终结果做最后的处理，或者直接返回
    return results