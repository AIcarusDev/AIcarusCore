# src/observation/summarization_service.py
from typing import Any

from src.common.custom_logging.logger_manager import get_logger
from src.config import config
from src.llmrequest.llm_processor import Client as LLMProcessorClient

logger = get_logger("AIcarusCore.observation.SummarizationService")

class SummarizationService:
    """
    服务类，负责对会话历史进行总结，生成第一人称的回忆录。
    哼，现在我只负责动脑，脏活累活都让别人干了。
    """

    def __init__(self, llm_client: LLMProcessorClient) -> None:
        """
        初始化服务，只需要一个LLM客户端就够了，真省心。
        :param llm_client: LLMProcessorClient 的实例。
        """
        self.llm_client = llm_client
        self.logger = logger
        logger.info("SummarizationService (重构版) 已初始化。")

    def _format_events_for_summary_prompt(
        self,
        events: list[dict[str, Any]],
        user_map: dict[str, dict[str, Any]],
        bot_id: str,
    ) -> str:
        """
        将事件列表格式化为详细的“战地简报”格式，用于总结。
        :param events: 要格式化的事件文档列表。
        :param user_map: 从外部传入的用户ID到用户信息的映射。
        :param bot_id: 机器人的平台ID。
        :return: 格式化后的聊天记录字符串。
        """
        chat_log_lines: list[str] = []
        platform_id_to_uid_str = {p_id: u_info["uid_str"] for p_id, u_info in user_map.items()}

        for event_doc in events:
            if not isinstance(event_doc, dict):
                continue

            event_type = event_doc.get("event_type", "unknown")
            timestamp = event_doc.get("timestamp", 0)
            time_str = ""
            if timestamp > 0:
                from datetime import datetime
                time_str = datetime.fromtimestamp(timestamp / 1000.0).strftime("%H:%M:%S")

            user_info = event_doc.get("user_info", {})
            sender_platform_id = user_info.get("user_id") if isinstance(user_info, dict) else None
            uid_str = platform_id_to_uid_str.get(sender_platform_id, f"Unknown({str(sender_platform_id)[:4]})")

            content_text = ""
            content_segs = event_doc.get("content", [])
            if isinstance(content_segs, list):
                text_parts = []
                for seg in content_segs:
                    if isinstance(seg, dict) and seg.get("type") == "text":
                        data = seg.get("data", {})
                        if isinstance(data, dict):
                            text_parts.append(str(data.get("text", "")))
                content_text = "".join(text_parts)
            
            # 标记机器人自己的发言
            is_bot_message = (sender_platform_id == bot_id)

            log_line = ""
            if event_type.startswith("message.") or (is_bot_message and event_type == "action.message.send"):
                log_line = f"[{time_str}] {uid_str} [MSG]: {content_text} (id:{event_doc.get('_key')})"
                
                # 如果是机器人发的，并且有动机，就附加上
                motivation = event_doc.get("motivation")
                if is_bot_message and motivation and isinstance(motivation, str) and motivation.strip():
                    log_line += f"\n    - [MOTIVE]: {motivation}"

            elif event_type == "internal.focus_chat_mode.thought_log":
                log_line = f"[{time_str}] {uid_str} [MOTIVE]: {content_text}"
            
            if log_line:
                chat_log_lines.append(log_line)

        return "\n".join(chat_log_lines) if chat_log_lines else "这段时间内没有新的文本对话。"

    async def _build_summary_prompt(
        self,
        previous_summary: str | None,
        recent_events: list[dict[str, Any]],
        bot_profile: dict[str, Any],
        conversation_info: dict[str, Any],
        user_map: dict[str, dict[str, Any]],
    ) -> tuple[str, str]:
        """
        构建用于整合摘要的 System Prompt 和 User Prompt。
        这是我的新大脑，更聪明了。
        """
        # --- System Prompt ---
        persona_config = config.persona
        current_time_str = ""
        from datetime import datetime
        current_time_str = datetime.now().strftime("%Y年%m月%d日 %H点%M分%S秒")

        system_prompt = f"""
现在是{current_time_str}
你是{persona_config.bot_name}
{persona_config.description}
{persona_config.profile}
你的qq号是"{bot_profile.get('user_id', '未知')}"；
你当前正在qq群"{conversation_info.get('name', '未知群聊')}"中参与qq群聊
你在该群的群名片是"{bot_profile.get('card', persona_config.bot_name)}"
你的任务是以你的视角总结聊天记录里的内容，包括人物、事件和主要信息，不要分点，不要换行。
现在请将“最新的聊天记录内容”无缝地整合进“已有的记录总结”中，生成一份更新后的、连贯的完整聊天记录总结。
更新后的记录总结必须保留所有旧回忆录中的关键信息、情感转折和重要决策。不能因为有了新内容就忘记或丢弃旧的重点。
如果已总结的内容已经非常长，可以适当的删减一些你觉得不重要的部分。
你要将新的聊天记录自然地融入到已有的总结中，而不是简单地把新内容附加在末尾。
你最终的输出应该是一份流畅、完整、独立的聊天记录总结。
请确保输出的只是更新后的聊天记录总结本身，不要包含任何额外的解释或标题。"""

        # --- User Prompt ---
        # Part 1: 已有总结
        if previous_summary and previous_summary.strip():
            summary_block = previous_summary
        else:
            summary_block = "暂时无总结，这是你专注于该群聊的首次总结"

        # Part 2: 聊天记录格式提示 (静态)
        format_hint_block = """
# CONTEXT
## Conversation Info
- conversation_name: "{conv_name}"
- conversation_type: "{conv_type}"

## Users
# 格式: ID: qq号 [nick:昵称, card:群名片/备注, title:头衔, perm:权限]
{user_list}
（注意U0代表的是你自己）

## Event Types
[MSG]: 普通消息，在消息后的（id:xxx）为消息的id
[SYS]: 系统通知
[MOTIVE]: 对应你的"motivation"，帮助你更好的了解自己的心路历程，它有两种出现形式：
      1. 独立出现时 (无缩进): 代表你经过思考后，决定“保持沉默/不发言”的原因。
      2. 附属出现时 (在[MSG]下缩进): 代表你发出该条消息的“背后动机”或“原因”，是消息的附注说明。
[IMG]: 图片消息
[FILE]: 文件分享""".format(
            conv_name=conversation_info.get('name', '未知'),
            conv_type=conversation_info.get('type', '未知'),
            user_list="\n".join([f"{u_info['uid_str']}: {p_id} [nick:{u_info['nick']}, card:{u_info['card']}, title:{u_info['title']}, perm:{u_info['perm']}]" for p_id, u_info in user_map.items()])
        )

        # Part 3: 新聊天记录
        chat_history_block = self._format_events_for_summary_prompt(
            recent_events, user_map, bot_profile.get("user_id", "")
        )

        # Part 4: 注意事项 (静态)
        notes_block = "像U0,U1这样的编号只是为了让你更好的分辨谁是谁，以及获取更多信息，你在输出总结时不应该使用这类编号来指代某人"

        user_prompt = f"""
<已有的记录总结>
{summary_block}
</已有的记录总结>

<聊天记录格式提示>
{format_hint_block}
</聊天记录格式提示>

<需要总结的新聊天记录>
# CHAT HISTORY LOG
{chat_history_block}
</需要总结的新聊天记录>

<注意事项>
{notes_block}
</注意事项>"""

        return system_prompt, user_prompt

    async def consolidate_summary(
        self,
        previous_summary: str | None,
        recent_events: list[dict[str, Any]],
        bot_profile: dict[str, Any],
        conversation_info: dict[str, Any],
        user_map: dict[str, dict[str, Any]],
    ) -> str:
        """
        对提供的最近事件列表进行总结，并将其整合进之前的摘要中。
        :param previous_summary: 上一轮的总结摘要。
        :param recent_events: 最近发生的事件列表。
        :param bot_profile: 机器人在当前会话的档案。
        :param conversation_info: 当前会话的信息。
        :param user_map: 当前会话的用户映射表。
        :return: 新的、更新后的第一人称总结文本。
        """
        self.logger.debug(
            f"开始整合摘要。之前摘要是否存在: {'是' if previous_summary else '否'}, 新事件数: {len(recent_events)}"
        )

        if not recent_events:
            self.logger.info("没有新的事件，直接返回之前的摘要。")
            return previous_summary or "我刚才好像走神了，什么也没记住。"

        system_prompt, user_prompt = await self._build_summary_prompt(
            previous_summary, recent_events, bot_profile, conversation_info, user_map
        )

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
                if previous_summary:
                    # 如果有旧摘要，就在后面附加上错误提示
                    return f"{previous_summary}\n\n[系统提示：我试图更新我的回忆，但是失败了（错误: {error_msg}）]"
                else:
                    # 如果没有旧摘要，就直接返回错误提示
                    return f"我试图开始我的回忆，但是失败了（错误: {error_msg}）。"

        except Exception as e:
            self.logger.error(f"生成整合摘要时发生意外错误: {e}", exc_info=True)
            return previous_summary or f"我在更新回忆时遇到了一个意想不到的问题（错误: {str(e)}）。"
