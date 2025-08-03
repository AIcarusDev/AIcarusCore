# src/common/interruption_broker.py
import asyncio
import contextlib
from collections import defaultdict
from typing import TYPE_CHECKING

from src.common.custom_logging.logging_config import get_logger

if TYPE_CHECKING:
    from src.focus_chat_mode.chat_session import ChatSession

logger = get_logger(__name__)


class InterruptionEventBroker:
    """一个轻量级的内存事件代理，专门用于处理中断事件的实时推送."""

    def __init__(self) -> None:
        # 主入口队列，所有DMP发布的消息都先进这里
        self._main_queue: asyncio.Queue[dict] = asyncio.Queue()
        # 订阅者映射：{conversation_entity_uid: asyncio.Queue}
        self._subscribers: dict[str, asyncio.Queue] = defaultdict(asyncio.Queue)
        self._dispatch_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        logger.info("中断事件代理 (InterruptionEventBroker) 已初始化。")

    def start(self) -> None:
        """启动后台的分发循环."""
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
                event = await self._main_queue.get()

                # 从事件中提取会话实体UID
                conv_info = event.get("conversation_info", {})
                platform = event.get("platform", "unknown")
                conv_type = conv_info.get("type")
                native_id = conv_info.get("conversation_id")

                if not (conv_type and native_id):
                    continue

                conversation_entity_uid = f"{platform}_{conv_type}_{native_id}"

                async with self._lock:
                    if conversation_entity_uid in self._subscribers:
                        subscriber_queue = self._subscribers[conversation_entity_uid]
                        await subscriber_queue.put(event)
                        logger.debug(
                            f"事件 '{event.get('_key')}' 已成功投递给订阅者 "
                            f"'{conversation_entity_uid}'。"
                        )

            except asyncio.CancelledError:
                logger.info("分发循环被取消。")
                break
            except Exception as e:
                logger.error(f"中断事件分发循环发生未知错误: {e}", exc_info=True)
                await asyncio.sleep(1)  # 发生错误时等待一下，避免刷日志

    async def publish(self, event: dict) -> None:
        """由 DefaultMessageProcessor 调用，发布一个新事件."""
        await self._main_queue.put(event)

    async def subscribe(self, session: "ChatSession") -> asyncio.Queue:
        """由 CoreLogic 的哨兵调用，订阅一个会话的事件."""
        async with self._lock:
            key = session.conversation_id
            logger.info(f"会话 '{key}' 的哨兵正在订阅中断事件。")
            # a defaultdict will create a new queue if it doesn't exist
            return self._subscribers[key]

    async def unsubscribe(self, session: "ChatSession") -> None:
        """由 CoreLogic 的哨兵调用，取消订阅."""
        async with self._lock:
            key = session.conversation_id
            if key in self._subscribers:
                del self._subscribers[key]
                logger.info(f"会话 '{key}' 的哨兵已取消订阅。")
