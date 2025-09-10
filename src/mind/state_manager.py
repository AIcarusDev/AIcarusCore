# src/core_logic/state_manager.py
from typing import Any

from src.common.custom_logging.logging_config import get_logger
from src.mind.goal_manager import GoalManager
from src.services.database import ActionLogStorageService, ThoughtStorageService

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
        goal_manager: GoalManager,
    ) -> None:
        """初始化需要 thought_storage_service 和 action_log_service 才能干活，哼."""
        self.thought_service = thought_service
        self.action_log_service = action_log_service
        self.goal_manager = goal_manager
        self._strategic_memos: list[dict[str, Any]] = []
        logger.info("AIStateManager 初始化完毕。")

    async def initialize(self) -> None:
        """初始化所有需要异步加载的状态组件."""
        await self.goal_manager.initialize()
