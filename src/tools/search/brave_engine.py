# tools/search/brave_engine.py
import httpx
import os
from typing import Any, List, Dict

from src.common.custom_logging.logger_manager import get_logger
from .base_engine import SearchEngineBase

logger = get_logger("AIcarusCore.tools.brave_engine")

# 建议把 API Key 存在环境变量里，更安全哦
BRAVE_API_KEY = os.environ.get("BRAVE_API_KEY")

class BraveSearchEngine(SearchEngineBase):
    """
    这是我们可靠的 Brave 引擎，作为 T1 主力。
    她有官方身份（API），活好不粘人，稳定输出。
    """
    async def search(self, query: str, max_results: int = 5) -> List[Dict[str, Any]]:
        if not BRAVE_API_KEY:
            logger.warning("未配置 BRAVE_API_KEY，Brave 引擎跳过执行。")
            return []
            
        logger.info(f"正在使用 Brave Search API (T1梯队) 搜索: {query}")
        headers = {
            "Accept": "application/json",
            "X-Subscription-Token": BRAVE_API_KEY,
        }
        params = {"q": query, "count": max_results}

        try:
            async with httpx.AsyncClient() as client:
                response = await client.get("https://api.search.brave.com/res/v1/web/search", headers=headers, params=params, timeout=10.0)
                response.raise_for_status() # 如果状态码不是 2xx，会抛出异常
            
            data = response.json()
            search_results = data.get("web", {}).get("results", [])

            if not search_results:
                logger.info(f"Brave API 搜索 '{query}' 没有返回结果。")
                return []

            formatted_results = [
                {
                    "title": result.get("title", "无标题"),
                    "url": result.get("url", "#"),
                    "snippet": result.get("description", "无摘要"),
                    "source": "Brave API"
                }
                for result in search_results
            ]
            logger.info(f"Brave API 搜索完成，找到 {len(formatted_results)} 个结果。")
            return formatted_results
        except httpx.HTTPStatusError as e:
            logger.error(f"Brave API 请求失败，状态码: {e.response.status_code}, 内容: {e.response.text}")
            return []
        except Exception as e:
            logger.error(f"Brave API 搜索过程中发生严重错误: {e}", exc_info=True)
            return []