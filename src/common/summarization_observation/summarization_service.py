
from typing import Any

# 导入我们的新玩具！
from src.common.focus_chat_history_builder.chat_history_formatter import format_chat_history_for_llm
from src.common.custom_logging.logging_config import get_logger
from src.config import config
from src.llmrequest.llm_processor import Client as LLMProcessorClient

logger = get_logger(__name__)


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
        logger.info("SummarizationService (重构版) 已初始化。")

    # 注意：这里原来那个又长又臭的 _format_events_for_summary_prompt 方法已经被我扔进垃圾桶了！

    async def _build_summary_prompt(
        self,
        previous_summary: str | None,
        formatted_chat_history: str,
        image_references: list[str],
        conversation_info: dict[str, Any],
        user_map: dict[str, Any],
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
你的qq号是"{conversation_info.get("bot_id", "未知")}"；
你当前正在qq群"{conversation_info.get("name", "未知群聊")}"中参与qq群聊
你在该群的群名片是"{conversation_info.get("bot_card", persona_config.bot_name)}"
你的任务是以你的视角总结聊天记录里的内容，包括人物、事件和主要信息，不要分点，不要换行。
现在请将“最新的聊天记录内容”无缝地整合进“已有的记录总结”中，生成一份更新后的、连贯的完整聊天记录总结。
更新后的记录总结必须保留所有旧回忆录中的关键信息、情感转折和重要决策。不能因为有了新内容就忘记或丢弃旧的重点。
如果已总结的内容已经非常长，可以适当的删减一些你觉得不重要的部分。
你要将新的聊天记录自然地融入到已有的总结中，而不是简单地把新内容附加在末尾。
你最终的输出应该是一份流畅、完整、独立的聊天记录总结。
请确保输出的只是更新后的聊天记录总结本身，不要包含任何额外的解释或标题。"""

        # --- User Prompt ---
        # Part 1: 已有总结
        summary_block = previous_summary or "暂时无总结，这是你专注于该群聊的首次总结"

        # Part 2: 聊天记录格式提示 (遵从你的旧格式，我用新工具返回的数据帮你拼起来)
        user_list_lines = []
        for p_id, u_info in user_map.items():
            user_list_lines.append(
                f"{u_info['uid_str']}: {p_id} [nick:{u_info['nick']}, card:{u_info['card']}, title:{u_info['title']}, perm:{u_info['perm']}]"
            )
        user_list_block = "\n".join(user_list_lines)

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
[FILE]: 文件分享""".format(
            conv_name=conversation_info.get("name", "未知"),
            conv_type=conversation_info.get("type", "未知"),
            user_list=user_list_block,
        )

        # Part 3: 新聊天记录
        chat_history_block = formatted_chat_history

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
        event_storage: Any, # 实际应为 EventStorageService
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
        logger.debug(
            f"开始整合摘要。之前摘要是否存在: {'是' if previous_summary else '否'}, 新事件数: {len(recent_events)}"
        )

        if not recent_events:
            logger.info("没有新的事件，直接返回之前的摘要。")
            return previous_summary or "我刚才好像走神了，什么也没记住。"

        # --- 【核心改造点】调用通用格式化工具 ---
        # 很多参数都是为了调用这个新玩具准备的
        (
            formatted_chat_history,
            user_map, # 需要这个来构建你的旧Prompt
            _, # uid_to_pid_map, 我们不需要
            _, # processed_event_ids, 我们不需要
            image_references, # 我们需要这个！
            conversation_name_from_formatter,
            _, # last_message_text, 我们不需要
        ) = await format_chat_history_for_llm(
            event_storage=event_storage, # 把 event_storage 传给它
            conversation_id=conversation_info.get("id"),
            bot_id=bot_profile.get("user_id"),
            platform=conversation_info.get("platform", "unknown"),
            bot_profile=bot_profile,
            conversation_type=conversation_info.get("type"),
            conversation_name=conversation_info.get("name"),
            last_processed_timestamp=0, # 对于总结，我们通常处理的是一批，所以时间戳起点不重要
            is_first_turn=True, # 同上
            raw_events_from_caller=recent_events, # 直接把事件喂给它，懒得让它再去查数据库
        )

        # 准备一个包含bot_id和bot_card的conversation_info字典，给你的旧Prompt用
        extended_conv_info = conversation_info.copy()
        extended_conv_info["bot_id"] = bot_profile.get("user_id")
        extended_conv_info["bot_card"] = bot_profile.get("card")
        extended_conv_info["name"] = conversation_name_from_formatter or conversation_info.get("name")

        # 构建 Prompt，这次用的是你那个没改过的旧版逻辑
        system_prompt, user_prompt = await self._build_summary_prompt(
            previous_summary, formatted_chat_history, image_references, extended_conv_info, user_map
        )
         # --- 【在这里也加上我的探针！】 ---
        conv_id_for_log = conversation_info.get("id", "未知会话")
        logger.debug(
            f"[{conv_id_for_log}] 摘要整合 - 准备发送给LLM的完整Prompt:\n"
            f"==================== SYSTEM PROMPT (摘要整合) ====================\n"
            f"{system_prompt}\n"
            f"==================== USER PROMPT (摘要整合) ======================\n"
            f"{user_prompt}\n"
            f"=================================================================="
        )

        try:
            # 【改造点】调用LLM时，把图片也一起喂进去！
            response_data = await self.llm_client.make_llm_request(
                prompt=user_prompt,
                system_prompt=system_prompt,
                is_stream=False,
                is_multimodal=bool(image_references), # 告诉LLM有图
                image_inputs=image_references, # 把图片塞进去
            )

            if response_data and not response_data.get("error"):
                new_summary_text = response_data.get("text", "").strip()
                if new_summary_text:
                    logger.info(f"成功生成整合后的摘要 (部分): {new_summary_text[:100]}...")
                    return new_summary_text
                else:
                    logger.warning("LLM为摘要整合返回了空内容。将保留之前的摘要（如果存在）。")
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
            logger.error(f"生成整合摘要时发生意外错误: {e}", exc_info=True)
            return previous_summary or f"我在更新回忆时遇到了一个意想不到的问题（错误: {str(e)}）。"
