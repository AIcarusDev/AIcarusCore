# tools/search/ddg_engine.py
import asyncio
from typing import Any, List, Dict, Optional
from duckduckgo_search import DDGS

from src.common.custom_logging.logger_manager import get_logger
from .base_engine import SearchEngineBase

logger = get_logger("AIcarusCore.tools.ddg_engine")

class DuckDuckGoEngine(SearchEngineBase):
    """
    这位妹妹现在学会了走“秘密通道”哦～
    """
    def __init__(self, proxies: Optional[str] = None):
        """
        在创建她的时候，就告诉她“秘密通道”的地址。
        """
        super().__init__()
        # 这里我们把 proxies 改成 proxy，让她开心
        self.proxy = proxies 
        if self.proxy:
            logger.info(f"DuckDuckGo 引擎已配置秘密通道: {self.proxy}")

    async def search(self, query: str, max_results: int = 5) -> List[Dict[str, Any]]:
        logger.info(f"正在使用 DuckDuckGo (T2梯队) 搜索: {query}")
        try:
            # 关键的修改在这里！把 proxies=... 改成 proxy=...
            ddgs = DDGS(proxy=self.proxy) 
            
            search_results = await asyncio.to_thread(
                ddgs.text, keywords=query, max_results=max_results
            )
            
            if not search_results:
                logger.warning(f"DuckDuckGo 搜索 '{query}' 没有返回结果。")
                return []

            formatted_results = [
                {
                    "title": result.get("title", "无标题"),
                    "url": result.get("href", "#"),
                    "snippet": result.get("body", "无摘要"),
                    "source": "DuckDuckGo"
                }
                for result in search_results
            ]
            logger.info(f"DuckDuckGo 搜索完成，找到 {len(formatted_results)} 个结果。")
            return formatted_results
        except Exception as e:
            logger.error(f"DuckDuckGo 搜索过程中发生严重错误: {e}", exc_info=True)
            return []