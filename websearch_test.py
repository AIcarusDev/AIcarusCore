import asyncio
from src.tools.web_searcher import search_web
from src.common.custom_logging.logger_manager import get_logger

logger = get_logger("web_searcher.test")

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