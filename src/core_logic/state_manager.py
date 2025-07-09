# src/core_logic/state_manager.py
import datetime
from typing import Any, ClassVar

from src.common.custom_logging.logging_config import get_logger
from src.database import ActionLogStorageService, ThoughtStorageService

logger = get_logger(__name__)


class AIStateManager:
    """AIStateManager (思想链版) 负责管理 AI 的状态信息，包括心情、内心想法、目标和最近的行动日志等.

    Attributes:
        thought_service (ThoughtStorageService): 用于存储和获取思想点的服务实例.
        action_log_service (ActionLogStorageService): 用于存储和获取动作日志的服务实例.

    这个类的主要职责是从思想链中获取最新的状态信息，并将其格式化为适合生成 Prompt 的状态块。
    它还提供了一个初始状态，确保在思想链为空或断裂时，仍然能够提供有用的默认信息。
    这个初始状态包括心情、内心想法、目标以及最近的行动日志等信息。
    通过这种方式，AIStateManager 确保 AI 在任何时候都能够获取到最新的状态信息，
    并且在思想链断裂时也能提供有用的默认信息，确保生成的 Prompt 始终具有上下文相关性。
    """

    # 这个初始状态仍然有用，在思想链还没有任何内容的时候，可以作为保底
    INITIAL_STATE: ClassVar[dict[str, Any]] = {
        "mood_block": "你刚才的心情是：平静。",
        "think_block": "你刚才的内心想法是：这是你的第一次思考，请开始吧。",
        "goal_block": "你当前没有什么特定的目标或任务。",
        # // 注意，这几个字段现在只在初始状态下或者思想链断裂时使用
        "action_request_block": "你上一轮没有试图执行任何动作。",
        "action_response_block": "因此也没有任何行动结果。",
        "action_log_block": "你最近没有执行过任何动作。",
    }

    def __init__(
        self, thought_service: ThoughtStorageService, action_log_service: ActionLogStorageService
    ) -> None:
        """初始化需要 thought_storage_service 和 action_log_service 才能干活，哼."""
        self.thought_service = thought_service
        self.action_log_service = action_log_service
        logger.info("AIStateManager (思想链版) 初始化完毕。")

    async def get_current_state_for_prompt(self) -> dict[str, str]:
        """从思想链获取最新的状态，构建Prompt需要的所有状态块."""
        state_blocks = self.INITIAL_STATE.copy()

        # 1. 获取最新的思想点
        latest_thought = await self.thought_service.get_latest_thought_document()

        # 2. 从点里拿出我们需要的东西，填充状态块
        if latest_thought:
            # --- 填充心情、想法、目标，这些都是直接抄作业 ---
            state_blocks["mood_block"] = f"你刚才的心情是：{latest_thought.get('mood', '平静')}"
            state_blocks["think_block"] = (
                f"你刚才的内心想法是：{latest_thought.get('think', '我好像忘了刚才在想啥')}"
            )

            goal_db = latest_thought.get("goal")
            if goal_db and goal_db.strip().lower() != "null":
                state_blocks["goal_block"] = f"你当前的目标是：【{goal_db}】"
            else:
                state_blocks["goal_block"] = self.INITIAL_STATE["goal_block"]

            # --- 关键部分：处理动作和它的“回执单” ---

            # 先看看有没有“发货单号”（action_id）
            if action_id := latest_thought.get("action_id"):
                # 如果有，就告诉主意识它上次试图干了啥
                action_desc = "某个动作"
                if action_payload := latest_thought.get("action_payload", {}):
                    # 随便从动作描述里抓个大概意思当代表
                    try:
                        platform, actions = next(iter(action_payload.items()))
                        action_name, _ = next(iter(actions.items()))
                        action_desc = f"在平台 '{platform}' 执行 '{action_name}'"
                    except (IndexError, AttributeError):
                        action_desc = "执行一个复杂的未知动作"

                state_blocks["action_request_block"] = (
                    f'你刚才的想法导致你试图执行动作"{action_desc}"。'
                )

                # 现在，我只关心有没有“回执单”（action_result）
                if action_result := latest_thought.get("action_result"):
                    # 有回执单！太棒了！直接抄！
                    state_blocks["action_response_block"] = f"你刚才的动作返回的结果是：\n---\n{action_result}\n---"
                else:
                    # 没回执单，就告诉主意识快递还在路上，或者这趟活儿本来就没回执
                    state_blocks["action_response_block"] = "该行动正在执行或未产生直接文本结果，请参考下面的行动日志了解状态。"

            else:
                # 如果最新的思考没有附带动作，就用初始的默认值
                state_blocks["action_request_block"] = self.INITIAL_STATE["action_request_block"]
                state_blocks["action_response_block"] = self.INITIAL_STATE["action_response_block"]

        # 3. 动作日志照旧，这是独立的外部信息源
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
                    status_display += (
                        f" (原因: {error_info[:30]}{'...' if len(error_info) > 30 else ''})"
                    )

                log_lines.append(
                    f"- 在 {time_str}，你执行了动作: {log.get('action_type')}，{status_display}"
                )
            state_blocks["action_log_block"] = "\n".join(log_lines)
        else:
            state_blocks["action_log_block"] = self.INITIAL_STATE["action_log_block"]

        return state_blocks
