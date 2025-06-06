# tools/search/search_service.py
import asyncio
from typing import List, Dict, Any

from src.common.custom_logging.logger_manager import get_logger
from .ddg_engine import DuckDuckGoEngine
from .brave_engine import BraveSearchEngine

logger = get_logger("AIcarusCore.tools.search_service")

class SearchService:
    """
    这就是我们的后宫总管，负责调度所有搜索引擎。
    它会先让 T1 梯队的正宫们上，如果她们不行，再让 T2 梯队的野花们上。
    """
    def __init__(self):
        # 初始化我们的后宫团
        self.t1_engines = [BraveSearchEngine()]
        self.t2_engines = [DuckDuckGoEngine()]

    async def search(self, query: str, max_results: int = 5) -> List[Dict[str, Any]]:
        logger.info(f"搜索服务收到请求: {query}")
        
        # 优先尝试 T1 引擎
        try:
            t1_tasks = [engine.search(query, max_results) for engine in self.t1_engines]
            # asyncio.gather 可以让她们一起上！
            t1_results_list = await asyncio.gather(*t1_tasks, return_exceptions=True)
            
            # 处理 T1 结果
            for results in t1_results_list:
                if isinstance(results, list) and results:
                    logger.info("T1 引擎成功返回结果，流程结束。")
                    return self._deduplicate(results) # 去重一下，更干净

        except Exception as e:
            logger.error(f"调用 T1 引擎时发生未知错误: {e}", exc_info=True)

        logger.warning("所有 T1 引擎均未能返回有效结果，启动 T2 梯队作为备用。")
        
        # T1 不行了，轮到 T2 上场
        try:
            t2_tasks = [engine.search(query, max_results) for engine in self.t2_engines]
            t2_results_list = await asyncio.gather(*t2_tasks, return_exceptions=True)

            for results in t2_results_list:
                if isinstance(results, list) and results:
                    logger.info("T2 引擎成功返回结果。")
                    return self._deduplicate(results)

        except Exception as e:
            logger.error(f"调用 T2 引擎时发生未知错误: {e}", exc_info=True)

        logger.error(f"所有搜索引擎都未能完成对 '{query}' 的搜索。")
        return []

    def _deduplicate(self, results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        # 简单的基于 url 去重
        seen_urls = set()
        unique_results = []
        for result in results:
            if result['url'] not in seen_urls:
                unique_results.append(result)
                seen_urls.add(result['url'])
        return unique_results

# 创建一个单例，这样整个应用都能共享这一个后宫总管
search_service_instance = SearchService()