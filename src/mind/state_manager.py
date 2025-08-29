# src/core_logic/state_manager.py
import time
from typing import Any

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
