# src/action/providers/platform_action_provider.py
import uuid
from collections.abc import Callable, Coroutine
from functools import partial
from typing import TYPE_CHECKING, Any

from src.action.action_provider import ActionProvider
from src.config import config

if TYPE_CHECKING:
    from src.action.action_handler import ActionHandler


class PlatformActionProvider(ActionProvider):
    """
    提供与外部平台交互的动作。
    它不是直接执行动作，而是构造一个标准的平台动作事件字典，
    然后调用 ActionHandler 中通用的平台动作执行方法来处理。
    """

    def __init__(self, action_handler: "ActionHandler") -> None:
        self._action_handler = action_handler
        # 假设支持的平台和动作是固定的，或者可以从配置中读取
        self._supported_actions = {
            "qq": ["send_message", "poke"],
            "master_ui": ["send_message"],
            # 未来可以添加更多平台，如 'discord': ['send_message']
        }

    @property
    def name(self) -> str:
        # 这个 Provider 比较特殊，它本身不代表一个层级，而是动态生成多个
        # 所以它的 name 属性其实不会被直接用到
        return "platform"

    def get_actions(self) -> dict[str, Callable[..., Coroutine[Any, Any, Any]]]:
        """
        动态生成所有平台动作的包装函数。
        """
        actions: dict[str, Callable[..., Coroutine[Any, Any, Any]]] = {}
        for platform, action_names in self._supported_actions.items():
            for action_name in action_names:
                full_action_name = f"platform.{platform}.{action_name}"
                # 使用偏函数 (partial) 来绑定 platform 和 action_name
                # 注意：kwargs 将在实际调用时由 ActionHandler 传入
                actions[full_action_name] = partial(
                    self._platform_action_wrapper, platform=platform, action_name=action_name
                )
        return actions

    async def _platform_action_wrapper(
        self,
        platform: str,
        action_name: str,
        thought_doc_key: str,
        original_action_description: str,
        **kwargs: dict[str, Any],
    ) -> tuple[bool, str]:
        """
        一个通用的包装器，用于构造平台动作事件并调用 ActionHandler 的执行方法。
        """
        # 构造 action_to_send 字典
        action_to_send = {
            "event_id": str(uuid.uuid4()),  # 为每个具体动作生成唯一ID
            "event_type": f"platform.{platform}.{action_name}",
            "platform": platform,
            "bot_id": config.persona.bot_name,
            **kwargs,  # 将来自LLM的 arguments 直接传递进来
        }

        # 调用 ActionHandler 中通用的执行方法
        # 这个方法处理日志、超时和数据库更新等
        return await self._action_handler._execute_platform_action(
            action_to_send=action_to_send,
            thought_doc_key=thought_doc_key,
            original_action_description=original_action_description,
        )
