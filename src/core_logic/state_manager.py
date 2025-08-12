# src/core_logic/state_manager.py
from typing import Any, ClassVar

from src.common.custom_logging.logging_config import get_logger
from src.core_logic.goal_manager import GoalManager
from src.database import ActionLogStorageService, ThoughtStorageService
from src.database.services.goal_storage_service import GoalStorageService

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
        logger.info("AIStateManager 初始化完毕。")

    async def initialize(self) -> None:
        """执行所有需要异步初始化的状态组件."""
        await self.goal_manager.initialize()

