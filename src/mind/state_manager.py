# src/core_logic/state_manager.py
import datetime
import time
from typing import Any, ClassVar

from src.common.custom_logging.logging_config import get_logger
from src.mind.goal_manager import GoalManager
from src.services.database import ActionLogStorageService, GoalStorageService, ThoughtStorageService

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
        "intent_block": "你当前没有什么特定的目标或任务。",
        # // 注意，这几个字段现在只在初始状态下或者思想链断裂时使用
        "action_request_block": "你上一轮没有试图执行任何动作。",
        "action_response_block": "因此也没有任何行动结果。",
        "action_log_block": "你最近没有执行过任何动作。",
    }

    # <-- 修改点 1: 移除了 goal_storage_service 参数
    def __init__(
        self,
        thought_service: ThoughtStorageService,
        action_log_service: ActionLogStorageService,
        goal_storage_service: GoalStorageService,
    ) -> None:
        """初始化需要 thought_storage_service 和 action_log_service 才能干活，哼."""
        self.thought_service = thought_service
        self.action_log_service = action_log_service
        self.goal_manager = GoalManager(goal_storage_service)
        self._strategic_memos: list[dict[str, Any]] = []
        # GoalManager 现在是纯内存组件，不再需要 GoalStorageService
        logger.info("AIStateManager 初始化完毕。")

    # -- 修改点 2: 移除了 initialize 方法，因为它不再需要从数据库加载目标

    async def initialize(self) -> None:
        """初始化所有需要异步加载的状态组件."""
        await self.goal_manager.initialize()

    def add_strategic_memo(self, resolution: dict) -> None:
        """将慢思考的决议作为一个有时效性的备忘录添加到全局状态中."""
        if not resolution or not resolution.get("summary"):
            return

        # remaining_turns (思考轮数) 转换为过期时间戳
        # 假设每轮思考间隔为 thinking_interval_seconds
        from src.config import config
        thinking_interval = config.core_logic_settings.thinking_interval_seconds
        remaining_turns = resolution.get("memory_duration", 2)
        lifetime_seconds = remaining_turns * thinking_interval

        memo = {
            "summary": resolution.get("summary"),
            "expires_at": time.time() + lifetime_seconds,
            "remaining_turns": remaining_turns # 保留轮数用于显示
        }
        self._strategic_memos.append(memo)
        logger.info(f"新的战略备忘录已添加，将在约 {lifetime_seconds} 秒后过期。")

    # [核心新增] 获取并清理过期的备忘录，用于构建Prompt
    def get_formatted_strategic_memos(self) -> str:
        """获取所有未过期的战略备忘录，并格式化为字符串."""
        current_time = time.time()

        # 过滤掉已过期的备忘录
        self._strategic_memos = [
            memo for memo in self._strategic_memos if memo["expires_at"] > current_time
        ]

        if not self._strategic_memos:
            return ""

        # 格式化输出
        memo_blocks = []
        for memo in self._strategic_memos:
            # 实时计算剩余轮数
            from src.config import config
            thinking_interval = config.core_logic_settings.thinking_interval_seconds
            remaining_seconds = memo['expires_at'] - current_time
            remaining_turns = max(1, round(remaining_seconds / thinking_interval))

            block = (
                f'<deliberation_summary duration="{remaining_turns}_cycles">\n'
                f"<!-- 这是你仔细思考后的总结，将在约 {remaining_turns} 轮思考后遗忘 -->\n"
                f"{memo['summary']}\n"
                f"</deliberation_summary>"
            )
            memo_blocks.append(block)

        return "\n\n".join(memo_blocks)

    async def get_current_state_for_prompt(self) -> dict[str, str]:  # TODO：该方法可能废弃，待处理
        """从思想链获取最新的状态，构建Prompt需要的所有状态块."""
        state_blocks = self.INITIAL_STATE.copy()

        # 1. 获取最新的思想点
        latest_thought = await self.thought_service.get_latest_thought_document()

        # 2. 从点里拿出我们需要的东西，填充状态块
        if latest_thought:
            state_blocks["mood_block"] = f"你刚才的心情是：{latest_thought.get('mood', '平静')}"
            state_blocks["think_block"] = (
                f"你刚才的内心想法是：{latest_thought.get('think', '我好像忘了刚才在想啥')}"
            )

            intent_db = latest_thought.get("intent")
            if intent_db and intent_db.strip().lower() != "null":
                state_blocks["intent_block"] = f"你当前的目标是：【{intent_db}】"
            else:
                state_blocks["intent_block"] = self.INITIAL_STATE["intent_block"]

            # 先看看有没有“发货单号”（action_id）
            if _action_id := latest_thought.get("action_id"):
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
                    state_blocks["action_response_block"] = (
                        f"你刚才的动作返回的结果是：\n---\n{action_result}\n---"
                    )
                else:
                    # 没回执单，就告诉主意识快递还在路上，或者这趟活儿本来就没回执
                    state_blocks["action_response_block"] = (
                        "该行动正在执行或未产生直接文本结果，请参考下面的行动日志了解状态。"
                    )

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
