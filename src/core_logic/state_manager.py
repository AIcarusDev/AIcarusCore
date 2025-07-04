# src/core_logic/state_manager.py
import datetime
from typing import Any

from src.common.custom_logging.logging_config import get_logger
from src.database import ActionLogStorageService, ThoughtStorageService  # 导入ActionLogStorageService

logger = get_logger(__name__)


class AIStateManager:
    """
    管理AI的内心世界，烦死了，这么多内心戏。
    负责从数据库获取和处理AI的思考状态。
    """

    INITIAL_STATE: dict[str, Any] = {
        "mood_block": "你刚才的心情是：平静。",
        "think_block": "你刚才的内心想法是：这是你的第一次思考，请开始吧。",
        "goal_block": "你当前没有什么特定的目标或任务。",
        "action_request_block": "你上一轮没有试图执行任何动作。",
        "action_response_block": "因此也没有任何行动结果。",
        "action_log_block": "你最近没有执行过任何动作。",
    }

    def __init__(self, thought_service: ThoughtStorageService, action_log_service: ActionLogStorageService) -> None:
        """
        初始化需要 thought_storage_service 和 action_log_service 才能干活，哼。
        """
        self.thought_service = thought_service
        self.action_log_service = action_log_service  # 新增
        self._next_handover_summary: str | None = None
        self._next_last_focus_think: str | None = None
        self._next_last_focus_mood: str | None = None
        self.bot_profile_cache: dict[str, Any] = {}  # 新增
        logger.info("AIStateManager 初始化完毕，已准备好接收交接信息和处理动作日志。")

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

    async def get_current_state_for_prompt(self) -> dict[str, str]:
        """
        从数据库获取最新的思考和动作日志，处理一下，变成能直接喂给PromptBuilder的状态块字典。
        """
        state_blocks = self.INITIAL_STATE.copy()

        # 1. 获取最新的思考文档
        latest_thought_documents = await self.thought_service.get_latest_main_thought_document()
        latest_thought = latest_thought_documents[0] if latest_thought_documents else None

        # 2. 构建心情、想法和目标块
        if latest_thought:
            # 心情
            mood_db = latest_thought.get("emotion_output", "平静")
            state_blocks["mood_block"] = f"你刚才的心情是：{mood_db}"
            # 想法
            think_db = latest_thought.get("think_output")
            state_blocks["think_block"] = f"你刚才的内心想法是：{think_db}" if think_db else state_blocks["think_block"]
            # 目标
            goal_db = latest_thought.get("to_do_output")
            state_blocks["goal_block"] = f"你当前的目标是：【{goal_db}】" if goal_db else state_blocks["goal_block"]

        # 3. 处理上一轮的动作请求和响应
        last_action_attempt = latest_thought.get("action_attempted") if latest_thought else None
        if last_action_attempt and isinstance(last_action_attempt, dict):
            action_desc = last_action_attempt.get("action_description", "某个动作")
            action_motive = last_action_attempt.get("action_motivation", "某种动机")
            state_blocks["action_request_block"] = f'你刚才试图做的动作是:"{action_desc}"，因为:"{action_motive}"'

            action_status = last_action_attempt.get("status")
            if action_status in ["COMPLETED_SUCCESS", "COMPLETED_FAILURE", "CRITICAL_FAILURE"]:
                result_for_shimo = last_action_attempt.get("final_result_for_shimo")
                state_blocks["action_response_block"] = (
                    f'你刚才的动作"{action_desc}"，{result_for_shimo or "没有返回具体信息。"}'
                )
            elif action_status:
                state_blocks["action_response_block"] = (
                    f'你刚才的动作"{action_desc}"，目前还在执行中(状态: {action_status})。'
                )
            else:
                state_blocks["action_response_block"] = f'你刚才的动作"{action_desc}"，目前状态未知。'

        # 4. 构建动作日志块
        recent_logs = await self.action_log_service.get_recent_action_logs(limit=10)
        if recent_logs:
            log_lines = ["你最近执行过的动作有："]
            for log in reversed(recent_logs):  # 从旧到新显示
                ts = datetime.datetime.fromtimestamp(log.get("timestamp", 0) / 1000.0)
                time_str = ts.strftime("%H:%M:%S")
                log_lines.append(f"- 在 {time_str}，你执行了动作: {log.get('action_type')}")
            state_blocks["action_log_block"] = "\n".join(log_lines)

        return state_blocks
