from typing import Any

from src.common.custom_logging.logging_config import get_logger

# 导入我们的新玩具！
from src.common.focus_chat_history_builder.chat_history_formatter import format_chat_history_for_llm
from src.config import config
from src.database.services.event_storage_service import EventStorageService
from src.llmrequest.llm_processor import Client as LLMProcessorClient

logger = get_logger(__name__)


class SummarizationService:
    """服务类，负责对会话历史进行总结，生成第一人称的回忆录.

    Attributes:
        llm_client (LLMProcessorClient): 用于与LLM交互的客户端实例.
    """

    def __init__(self, llm_client: LLMProcessorClient) -> None:
        """初始化服务，只需要一个LLM客户端就够了，真省心.

        Args:
            llm_client (LLMProcessorClient): 用于与LLM交互的客户端实例.
        """
        self.llm_client = llm_client
        logger.info("SummarizationService (重构版) 已初始化。")

    async def _build_summary_prompt(
        self,
        previous_summary: str | None,
        formatted_chat_history: str,
        image_references: list[str],
        conversation_info: dict[str, Any],
        user_map: dict[str, Any],
        shift_motivation: str | None = None,
        target_conversation_id: str | None = None,
    ) -> tuple[str, str]:
        """构建用于总结的系统和用户提示.

        Args:
            previous_summary (str | None): 之前的聊天记录总结，如果没有则为None.
            formatted_chat_history (str): 格式化后的聊天记录内容.
            image_references (list[str]): 图片引用列表，包含图片的URL或路径.
            conversation_info (dict[str, Any]): 会话信息，包括ID、名称等.
            user_map (dict[str, Any]): 用户映射，包含用户ID、昵称、群名片等信息.
            shift_motivation (str | None): 跳槽动机，如果有则为非None，
                表示用户有意转移注意力到另一个会话.
            target_conversation_id (str | None): 目标会话ID，如果有跳槽动机，
                则表示用户想要转移到的会话ID.

        Returns:
            tuple[str, str]: 返回系统提示和用户提示的元组.
        """
        # --- System Prompt ---
        persona_config = config.persona
        from datetime import datetime

        current_time_str = datetime.now().strftime("%Y年%m月%d日 %H点%M分%S秒")

        # 构造“跳槽动机”的文本块
        shift_motivation_block = ""
        if shift_motivation and target_conversation_id:
            # 尝试从 user_map 获取目标会话的名称，哼，虽然不一定有
            _target_conv_name = target_conversation_id  # 默认用ID
            for _, _u_info in user_map.items():
                # 这个逻辑不完全对，因为user_map是当前会话的，不一定有目标会话的信息
                # 但我们可以先这么写，以后再优化。或者直接让调用者传名字进来。
                # 这里我们简化，就用ID。
                pass  # 暂时找不到好办法，就用ID吧

            shift_motivation_block = (
                f"\n此刻，你因为“{shift_motivation}”，决定将注意力转移到另一个会话 "
                f"(ID: {target_conversation_id})。请在总结中自然地体现出这个转折点。"
            )

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
{shift_motivation_block}
你最终的输出应该是一份流畅、完整、独立的聊天记录总结。
请确保输出的只是更新后的聊天记录总结本身，不要包含任何额外的解释或标题。
"""

        # --- User Prompt ---
        # Part 1: 已有总结
        summary_block = previous_summary or "暂时无总结，这是你专注于该群聊的首次总结"

        # Part 2: 聊天记录格式提示
        user_list_lines = []
        for p_id, u_info in user_map.items():
            user_list_lines.append(
                f"{u_info['uid_str']}: {p_id} [nick:{u_info['nick']}, card:{u_info['card']}, "
                f"title:{u_info['title']}, perm:{u_info['perm']}]"
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
        notes_block = (
            "像U0,U1这样的编号只是为了让你更好的分辨谁是谁，以及获取更多信息，"
            "你在输出总结时不应该使用这类编号来指代某人"
        )

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
        event_storage: "EventStorageService",
        shift_motivation: str | None = None,  # 新玩具
        target_conversation_id: str | None = None,  # 新玩具
    ) -> str:
        """对提供的最近事件列表进行总结，并将其整合进之前的摘要中."""
        logger.debug(
            f"开始整合摘要。之前摘要是否存在: {'是' if previous_summary else '否'}, "
            f"新事件数: {len(recent_events)}"
        )

        # 这里检查一下，如果没新事件，但有“跳槽动机”，说明是“临别赠言”，也得生成一个最终总结
        if not recent_events and not shift_motivation:
            logger.info("没有新的事件，也没有转移意图，直接返回之前的摘要。")
            return previous_summary or "我刚才好像走神了，什么也没记住。"

        prompt_components = await format_chat_history_for_llm(
            event_storage=event_storage,
            conversation_id=conversation_info.get("id"),
            bot_id=bot_profile.get("user_id"),
            platform=conversation_info.get("platform", "unknown"),
            bot_profile=bot_profile,
            conversation_type=conversation_info.get("type"),
            conversation_name=conversation_info.get("name"),
            last_processed_timestamp=0,
            is_first_turn=True,
            raw_events_from_caller=recent_events,
        )

        # 如果没有新事件，聊天记录就是空的，这没关系
        if not recent_events:
            prompt_components.chat_history_log_block = "（无新的聊天记录）"

        extended_conv_info = conversation_info.copy()
        extended_conv_info["bot_id"] = bot_profile.get("user_id")
        extended_conv_info["bot_card"] = bot_profile.get("card")
        extended_conv_info["name"] = prompt_components.conversation_name or conversation_info.get(
            "name"
        )

        system_prompt, user_prompt = await self._build_summary_prompt(
            previous_summary,
            prompt_components.chat_history_log_block,
            prompt_components.image_references,
            extended_conv_info,
            prompt_components.user_map,
            shift_motivation,
            target_conversation_id,
        )

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
            response_data = await self.llm_client.make_llm_request(
                prompt=user_prompt,
                system_prompt=system_prompt,
                is_stream=False,
                is_multimodal=bool(prompt_components.image_references),
                image_inputs=prompt_components.image_references,
                use_google_search=True,
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
                error_msg = (
                    response_data.get("message", "未知错误") if response_data else "LLM无响应"
                )
                if previous_summary:
                    # 如果有旧摘要，就在后面附加上错误提示
                    return (
                        f"{previous_summary}\n\n[系统提示：我试图更新我的回忆，"
                        f"但是失败了（错误: {error_msg}）]"
                    )
                else:
                    # 如果没有旧摘要，就直接返回错误提示
                    return f"我试图开始我的回忆，但是失败了（错误: {error_msg}）。"

        except Exception as e:
            logger.error(f"生成整合摘要时发生意外错误: {e}", exc_info=True)
            return previous_summary or f"我在更新回忆时遇到了一个意想不到的问题（错误: {e!s}）。"
