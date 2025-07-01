# 哼，这是资格证，照着抄就行了
from abc import ABC, abstractmethod
from typing import Any

from aicarus_protocols import Event


class BasePlatformBuilder(ABC):
    """
    平台事件构建器的抽象基类。
    所有平台的“翻译官”都得有这张证，不然不准上岗！
    """

    @property
    @abstractmethod
    def platform_id(self) -> str:
        """
        返回此构建器服务的平台ID (e.g., 'napcat_qq')。
        必须和你 Adapter 在 Core 注册的 ID 一模一样，懂？
        """
        pass

    def parse_incoming_event(self, event: Event) -> Event:
        """
        解析从 Adapter 传来的事件。
        有时候平台会传来一些“土话”Seg，你可以在这里把它翻译成“通用语”。
        不过大部分时候用不着，所以默认啥也不干，直接把原话丢回去。懒得动。
        """
        return event

    @abstractmethod
    def build_action_event(self, intent_data: dict[str, Any]) -> Event | None:
        """
        把核心下的“通用指令”翻译成平台能懂的“土话”事件。
        这是最重要的活儿，干不好就滚蛋！

        Args:
            intent_data (Dict[str, Any]): 一个通用的意图字典，
                比如：{'action_type': 'send_message', 'params': {'conversation_id': '123', 'text': '你好'}}

        Returns:
            一个构造好的、平台专属的 aicarus_protocols.Event 对象，或者在无法翻译时返回 None。
        """
        pass

    @abstractmethod
    def get_action_schema_for_llm(self) -> list[dict[str, Any]]:
        """
        提供一份你们平台的“功能说明书”(JSON Schema格式)，给那个傻乎乎的LLM看。
        这样它才知道你们平台能干嘛，以及怎么指挥你。
        （小色猫乱入：主人，你看这个Schema，像不像我的性感内衣清单？）
        """
        pass
