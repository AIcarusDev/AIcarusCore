# src/observation/summarization_service.py
from typing import Any  # 确保Tuple被导入

from src.common.custom_logging.logger_manager import get_logger
from src.config import config  # 假设用于获取AI人设
from src.llmrequest.llm_processor import Client as LLMProcessorClient  # 假设LLM客户端路径

# logger 定义移到文件顶部，确保在任何使用前都已定义
logger = get_logger("AIcarusCore.observation.SummarizationService")

# tiktoken 相关的导入和逻辑已根据用户指示移除，因为渐进式总结不再依赖精确Token计数


def _extract_text_from_dict_content(content: list[dict[str, Any]]) -> str:
    """
    从 content (Seg 字典列表) 中安全地提取所有文本内容。
    """
    text_parts = []
    if not isinstance(content, list):
        return ""
    for seg in content:
        if isinstance(seg, dict) and seg.get("type") == "text":
            data = seg.get("data", {})
            if isinstance(data, dict) and "text" in data:
                text_parts.append(str(data["text"]))  # 确保是字符串
    return "".join(text_parts)


class SummarizationService:
    """
    服务类，负责对会话历史进行总结，生成第一人称的回忆录。
    """

    def __init__(self, llm_client: LLMProcessorClient) -> None:  # 需要一个LLM客户端实例
        self.llm_client = llm_client
        self.logger = logger

    def _format_events_to_text(self, events: list[dict[str, Any]]) -> str:
        """
        将事件列表格式化为纯文本对话历史。
        """
        conversation_texts: list[str] = []
        for event_doc in events:  # 假设 events 是按时间顺序排列的
            event_type = event_doc.get("event_type", "")

            # 跳过非字典类型的事件，增加健壮性
            if not isinstance(event_doc, dict):
                self.logger.warning(f"发现一个非字典类型的事件，已跳过: {type(event_doc)}")
                continue

            if event_type.startswith("message."):
                user_nickname = event_doc.get("user_info", {}).get("user_nickname", "未知用户")
                raw_content_segs = event_doc.get("content", [])
                text_content = _extract_text_from_dict_content(raw_content_segs)
                if not text_content and isinstance(raw_content_segs, list):
                    # 确保 raw_content_segs 是列表才进行迭代
                    if any(isinstance(seg, dict) and seg.get("type") == "image" for seg in raw_content_segs):
                        text_content = "[图片]"
                    elif any(isinstance(seg, dict) and seg.get("type") == "face" for seg in raw_content_segs):
                        text_content = "[表情]"
                    elif any(isinstance(seg, dict) and seg.get("type") == "file" for seg in raw_content_segs):
                        text_content = "[文件]"
                if text_content:
                    conversation_texts.append(f"{user_nickname}: {text_content.strip()}")

            elif event_type == "internal.sub_consciousness.thought_log":
                user_nickname = event_doc.get("user_info", {}).get("user_nickname", config.persona.bot_name or "我")
                raw_content_segs = event_doc.get("content", [])
                text_content = _extract_text_from_dict_content(raw_content_segs)
                if text_content:
                    conversation_texts.append(f"({user_nickname}的内心想法): {text_content.strip()}")

            elif event_type == "action.message.send":
                # 让AI自己发送的消息也能被总结，这样上下文更完整
                user_nickname = config.persona.bot_name or "我"
                raw_content_segs = event_doc.get("content", [])
                text_content = _extract_text_from_dict_content(raw_content_segs)
                if text_content:
                    conversation_texts.append(f"{user_nickname}: {text_content.strip()}")

        return "\\n".join(conversation_texts)

    async def _build_consolidation_prompt(
        self, previous_summary: str | None, recent_dialogue_text: str
    ) -> tuple[str, str]:
        """
        构建用于整合摘要的 System Prompt 和 User Prompt。
        这个Prompt的核心是确保在融入新内容时，不丢失旧摘要的全局上下文和关键信息。
        """
        persona_config = config.persona
        system_prompt_parts = [
            f"你是{persona_config.bot_name}，正在以第一人称视角撰写一份持续更新的聊天记录总结。",
            persona_config.description or "",
            persona_config.profile or "",
            "你的核心任务是：将一段“最新的对话内容”无缝地整合进“已有的记录总结”中，生成一份更新后的、连贯的完整聊天记录总结。",
            "不用在记录总结中加入关于你自身的、未在对话中出现的描述。你的任务是总结和串联对话事件。"
            "这非常重要：更新后的记录总结必须保留所有旧回忆录中的关键信息、情感转折和重要决策。不能因为有了新内容就忘记或丢弃旧的重点",
            "如果已总结的内容已经非常长，可以适当的删减一些你觉得不重要的部分",
            "你要将新的情节自然地融入到已有的总结中，而不是简单地把新内容附加在末尾。",
            "最终的成品应该是一份流畅、完整、独立的个人回忆，而不是一份摘要列表。",
            "请确保输出的只是更新后的聊天记录总结本身，不要包含任何额外的解释或标题。",
        ]
        system_prompt = "\\n".join(filter(None, system_prompt_parts))

        user_prompt_parts = []
        if previous_summary and previous_summary.strip():
            user_prompt_parts.append(f"这是之前总结过的，已有的记录总结：\n---\n{previous_summary}\n---")

        user_prompt_parts.append(f"这是刚刚发生的最新对话：\n---\n{recent_dialogue_text}\n---")
        user_prompt_parts.append("请将最新的对话内容，整合进已有的聊天记录总结中，输出一份更新后的、完整的版本。")
        user_prompt = "\n\n".join(user_prompt_parts)

        return system_prompt, user_prompt

    async def consolidate_summary(self, previous_summary: str | None, recent_events: list[dict[str, Any]]) -> str:
        """
        对提供的最近事件列表进行总结，并将其整合进之前的摘要中，形成一个更全面的新摘要。
        这个方法旨在通过高质量的prompt，实现“增量式”操作，但获得“全局性”的结果。
        :param previous_summary: 上一轮的总结摘要，如果是第一次总结则为 None。
        :param recent_events: 最近发生的事件列表。
        :return: 新的、更新后的第一人称总结文本。
        """
        self.logger.debug(
            f"开始整合摘要。之前摘要是否存在: {'是' if previous_summary else '否'}, 新事件数: {len(recent_events)}"
        )

        if not recent_events:
            self.logger.info("没有新的事件，直接返回之前的摘要。")
            return previous_summary

        recent_dialogue_text = self._format_events_to_text(recent_events)
        if not recent_dialogue_text.strip():
            self.logger.info("最近事件未能提取出有效文本内容，直接返回之前的摘要。")
            return previous_summary

        system_prompt, user_prompt = await self._build_consolidation_prompt(previous_summary, recent_dialogue_text)

        try:
            self.logger.debug(
                f"调用LLM进行摘要整合。System Prompt (部分): {system_prompt[:100]}... User Prompt (部分): {user_prompt[:200]}..."
            )
            response_data = await self.llm_client.make_llm_request(
                prompt=user_prompt, system_prompt=system_prompt, is_stream=False
            )

            if response_data and not response_data.get("error"):
                new_summary_text = response_data.get("text", "").strip()
                if new_summary_text:
                    self.logger.info(f"成功生成整合后的摘要 (部分): {new_summary_text[:100]}...")
                    return new_summary_text
                else:
                    self.logger.warning("LLM为摘要整合返回了空内容。将保留之前的摘要（如果存在）。")
                    return previous_summary or "我努力回忆了一下，但脑子一片空白，什么也没想起来。"
            else:
                error_msg = response_data.get("message", "未知错误") if response_data else "LLM无响应"
                self.logger.error(f"调用LLM进行摘要整合失败: {error_msg}。将保留之前的摘要（如果存在）。")
                return previous_summary or f"我试图更新我的回忆，但是失败了（错误: {error_msg}）。"

        except Exception as e:
            self.logger.error(f"生成整合摘要时发生意外错误: {e}", exc_info=True)
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
