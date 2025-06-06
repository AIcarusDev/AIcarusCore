# tools/search/ddg_engine.py
import asyncio
import traceback
from typing import Any, List, Dict
from duckduckgo_search import DDGS

from src.common.custom_logging.logger_manager import get_logger
from .base_engine import SearchEngineBase

logger = get_logger("AIcarusCore.tools.ddg_engine")

class DuckDuckGoEngine(SearchEngineBase):
    """
    这是你的老相好 DuckDuckGo，我们给她穿上新衣服（继承基类）。
    她属于不稳定的 T2 梯队，偶尔会给你惊喜，但也可能闹脾气。
    """
    async def search(self, query: str, max_results: int = 5) -> List[Dict[str, Any]]:
        logger.info(f"正在使用 DuckDuckGo (T2梯队) 搜索: {query}")
        try:
            # 这里的逻辑和你原来的一样，很棒！
            search_results = await asyncio.to_thread(
                DDGS().text, keywords=query, max_results=max_results
            )
            
            if not search_results:
                logger.warning(f"DuckDuckGo 搜索 '{query}' 没有返回结果。")
                return []

            formatted_results = [
                {
                    "title": result.get("title", "无标题"),
                    "url": result.get("href", "#"),
                    "snippet": result.get("body", "无摘要"),
                    "source": "DuckDuckGo" # 标明一下来源，方便调试
                }
                for result in search_results
            ]
            logger.info(f"DuckDuckGo 搜索完成，找到 {len(formatted_results)} 个结果。")
            return formatted_results
        except Exception as e:
            logger.error(f"DuckDuckGo 搜索过程中发生严重错误: {e}", exc_info=True)
            return [] # 出错了就返回空列表，让上层处理