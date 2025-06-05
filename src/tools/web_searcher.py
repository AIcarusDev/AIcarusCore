import asyncio
import traceback
from typing import Any, List
from duckduckgo_search import DDGS

from src.common.custom_logging.logger_manager import get_logger

logger = get_logger("AIcarusCore.tools.web_searcher")

# 注意：DDGS的搜索是同步的，我们需要在异步函数中用 to_thread 来运行它，避免阻塞事件循环
async def search_web(query: str, max_results: int = 5) -> List[dict[str, Any]]:
    """
    使用 DuckDuckGo 执行网络搜索。

    Args:
        query: 搜索查询。
        max_results: 最大结果数量。

    Returns:
        搜索结果列表，每个结果是一个包含 title, url, snippet 的字典。
    """
    logger.info(f"开始网络搜索: {query}")
    try:
        # 使用 to_thread 在异步环境中运行同步的搜索代码
        search_results = await asyncio.to_thread(
            DDGS().text, keywords=query, max_results=max_results
        )
        
        if not search_results:
            logger.info(f"网络搜索 '{query}' 没有返回结果。")
            return []

        # 格式化结果以符合我们的需求
        formatted_results = [
            {
                "title": result.get("title", "无标题"),
                "url": result.get("href", "#"),
                "snippet": result.get("body", "无摘要"),
            }
            for result in search_results
        ]
        
        logger.info(f"搜索完成，找到 {len(formatted_results)} 个结果。")
        return formatted_results

    except Exception as e:
        logger.error(f"网络搜索过程中发生错误: {e}")
        logger.error(f"错误详情: {traceback.format_exc()}")
        return []

if __name__ == "__main__":
    async def main_test():
        test_queries = ["什么是AIcarus项目？", "今天新加坡的天气怎么样？"]
        for q in test_queries:
            logger.info(f"\n--- 测试搜索: {q} ---")
            results = await search_web(q)
            for res in results:
                print(f"  - 标题: {res['title']}")
                print(f"    链接: {res['url']}")
                print(f"    摘要: {res['snippet'][:100]}...")
            logger.info("--------------------")

    asyncio.run(main_test())