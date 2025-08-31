# src/common/interruption_broker.py
import asyncio
import contextlib
from typing import TYPE_CHECKING

from src.common.custom_logging.logging_config import get_logger
from src.domain.models import Stimulus

if TYPE_CHECKING:
    from src.os.apps.qq.qq_chat_session import QQChatSession

logger = get_logger(__name__)


class InterruptionEventBroker:
    """一个轻量级的内存事件代理，专门用于处理中断事件的实时推送."""

    def __init__(self) -> None:
        # 主入口队列，现在传输的是我们定义的领域模型 Stimulus
        self._main_queue: asyncio.Queue[Stimulus] = asyncio.Queue()
        # 订阅者映射也同样更新
        self._subscribers: dict[str, asyncio.Queue[Stimulus]] = {}
        self._dispatch_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        logger.info("中断事件代理 (InterruptionEventBroker) 已初始化 (领域驱动改造版)。")

    async def start(self) -> None:
        """启动后台的分发循环, 必须在一个正在运行的事件循环中调用."""
        if not self._dispatch_task or self._dispatch_task.done():
            self._dispatch_task = asyncio.create_task(self._dispatch_loop())
            logger.info("中断事件代理的分发循环已启动。")

    async def stop(self) -> None:
        """停止分发循环."""
        if self._dispatch_task and not self._dispatch_task.done():
            self._dispatch_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._dispatch_task
            logger.info("中断事件代理的分发循环已停止。")

    async def _dispatch_loop(self) -> None:
        """核心分发逻辑，不断从主队列取事件，并投递给对应的订阅者."""
        while True:
            try:
                # 从队列中取出的是 Stimulus 对象
                stimulus = await self._main_queue.get()

                # 从 Stimulus 对象中提取信息
                platform = stimulus.platform
                conv_type = stimulus.conversation_type
                native_id = stimulus.conversation_id

                if not (conv_type and native_id):
                    continue

                conversation_entity_uid = f"{platform}_{conv_type}_{native_id}"

                async with self._lock:
                    if conversation_entity_uid in self._subscribers:
                        subscriber_queue = self._subscribers[conversation_entity_uid]
                        # 将 Stimulus 对象放入订阅者的队列
                        await subscriber_queue.put(stimulus)
                        logger.debug(
                            f"Stimulus (源自事件 '{stimulus.event_id}') 已成功投递给订阅者 "
                            f"'{conversation_entity_uid}'。"
                        )

            except asyncio.CancelledError:
                logger.info("分发循环被取消。")
                break
            except Exception as e:
                logger.error(f"中断事件分发循环发生未知错误: {e}", exc_info=True)
                await asyncio.sleep(1)

    async def publish(self, stimulus: Stimulus) -> None:
        """由 DefaultMessageProcessor 调用，发布一个新事件."""
        # 方法签名和类型提示更新为 Stimulus
        await self._main_queue.put(stimulus)

    async def subscribe(self, session: "QQChatSession") -> asyncio.Queue[Stimulus]:
        """由 CoreLogic 的哨兵调用，订阅一个会话的事件."""
        # 返回值类型提示更新为 asyncio.Queue[Stimulus]
        async with self._lock:
            key = session.conversation_id
            logger.info(f"会话 '{key}' 的哨兵正在订阅中断事件。")
            queue = self._subscribers.get(key)
            if queue is None:
                queue = asyncio.Queue()
                self._subscribers[key] = queue
                logger.debug(f"为会话 '{key}' 创建了新的中断事件队列。")
            return queue

    async def unsubscribe(self, session: "QQChatSession") -> None:
        """由 CoreLogic 的哨兵调用，取消订阅."""
        async with self._lock:
            key = session.conversation_id
            if key in self._subscribers:
                del self._subscribers[key]
                logger.info(f"会话 '{key}' 的哨兵已取消订阅。")
