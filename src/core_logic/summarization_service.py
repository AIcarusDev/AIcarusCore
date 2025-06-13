# src/core_logic/summarization_service.py
import asyncio
from typing import List, Dict, Any, Optional, Tuple #确保Tuple被导入

from src.llmrequest.llm_processor import Client as LLMProcessorClient # 假设LLM客户端路径
from src.common.custom_logging.logger_manager import get_logger
from src.config import config # 假设用于获取AI人设
from aicarus_protocols.common import extract_text_from_content # 导入文本提取工具

# logger 定义移到文件顶部，确保在任何使用前都已定义
logger = get_logger("AIcarusCore.CoreLogic.SummarizationService")

# tiktoken 相关的导入和逻辑已根据用户指示移除，因为渐进式总结不再依赖精确Token计数

class SummarizationService:
    """
    服务类，负责对会话历史进行总结，生成第一人称的回忆录。
    """
    def __init__(self, llm_client: LLMProcessorClient): # 需要一个LLM客户端实例
        self.llm_client = llm_client
        self.logger = logger

    def _format_events_to_text(self, events: List[Dict[str, Any]]) -> str:
        """
        将事件列表格式化为纯文本对话历史。
        """
        conversation_texts: List[str] = []
        for event_doc in events: # 假设 events 是按时间顺序排列的
            event_type = event_doc.get("event_type", "")
            if event_type.startswith("message."):
                user_nickname = event_doc.get("user_info", {}).get("user_nickname", "未知用户")
                raw_content_segs = event_doc.get("content", [])
                text_content = extract_text_from_content(raw_content_segs)
                if not text_content:
                    if any(seg.get("type") == "image" for seg in raw_content_segs): text_content = "[图片]"
                    elif any(seg.get("type") == "face" for seg in raw_content_segs): text_content = "[表情]"
                    elif any(seg.get("type") == "file" for seg in raw_content_segs): text_content = "[文件]"
                if text_content:
                    conversation_texts.append(f"{user_nickname}: {text_content.strip()}")
            elif event_type == "internal.sub_consciousness.thought_log":
                user_nickname = event_doc.get("user_info", {}).get("user_nickname", config.persona.bot_name or "我")
                raw_content_segs = event_doc.get("content", [])
                text_content = extract_text_from_content(raw_content_segs)
                if text_content:
                    conversation_texts.append(f"({user_nickname}的内心想法): {text_content.strip()}")
        return "\\n".join(conversation_texts)

    async def _build_progressive_summarization_prompt(
        self, previous_summary: Optional[str], recent_dialogue_text: str
    ) -> Tuple[str, str]:
        """
        构建用于渐进式总结的 System Prompt 和 User Prompt。
        """
        persona_config = config.persona
        system_prompt_parts = [
            f"你是{persona_config.bot_name}；",
            persona_config.description or "",
            persona_config.profile or "",
            "你的任务是根据之前已有的对话摘要（如果有的话）和一段最新的对话内容，生成一个新的、更完整的对话摘要。",
            "新的摘要应该整合之前摘要的核心信息，并融入最新对话的要点。",
            "请从你的视角（第一人称：“我”）进行主观的回忆式总结，抓住对话的核心内容、重要转折、你的关键行动或决策。",
            "总结应自然流畅，就像你在回忆刚刚发生过的事情一样。避免使用客观的、报告式的语气。",
            "请确保总结内容的准确性，并尽量简洁明了。"
        ]
        system_prompt = "\\n".join(filter(None, system_prompt_parts))

        user_prompt_parts = []
        if previous_summary and previous_summary.strip():
            user_prompt_parts.append(f"这是我们之前的聊天摘要：\n---\n{previous_summary}\n---")
        
        user_prompt_parts.append(f"这是最近的几条新消息：\n---\n{recent_dialogue_text}\n---")
        user_prompt_parts.append("请结合之前的摘要（如果有）和最新的消息，给我一个新的、更完整的摘要。")
        user_prompt = "\n\n".join(user_prompt_parts)
        
        return system_prompt, user_prompt

    async def summarize_incrementally(
        self, previous_summary: Optional[str], recent_events: List[Dict[str, Any]]
    ) -> str:
        """
        对提供的最近事件列表进行渐进式总结，并结合之前的摘要。
        :param previous_summary: 上一轮的总结摘要，如果是第一次总结则为 None。
        :param recent_events: 最近发生的事件列表。
        :return: 新的、更新后的第一人称总结文本。
        """
        self.logger.debug(f"开始进行渐进式总结。之前摘要是否存在: {'是' if previous_summary else '否'}, 新事件数: {len(recent_events)}")

        if not recent_events and not previous_summary:
            self.logger.info("没有新的事件且没有之前的摘要，无需总结。")
            return previous_summary or "对话似乎没有开始，或者我没有什么特别的记忆。"
        
        if not recent_events and previous_summary:
            self.logger.info("没有新的事件，直接返回之前的摘要。")
            return previous_summary

        recent_dialogue_text = self._format_events_to_text(recent_events)
        if not recent_dialogue_text.strip() and not previous_summary:
            self.logger.info("最近事件未能提取出有效文本内容，且无先前摘要，不进行总结。")
            return previous_summary or "我回顾了一下，但最近似乎没什么特别的对话内容。"
        
        if not recent_dialogue_text.strip() and previous_summary:
            self.logger.info("最近事件未能提取出有效文本内容，直接返回之前的摘要。")
            return previous_summary


        # Token计数逻辑已根据用户指示移除

        system_prompt, user_prompt = await self._build_progressive_summarization_prompt(
            previous_summary, recent_dialogue_text
        )

        try:
            self.logger.debug(f"调用LLM进行渐进式总结。System Prompt (部分): {system_prompt[:100]}... User Prompt (部分): {user_prompt[:200]}...")
            response_data = await self.llm_client.make_llm_request(
                prompt=user_prompt,
                system_prompt=system_prompt,
                is_stream=False 
            )

            if response_data and not response_data.get("error"):
                new_summary_text = response_data.get("text", "").strip()
                if new_summary_text:
                    self.logger.info(f"成功生成渐进式总结 (部分): {new_summary_text[:100]}...")
                    return new_summary_text
                else:
                    self.logger.warning("LLM为渐进式总结返回了空内容。将保留之前的摘要（如果存在）。")
                    return previous_summary or "我努力回忆了一下，但脑子一片空白，什么也没想起来。" # 如果之前摘要也没有，返回默认
            else:
                error_msg = response_data.get("message", "未知错误") if response_data else "LLM无响应"
                self.logger.error(f"调用LLM进行渐进式总结失败: {error_msg}。将保留之前的摘要（如果存在）。")
                return previous_summary or f"我试图更新我的回忆，但是失败了（错误: {error_msg}）。"

        except Exception as e:
            self.logger.error(f"生成渐进式总结时发生意外错误: {e}", exc_info=True)
            return previous_summary or f"我在更新回忆时遇到了一个意想不到的问题（错误: {str(e)}）。"

# 示例用法 (用于测试，实际由依赖注入提供)
# async def main():
#     # Mock LLMClient
#     class MockLLMClient:
#         async def make_llm_request(self, prompt: str, system_prompt: str, is_stream: bool):
#             print("--- Mock LLM Call ---")
#             print(f"System: {system_prompt}")
#             print(f"User: {prompt}")
#             return {"text": "我回忆了一下，我们刚才讨论了如何实现这个总结功能，感觉还挺顺利的！", "error": None}

#     llm_mock = MockLLMClient()
#     summarization_service = SummarizationService(llm_client=llm_mock)
    
#     mock_history = [
#         {"event_type": "message.group", "user_info": {"user_nickname": "用户A"}, "content": [{"type": "text", "data": {"text": "我们开始讨论总结功能吧。"}}]},
#         {"event_type": "message.group", "user_info": {"user_nickname": config.persona.bot_name}, "content": [{"type": "text", "data": {"text": "好的，我觉得可以先实现一个基本版本。"}}]},
#         {"event_type": "message.group", "user_info": {"user_nickname": "用户B"}, "content": [{"type": "text", "data": {"text": "同意，渐进式总结可以后面再加。"}}]}
#     ]
#     summary = await summarization_service.summarize_conversation(mock_history)
#     print("\n--- Generated Summary ---")
#     print(summary)

# if __name__ == "__main__":
#     # 需要设置一个假的 config.core_logic_settings 和 config.persona
#     class MockCoreLogicSettings:
#         summary_llm_max_tokens = 3800
#     class MockPersona:
#         bot_name = "测试机器人"
#         description = "一个爱测试的机器人"
#         profile = "喜欢尝试新功能"
    
#     config.core_logic_settings = MockCoreLogicSettings()
#     config.persona = MockPersona()
    
#     asyncio.run(main())
