# src/core_logic/state_manager.py
from typing import Any

from src.common.custom_logging.logging_config import get_logger
from src.database import ThoughtStorageService

logger = get_logger(__name__)


class AIStateManager:
    """
    管理AI的内心世界，烦死了，这么多内心戏。
    负责从数据库获取和处理AI的思考状态。
    """

    INITIAL_STATE: dict[str, Any] = {
        "mood": "你现在的心情大概是：平静。",
        "previous_thinking": "你的上一轮思考是：这是你的第一次思考，请开始吧。",
        "thinking_guidance": "经过你上一轮的思考，你目前打算的思考方向是：随意发散一下吧。",
        "current_task": "没有什么具体目标",
        "action_result_info": "你上一轮没有执行产生结果的特定行动。",
        "pending_action_status": "",
        "recent_contextual_information": "最近未感知到任何特定信息或通知。",
    }

    def __init__(self, thought_service: ThoughtStorageService) -> None:
        """
        初始化需要一个 thought_storage_service 才能干活，哼。
        """
        self.thought_service = thought_service
        self._next_handover_summary: str | None = None
        self._next_last_focus_think: str | None = None
        self._next_last_focus_mood: str | None = None
        logger.info("AIStateManager 初始化完毕，已准备好接收交接信息。")

    def set_next_handover_info(
        self, summary: str | None, last_focus_think: str | None, last_focus_mood: str | None
    ) -> None:
        """
        存储从专注模式传递过来的交接信息，供下一轮思考使用。
        哼，别想把这些重要的东西随便丢掉！
        """
        self._next_handover_summary = summary
        self._next_last_focus_think = last_focus_think
        self._next_last_focus_mood = last_focus_mood
        log_parts = []
        if summary:
            log_parts.append(f"交接总结 (前50字符): '{summary[:50]}...'")
        if last_focus_think:
            log_parts.append(f"专注模式最后想法 (前50字符): '{last_focus_think[:50]}...'")
        if last_focus_mood:
            log_parts.append(f"专注模式最后心情: '{last_focus_mood}'")
        if log_parts:
            logger.info(f"AIStateManager 已接收到交接信息：{'; '.join(log_parts)}")
        else:
            logger.info("AIStateManager set_next_handover_info 被调用，但未提供有效信息。")

    async def get_current_state_for_prompt(
        self, formatted_recent_contextual_info: str
    ) -> tuple[dict[str, Any], str | None]:
        """
        从数据库获取最新的思考，处理一下，变成能直接喂给PromptBuilder的状态。
        这个方法就是把原来 CoreLogic._process_thought_and_action_state 的逻辑搬过来了。
        """
        action_id_whose_result_is_being_shown: str | None = None
        state_from_initial = self.INITIAL_STATE.copy()

        latest_thought_documents = await self.thought_service.get_latest_main_thought_document()
        latest_thought_document = latest_thought_documents[0] if latest_thought_documents else None

        if not latest_thought_document or not isinstance(latest_thought_document, dict):
            logger.info("最新的思考文档为空或格式不正确，将使用初始的处女思考状态。")
            mood_for_prompt = state_from_initial["mood"]
            previous_thinking_for_prompt = state_from_initial["previous_thinking"]
            thinking_guidance_for_prompt = state_from_initial["thinking_guidance"]
            actual_current_task_description = state_from_initial["current_task"]
        else:
            mood_db = latest_thought_document.get("emotion_output", state_from_initial["mood"].split("：", 1)[-1])
            mood_for_prompt = f"你现在的心情大概是：{mood_db}"

            # 默认的上一轮思考
            prev_think_db = latest_thought_document.get("think_output")
            previous_thinking_for_prompt = (
                f"你的上一轮思考是：{prev_think_db}"
                if prev_think_db and str(prev_think_db).strip()
                else state_from_initial["previous_thinking"]
            )

            # 检查是否有来自专注模式的交接信息，并用它覆盖/补充上一轮思考
            if self._next_last_focus_think or self._next_handover_summary or self._next_last_focus_mood:
                handover_parts = []
                if self._next_last_focus_mood:
                    mood_for_prompt = f"你现在的心情大概是：{self._next_last_focus_mood} "
                if self._next_last_focus_think:
                    handover_parts.append(f"刚刚结束的专注聊天留下的最后想法是：'{self._next_last_focus_think}'")
                if self._next_handover_summary:
                    handover_parts.append(f"该专注聊天的总结大致如下：\n---\n{self._next_handover_summary}\n---")

                if handover_parts:
                    previous_thinking_for_prompt = "。\n".join(handover_parts)
                    logger.info("已将专注模式的交接信息整合到 'previous_thinking' 中。")

                # 清理交接信息，确保只用一次
                self._next_handover_summary = None
                self._next_last_focus_think = None
                self._next_last_focus_mood = None
                logger.debug("已清理AIStateManager中的交接信息。")

            guidance_db = latest_thought_document.get(
                "next_think_output",
                state_from_initial["thinking_guidance"].split("：", 1)[-1]
                if "：" in state_from_initial["thinking_guidance"]
                else "随意发散一下吧。",
            )
            thinking_guidance_for_prompt = f"经过你上一轮的思考，你目前打算的思考方向是：{guidance_db}"

            actual_current_task_description = latest_thought_document.get(
                "to_do_output", state_from_initial["current_task"]
            )
            if latest_thought_document.get(
                "done_output", False
            ) and actual_current_task_description == latest_thought_document.get("to_do_output"):
                actual_current_task_description = state_from_initial["current_task"]

        action_result_info_prompt = state_from_initial["action_result_info"]
        pending_action_status_prompt = state_from_initial["pending_action_status"]
        last_action_attempt = latest_thought_document.get("action_attempted") if latest_thought_document else None

        if last_action_attempt and isinstance(last_action_attempt, dict):
            action_status = last_action_attempt.get("status")
            action_description_prev = last_action_attempt.get("action_description", "某个之前的动作")
            action_id = last_action_attempt.get("action_id")
            was_result_seen_by_llm = last_action_attempt.get("result_seen_by_shimo", False)
            if action_status in ["COMPLETED_SUCCESS", "COMPLETED_FAILURE", "CRITICAL_FAILURE"]:
                result_for_shimo = last_action_attempt.get("final_result_for_shimo")
                if result_for_shimo and not was_result_seen_by_llm:
                    action_result_info_prompt = result_for_shimo
                    action_id_whose_result_is_being_shown = action_id
            elif action_status and action_status not in ["COMPLETED_SUCCESS", "COMPLETED_FAILURE", "CRITICAL_FAILURE"]:
                pending_action_status_prompt = (
                    f"你目前有一个正在进行的动作：{action_description_prev} (状态：{action_status})"
                )

        current_state_for_prompt = {
            "mood": mood_for_prompt,
            "previous_thinking": previous_thinking_for_prompt,
            "thinking_guidance": thinking_guidance_for_prompt,
            "current_task_description": actual_current_task_description,
            "action_result_info": action_result_info_prompt,
            "pending_action_status": pending_action_status_prompt,
            "recent_contextual_information": formatted_recent_contextual_info,
        }

        return current_state_for_prompt, action_id_whose_result_is_being_shown
