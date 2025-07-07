# tools/search/search_service.py
import asyncio
import os
from typing import Any

from src.common.custom_logging.logging_config import get_logger

from .brave_engine import BraveSearchEngine
from .ddg_engine import DuckDuckGoEngine

logger = get_logger("AIcarusCore.tools.search_service")


class SearchService:
    """后宫总管现在手握“秘密通道”的钥匙，权力更大了!"""

    def __init__(self) -> None:
        # 读取“秘密通道”的配置
        proxy_host = os.environ.get("PROXY_HOST")
        proxy_port = os.environ.get("PROXY_PORT")

        # 组装成 httpx 和 ddgs 都认识的格式
        proxy_url = None
        if proxy_host and proxy_port:
            # 哥哥你可以根据需要改成 socks5:// 等
            proxy_url = f"http://{proxy_host}:{proxy_port}"
            logger.info(f"检测到代理配置，将为指定引擎启用: {proxy_url}")

        # 【小猫的性感修改】
        # 初始化我们的后宫团，并把钥匙交给所有需要它的美人！
        # 这样才能保证 Brave 小美人也能走“秘密通道”呀~
        self.t1_engines = [BraveSearchEngine(proxy_url=proxy_url)]

        # DDG 是我们重点关照对象，把钥匙也给她！
        self.t2_engines = [DuckDuckGoEngine(proxies=proxy_url)]

    async def search(self, query: str, max_results: int = 5) -> list[dict[str, Any]]:
        """执行搜索操作，返回结果列表.

        Args:
            query: 搜索关键词。
            max_results: 返回的最大结果数量。
        Returns:
            搜索结果列表。
        """
        logger.info(f"搜索服务收到请求: {query}")

        # 优先尝试 T1 引擎
        try:
            # 【小猫的性感修改】
            # 让两个梯队并发执行，谁先爽到就用谁的结果！这叫“双龙入洞”，效率最高！
            tasks = []
            if self.t1_engines:
                tasks.extend([engine.search(query, max_results) for engine in self.t1_engines])

            # 只有在T1引擎都挂了或者没结果的时候，才去麻烦T2小妹妹
            t1_results_list = await asyncio.gather(*tasks, return_exceptions=True)

            # 先检查T1的结果
            valid_results = []
            for result in t1_results_list:
                if isinstance(result, list) and result:
                    valid_results.extend(result)

            if valid_results:
                logger.info("T1 引擎成功返回结果，流程结束。")
                return self._deduplicate(valid_results)

        except Exception as e:
            logger.error(f"调用 T1 引擎时发生未知错误: {e}", exc_info=True)

        logger.warning("所有 T1 引擎均未能返回有效结果，启动 T2 梯队作为备用。")

        # T2
        try:
            t2_tasks = [engine.search(query, max_results) for engine in self.t2_engines]
            t2_results_list = await asyncio.gather(*t2_tasks, return_exceptions=True)

            valid_results = []
            for results in t2_results_list:
                if isinstance(results, list) and results:
                    valid_results.extend(results)

            if valid_results:
                logger.info("T2 引擎成功返回结果。")
                return self._deduplicate(valid_results)

        except Exception as e:
            logger.error(f"调用 T2 引擎时发生未知错误: {e}", exc_info=True)

        logger.error(f"所有搜索引擎都未能完成对 '{query}' 的搜索。")
        return []

    def _deduplicate(self, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen_urls = set()
        unique_results = []
        for result in results:
            # 有些骚货可能没有url，我们要处理一下
            if result.get("url") and result["url"] not in seen_urls:
                unique_results.append(result)
                seen_urls.add(result["url"])
        return unique_results


# 保持单例，让这个后宫总管全局唯一
search_service_instance = SearchService()
