# src/core_communication/action_sender.py
import asyncio
import json
from typing import Any

from websockets.exceptions import ConnectionClosed
from websockets.server import WebSocketServerProtocol

from src.common.custom_logging.logging_config import get_logger

logger = get_logger(__name__)


class ActionSender:
    """
    负责向适配器发送动作。
    它维护一个适配器ID到WebSocket连接的映射，并提供发送动作的接口。
    """

    def __init__(self) -> None:
        # 这两个字典将由外部（新的 ConnectionManager 或 CoreWebsocketServer）在适配器注册/注销时更新
        self.connected_adapters: dict[str, WebSocketServerProtocol] = {}
        self.adapter_clients_info: dict[str, dict[str, Any]] = {}
        self._websocket_to_adapter_id: dict[WebSocketServerProtocol, str] = {}
        logger.info("ActionSender 初始化完成。")

    def register_adapter(self, adapter_id: str, display_name: str, websocket: WebSocketServerProtocol) -> None:
        """由外部调用，用于注册一个新的适配器连接。"""
        self.connected_adapters[adapter_id] = websocket
        self._websocket_to_adapter_id[websocket] = adapter_id
        # adapter_clients_info 也需要被管理，但它的更新逻辑可能更适合放在连接管理器中
        # 这里为了简化，暂时也由 ActionSender 管理
        self.adapter_clients_info[adapter_id] = {
            "websocket": websocket,
            "display_name": display_name,
        }
        logger.debug(f"ActionSender: 适配器 '{display_name}({adapter_id})' 已注册。")

    def unregister_adapter(self, websocket: WebSocketServerProtocol) -> str | None:
        """由外部调用，用于注销一个适配器连接。"""
        adapter_id = self._websocket_to_adapter_id.pop(websocket, None)
        if adapter_id:
            self.connected_adapters.pop(adapter_id, None)
            self.adapter_clients_info.pop(adapter_id, None)
            logger.debug(f"ActionSender: 适配器 '{adapter_id}' 已注销。")
        return adapter_id

    async def broadcast_action_to_adapters(self, action_event: dict[str, Any]) -> bool:
        """向所有连接的适配器广播一个动作。"""
        if not self.connected_adapters:
            logger.warning("没有连接的适配器，无法广播动作")
            return False
        try:
            action_json = json.dumps(action_event, ensure_ascii=False)
            results = await asyncio.gather(
                *(ws.send(action_json) for ws in self.connected_adapters.values()), return_exceptions=True
            )
            success_count = sum(1 for res in results if not isinstance(res, Exception))
            for i, res in enumerate(results):
                if isinstance(res, Exception):
                    # 这是一个近似的查找，因为字典在gather期间可能改变
                    adapter_id = list(self.connected_adapters.keys())[i]
                    logger.error(f"向适配器 '{adapter_id}' 发送广播失败: {res}")
            logger.info(f"动作广播完成: {success_count}/{len(self.connected_adapters)} 个适配器尝试发送")
            return success_count > 0
        except Exception as e:
            logger.error(f"广播动作时发生错误: {e}", exc_info=True)
            return False

    async def send_action_to_specific_adapter(
        self, websocket: WebSocketServerProtocol, action_event: dict[str, Any]
    ) -> bool:
        """向指定的WebSocket连接发送一个动作。"""
        adapter_id = self._websocket_to_adapter_id.get(websocket)
        display_name = self.adapter_clients_info.get(adapter_id, {}).get("display_name", adapter_id or "未知")
        if not adapter_id or self.connected_adapters.get(adapter_id) is not websocket:
            logger.warning(
                f"尝试向一个未注册、ID不匹配或已断开的适配器 '{display_name}' 发送动作: {websocket.remote_address}"
            )
            return False
        try:
            message_json = json.dumps(action_event, ensure_ascii=False)
        except Exception as e_json:
            logger.error(f"序列化单个动作事件为 JSON 时出错 (目标: '{display_name}'): {e_json}", exc_info=True)
            return False
        try:
            await websocket.send(message_json)
            return True
        except ConnectionClosed:
            logger.warning(f"向特定适配器 '{display_name}' 发送动作失败: 连接已关闭.")
            return False
        except Exception as e:
            logger.error(f"向特定适配器 '{display_name}' 发送动作时发生错误: {e}")
            return False

    async def send_action_to_adapter_by_id(self, adapter_id: str, action_event: dict[str, Any]) -> bool:
        """通过适配器ID向其发送一个动作。"""
        websocket = self.connected_adapters.get(adapter_id)
        if not websocket:
            logger.warning(f"主人～ 没找到ID为 '{adapter_id}' 的适配器，它可能害羞跑掉了。")
            return False
        return await self.send_action_to_specific_adapter(websocket, action_event)
