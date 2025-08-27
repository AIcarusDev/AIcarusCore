# 文件路径: src/mind/abilities/information_retrieval_service.py

from typing import Any

from src.common.custom_logging.logging_config import get_logger
from src.config import config
from src.prompting.templates.url_context import URL_CONTEXT_SYSTEM_PROMPT, URL_CONTEXT_USER_PROMPT
from src.prompting.templates.web_search import WEB_SEARCH_SYSTEM_PROMPT, WEB_SEARCH_USER_PROMPT
from src.services.llmrequest.llm_processor import Client as ProcessorClient

logger = get_logger(__name__)


class InformationRetrievalService:
    """代表 AI 获取外部信息的核心技能 (网页搜索、URL总结)."""

    def __init__(
        self,
        web_search_agent_client: ProcessorClient,
        url_context_agent_client: ProcessorClient,
    ) -> None:
        self.web_search_agent_client = web_search_agent_client
        self.url_context_agent_client = url_context_agent_client
        logger.info("InformationRetrievalService 已初始化。")

    def get_actions_schema(self) -> dict[str, Any]:
        """返回此服务提供的所有动作的 JSON Schema 定义."""
        return {
            "web_search": {
                "type": "object", "description": "进行一次互联网搜索，以获取外部信息。",
                "properties": {"query": {"type": "string"}, "motivation": {"type": "string"}},
                "required": ["query", "motivation"],
            },
            "summarize_url": {
                "type": "object",
                "description": "访问一个指定的网页URL，获取其中信息，需要提供网址（url）。",
                "properties": {
                    "url": {"type": "string", "description": "需要访问和总结的完整网页URL。"},
                    "motivation": {"type": "string"},
                },
                "required": ["url", "motivation"],
            },
        }

    async def web_search(self, params: dict) -> str:
        """执行网页搜索并返回结果."""
        await self.initialize_llm_clients()
        query = params.get("query")
        motivation = params.get("motivation", "没有明确动机")
        if not query or not self.web_search_agent_client:
            result_text = "动作执行失败：LLM想搜索但没提供关键词，或者搜索代理客户端未初始化。"
            logger.warning(result_text)
            return result_text
        logger.info(f"正在调用搜索代理LLM，查询: '{query}'")
        system_prompt = WEB_SEARCH_SYSTEM_PROMPT.format(bot_name=config.persona.bot_name)
        user_prompt = WEB_SEARCH_USER_PROMPT.format(query=query, motivation=motivation)
        response = await self.web_search_agent_client.make_llm_request(
            prompt=user_prompt, system_prompt=system_prompt, is_stream=False, use_google_search=True
        )
        return response.get("text", "搜索失败或未返回任何信息。")



    async def summarize_url(self, params: dict) -> str:
        """访问指定的URL并返回总结结果."""
        await self.initialize_llm_clients()
        url = params.get("url")
        motivation = params.get("motivation", "没有明确动机")
        if not url or not self.url_context_agent_client:
            result_text = (
                "动作执行失败：LLM想访问URL但没提供网址，或者URL上下文代理客户端未初始化。"
            )
            logger.warning(result_text)
            return result_text
        logger.info(f"正在调用 URL 上下文代理LLM，目标URL: '{url}'")
        system_prompt = URL_CONTEXT_SYSTEM_PROMPT
        user_prompt = URL_CONTEXT_USER_PROMPT.format(url=url, motivation=motivation)
        response = await self.url_context_agent_client.make_llm_request(
            prompt=user_prompt,
            system_prompt=system_prompt,
            is_stream=False,
            use_url_context=True,
        )
        return response.get("text", "访问URL失败或未返回任何信息。")
