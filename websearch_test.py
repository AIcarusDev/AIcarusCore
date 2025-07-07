import asyncio

from src.common.custom_logging.logging_config import get_logger
from src.tools.web_searcher import search_web

logger = get_logger("web_searcher.test")

if __name__ == "__main__":

    async def main_test() -> None:
        test_queries = ["什么是AIcarus项目？", "今天新加坡的天气怎么样？"]  # 定义测试查询列表
        for q in test_queries:  # 遍历查询列表
            logger.info(f"\n--- 测试搜索: {q} ---")  # 记录当前查询
            results = await search_web(q)  # 执行网络搜索
            for res in results:  # 遍历搜索结果
                print(f"  - 标题: {res['title']}")  # 打印结果标题
                print(f"    链接: {res['url']}")  # 打印结果链接
                print(f"    摘要: {res['snippet'][:100]}...")  # 打印结果摘要（显示前100个字符）
            logger.info("--------------------")  # 记录查询结束

    asyncio.run(main_test())
