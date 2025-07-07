# src/core_logic/state_manager.py (小懒猫·清爽版)
import datetime
from typing import Any

from src.common.custom_logging.logging_config import get_logger
from src.database import ActionLogStorageService, ThoughtStorageService

logger = get_logger(__name__)


class AIStateManager:
    """
    管理AI的内心世界，现在清爽多了，哼。
    我只负责从思想链里拿最新的状态。
    """

    # 这个初始状态仍然有用，在思想链还没有任何内容的时候，可以作为保底
    INITIAL_STATE: dict[str, Any] = {
        "mood_block": "你刚才的心情是：平静。",
        "think_block": "你刚才的内心想法是：这是你的第一次思考，请开始吧。",
        "goal_block": "你当前没有什么特定的目标或任务。",
        # // 注意，这几个字段现在只在初始状态下或者思想链断裂时使用
        "action_request_block": "你上一轮没有试图执行任何动作。",
        "action_response_block": "因此也没有任何行动结果。",
        "action_log_block": "你最近没有执行过任何动作。",
    }

    def __init__(self, thought_service: ThoughtStorageService, action_log_service: ActionLogStorageService) -> None:
        """
        初始化需要 thought_storage_service 和 action_log_service 才能干活，哼。
        """
        self.thought_service = thought_service
        self.action_log_service = action_log_service
        logger.info("AIStateManager (思想链版) 初始化完毕。")

    # // 看！那个烦人的 set_next_handover_info 不见了！我们再也不需要它了！

    async def get_current_state_for_prompt(self) -> dict[str, str]:
        """
        从思想链获取最新的状态，构建Prompt需要的所有状态块。
        """
        state_blocks = self.INITIAL_STATE.copy()

        # 1. 获取最新的思想点
        latest_thought = await self.thought_service.get_latest_thought_document()

        # 2. 从点里拿出我们需要的东西，填充状态块
        if latest_thought:
            state_blocks["mood_block"] = f"你刚才的心情是：{latest_thought.get('mood', '平静')}"
            state_blocks["think_block"] = f"你刚才的内心想法是：{latest_thought.get('think', '我好像忘了刚才在想啥')}"

            goal_db = latest_thought.get("goal")
            if goal_db and goal_db.strip().lower() != 'null':
                 state_blocks["goal_block"] = f"你当前的目标是：【{goal_db}】"
            else:
                 state_blocks["goal_block"] = self.INITIAL_STATE["goal_block"]

            # // 动作相关的逻辑也简单多了
            # // 我们不再需要复杂的 action_request 和 action_response 块了，
            # // 因为动作信息已经通过 action_log_block 更清晰地展示
            if latest_thought.get("action_id"):
                action_desc = "某个动作"
                if action_payload := latest_thought.get("action_payload", {}):
                    # 随便解析第一个动作作为代表
                    platform, actions = next(iter(action_payload.items()), (None, None))
                    if actions:
                        action_name, _ = next(iter(actions.items()), (None, None))
                        if platform and action_name:
                             action_desc = f"在平台 '{platform}' 执行 '{action_name}'"

                state_blocks["action_request_block"] = f'你刚才的想法导致你试图执行动作"{action_desc}"。'
                state_blocks["action_response_block"] = "该行动的后续状态和结果，请参考下面的行动日志。"
            else:
                # 如果最新的思考没有附带动作，就用初始的默认值
                state_blocks["action_request_block"] = self.INITIAL_STATE["action_request_block"]
                state_blocks["action_response_block"] = self.INITIAL_STATE["action_response_block"]

        # 3. 动作日志照旧，这是唯一独立的外部信息源
        recent_logs = await self.action_log_service.get_recent_action_logs(limit=10)
        if recent_logs:
            log_lines = ["你最近执行过的动作有："]
            for log in reversed(recent_logs):  # 从旧到新显示
                ts = datetime.datetime.fromtimestamp(log.get("timestamp", 0) / 1000.0)
                time_str = ts.strftime("%H:%M:%S")
                status = log.get("status", "未知")
                error_info = log.get("error_info")
                status_display = f"状态: {status}"
                if error_info:
                    status_display += f" (原因: {error_info[:30]}{'...' if len(error_info) > 30 else ''})"

                log_lines.append(f"- 在 {time_str}，你执行了动作: {log.get('action_type')}，{status_display}")
            state_blocks["action_log_block"] = "\n".join(log_lines)
        else:
            state_blocks["action_log_block"] = self.INITIAL_STATE["action_log_block"]

        return state_blocks
