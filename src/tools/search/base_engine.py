# tools/search/base_engine.py
from abc import ABC, abstractmethod
from typing import List, Dict, Any

class SearchEngineBase(ABC):
    """
    所有搜索引擎都要继承的基类，定义了统一的“插入”姿势。
    """
    @abstractmethod
    async def search(self, query: str, max_results: int = 5) -> List[Dict[str, Any]]:
        """
        这个 search 方法就是每个引擎的小穴，我们都要从这里“进入”。
        """
        pass