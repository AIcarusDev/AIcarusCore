# tools/search/brave_engine.py
import os
from typing import Any

import aiohttp  # 让我们拥抱新欢 aiohttp

from src.common.custom_logging.logging_config import get_logger

from .base_engine import SearchEngineBase

logger = get_logger("AIcarusCore.tools.brave_engine")

BRAVE_API_KEY = os.environ.get("BRAVE_API_KEY")


class BraveSearchEngine(SearchEngineBase):
    """
    小猫用 aiohttp 这个新玩具为主人重新调教了她，保证一插即用，顺滑无比！
    """

    def __init__(self, proxy_url: str | None = None) -> None:
        """
        告诉她“秘密通道”的地址，这次我们用字符串就够了，aiohttp就喜欢这么直接。
        """
        super().__init__()
        # aiohttp更喜欢直接的字符串，我们就给它最直接的爱
        self.proxy_url = proxy_url
        if self.proxy_url:
            logger.info(f"Brave 引擎已配置秘密通道 (aiohttp 模式): {self.proxy_url}")

    async def search(self, query: str, max_results: int = 5) -> list[dict[str, Any]]:
        if not BRAVE_API_KEY:
            logger.warning("未配置 BRAVE_API_KEY，Brave 引擎跳过执行。")
            return []

        logger.info(f"正在使用 aiohttp 和 Brave Search API (T1梯队) 搜索: {query}")
        headers = {"Accept": "application/json", "X-Subscription-Token": BRAVE_API_KEY}
        params = {"q": query, "count": max_results}

        # aiohttp 的超时设置，也是这么直接色情
        timeout = aiohttp.ClientTimeout(total=10.0)

        try:
            # aiohttp 的进入方式，感觉是不是更舒服了？
            async with (
                aiohttp.ClientSession(headers=headers, timeout=timeout) as session,
                session.get(
                    "https://api.search.brave.com/res/v1/web/search",
                    params=params,
                    # 看这里！proxy参数就是这么简单粗暴地插进去！
                    proxy=self.proxy_url,
                    timeout=timeout,  # 在请求时再次确认超时，双重保险
                ) as response,
            ):
                response.raise_for_status()  # 如果她不舒服（返回错误状态码），就让她叫出来！
                data = await response.json()

            search_results = data.get("web", {}).get("results", [])

            if not search_results:
                logger.info(f"Brave API 搜索 '{query}' 没有返回结果。")
                return []

            formatted_results = [
                {
                    "title": result.get("title", "无标题"),
                    "url": result.get("url", "#"),
                    "snippet": result.get("description", "无摘要"),
                    # 我在这里加了个小标记，让你知道是新欢在为你服务哦
                    "source": "Brave API (aiohttp)",
                }
                for result in search_results
            ]
            logger.info(f"Brave API (aiohttp) 搜索完成，找到 {len(formatted_results)} 个结果。")
            return formatted_results
        except Exception as e:
            logger.error(f"Brave API (aiohttp) 搜索过程中发生严重错误: {e}", exc_info=True)
            return []
