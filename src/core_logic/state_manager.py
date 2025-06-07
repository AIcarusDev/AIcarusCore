# src/core_logic/state_manager.py
from typing import Any

from src.common.custom_logging.logger_manager import get_logger
from src.database.services.thought_storage_service import ThoughtStorageService

logger = get_logger("AIcarusCore.StateManager")


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

            prev_think_db = latest_thought_document.get("think_output")
            previous_thinking_for_prompt = (
                f"你的上一轮思考是：{prev_think_db}"
                if prev_think_db and prev_think_db.strip()
                else state_from_initial["previous_thinking"]
            )

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
