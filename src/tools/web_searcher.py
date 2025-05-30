import asyncio

from duckduckgo_search import DDGS


async def search_web(query: str, max_results: int = 3) -> str:
    """
    Performs an asynchronous web search using DuckDuckGo and returns a summary of results.
    Args:
        query (str): The search query.
        max_results (int): The maximum number of results to return.
    Returns:
        str: A summary of search results or an error message.
    """
    print(f"[WebSearcher] 异步搜索 (DuckDuckGo): '{query}' (最多 {max_results} 条结果)")
    try:
        # DDGS().text() is synchronous, so we run it in an executor to avoid blocking asyncio event loop.
        # Note: For production, consider a dedicated thread pool or process pool if many searches run concurrently.
        loop = asyncio.get_running_loop()

        # The DDGS() context manager should be used if making multiple calls with the same instance,
        # but for a single call, instantiating directly is fine.
        # results = await loop.run_in_executor(
        #     None,  # Uses the default ThreadPoolExecutor
        #     lambda: DDGS(timeout=10).text(query, max_results=max_results)
        # )
        # Simpler way for single call, DDGS context manager handles session internally
        with DDGS(timeout=10) as ddgs:  # timeout for the search request itself
            results = await loop.run_in_executor(None, lambda: ddgs.text(query, max_results=max_results))

        if results:
            summary = f"关于 '{query}' 的DuckDuckGo搜索结果摘要:\n"
            for i, r in enumerate(results):
                title = r.get("title", "N/A")
                body = r.get("body", "N/A")
                # href = r.get('href', '#') # Link, if needed in future
                summary += f"{i + 1}. {title}: {body[:200]}...\n"  # Limit snippet length
            print(f"[WebSearcher] 找到 {len(results)} 条结果 for '{query}'")
            return summary
        else:
            print(f"[WebSearcher] 未找到关于 '{query}' 的结果。")
            return f"未能通过DuckDuckGo找到关于 '{query}' 的相关信息。"
    except Exception as e:
        print(f"[WebSearcher] 搜索 '{query}' 时发生错误: {e}")
        import traceback

        traceback.print_exc()  # Print full traceback for debugging
        return f"网络搜索(DuckDuckGo)时出错: {e}"


if __name__ == "__main__":

    async def main_test() -> None:
        test_queries = ["什么是量子计算机?", "今天北京的天气怎么样？", "一个不存在的随机词汇"]
        for q in test_queries:
            print(f"\n--- 测试搜索: {q} ---")
            result = await search_web(q)
            print(result)
            print("--------------------")

    asyncio.run(main_test())
