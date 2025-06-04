# src/core_communication/core_ws_server.py
import asyncio
import json
from collections.abc import Awaitable, Callable

import websockets  # type: ignore
from aicarus_protocols import Event  # 替换 MessageBase 为 Event
from arango.database import StandardDatabase  # 保留用于类型提示，如果 db_instance 被使用
from websockets.exceptions import ConnectionClosed, ConnectionClosedError, ConnectionClosedOK  # 更具体的异常类型
from websockets.server import WebSocketServerProtocol  # type: ignore

from src.common.custom_logging.logger_manager import get_logger

logger = get_logger("AIcarusCore.ws_server")  # 获取日志记录器

# 定义回调函数类型，用于处理从适配器收到的事件
# 参数：解析后的 Event 对象，发送此事件的 WebSocket 连接对象，是否需要持久化标志
AdapterEventCallback = Callable[[Event, WebSocketServerProtocol, bool], Awaitable[None]]


class CoreWebsocketServer:
    def __init__(
        self,
        host: str,
        port: int,
        event_handler_callback: AdapterEventCallback,  # 重命名参数
        db_instance: StandardDatabase | None = None,
    ) -> None:
        self.host: str = host  # 服务器监听的主机地址
        self.port: int = port  # 服务器监听的端口
        self.server: websockets.WebSocketServer | None = None  # WebSocket 服务器实例
        self.connected_adapters: set[WebSocketServerProtocol] = set()  # 存储所有已连接的适配器客户端
        self._event_handler_callback: AdapterEventCallback = event_handler_callback  # 重命名属性
        self._stop_event: asyncio.Event = asyncio.Event()  # 用于优雅停止服务器的事件
        self.db_instance: StandardDatabase | None = db_instance  # 存储数据库实例，如果回调需要通过这里获取

    def _needs_persistence(self, event: Event) -> bool:
        """
        判断事件是否需要持久化存储到数据库

        参数:
            event: 需要判断的事件对象

        返回:
            bool: 如果需要持久化返回True，否则返回False
        """
        # 以下事件类型不需要持久化存储
        non_persistent_types = [
            "meta.heartbeat",  # 心跳包
            "meta.lifecycle.connect",  # 连接建立
            # 可以根据需要添加更多类型
        ]

        return event.event_type not in non_persistent_types

    async def _register_adapter(self, websocket: WebSocketServerProtocol) -> None:
        """注册一个新的适配器连接。"""
        self.connected_adapters.add(websocket)
        logger.info(f"适配器已连接: {websocket.remote_address}. 当前连接数: {len(self.connected_adapters)}")

    async def _unregister_adapter(self, websocket: WebSocketServerProtocol) -> None:
        """注销一个断开的适配器连接。"""
        if websocket in self.connected_adapters:  # 检查是否存在，避免重复移除或处理已关闭的连接
            self.connected_adapters.remove(websocket)
            logger.info(f"适配器已断开: {websocket.remote_address}. 当前连接数: {len(self.connected_adapters)}")
        else:
            logger.debug(f"尝试注销一个未在连接集合中的适配器: {websocket.remote_address}")

    async def _connection_handler(self, websocket: WebSocketServerProtocol, path: str) -> None:
        """处理每个新的 WebSocket 连接。"""
        await self._register_adapter(websocket)
        try:
            # 持续监听来自此连接的消息
            async for message_str in websocket:
                if self._stop_event.is_set():  # 如果服务器正在停止，则不再处理新消息
                    logger.info(f"服务器停止中，忽略来自 {websocket.remote_address} 的新消息。")
                    break

                logger.debug(f"核心 WebSocket 服务器收到原始消息: {message_str[:200]}...")
                try:
                    message_dict = json.loads(message_str)
                    # 验证消息结构是否符合 Event 的基本形态
                    if "event_id" in message_dict and "event_type" in message_dict and "content" in message_dict:
                        try:
                            # 输出详细的消息字典，帮助调试
                            logger.debug(f"准备解析的消息字典: {message_dict}")

                            # 使用协议库的 from_dict 方法将字典转换为 Event 对象
                            aicarus_event = Event.from_dict(message_dict)

                            # 验证事件对象的完整性
                            if not hasattr(aicarus_event, "event_id") or not hasattr(aicarus_event, "event_type"):
                                logger.error(f"解析后的事件对象缺少必要属性: {vars(aicarus_event)}")
                                continue

                            logger.debug(f"解析后的 Event: {aicarus_event.event_type} (ID: {aicarus_event.event_id})")

                            # 判断事件是否需要持久化
                            needs_persistence = self._needs_persistence(aicarus_event)
                            logger.debug(f"事件需要持久化: {needs_persistence}")

                            # 调用注册的回调函数处理解析后的事件，并传递持久化标志
                            try:
                                await self._event_handler_callback(aicarus_event, websocket, needs_persistence)
                                logger.info(
                                    f"事件处理回调已调用: {aicarus_event.event_type} (ID: {aicarus_event.event_id})"
                                )
                            except Exception as e_callback:
                                logger.error(f"事件处理回调执行错误: {e_callback}", exc_info=True)

                        except KeyError as e_key:
                            logger.error(f"解析或处理事件时发生键错误: {e_key}. 数据: {message_dict}", exc_info=True)
                        except AttributeError as e_attr:
                            logger.error(f"访问事件属性时出错: {e_attr}. 数据: {message_dict}", exc_info=True)
                        except Exception as e_parse:
                            logger.error(f"从字典解析 Event 时出错: {e_parse}. 数据: {message_dict}", exc_info=True)
                    else:
                        missing_keys = []
                        for key in ["event_id", "event_type", "content"]:
                            if key not in message_dict:
                                missing_keys.append(key)
                        logger.warning(f"收到的消息结构不像 Event: 缺少 {missing_keys}. 数据: {message_dict}")
                except json.JSONDecodeError as e_json:
                    logger.error(f"从适配器解码 JSON 失败: {e_json}. 原始消息: {message_str[:200]}")
                except Exception as e:  # 捕获回调函数或其他处理中未预料的错误
                    logger.error(f"处理来自适配器的事件时发生错误: {e}", exc_info=True)

        except ConnectionClosedOK:
            logger.info(f"适配器连接正常关闭: {websocket.remote_address}")
        except ConnectionClosedError as e_closed_err:  # 更具体的连接关闭错误
            logger.warning(
                f"适配器连接异常关闭 (错误码: {e_closed_err.code}, 原因: '{e_closed_err.reason}'): {websocket.remote_address}"
            )
        except ConnectionClosed as e_closed:  # 通用的连接关闭异常
            logger.warning(
                f"适配器连接被关闭 (代码: {e_closed.code}, 原因: '{e_closed.reason}'): {websocket.remote_address}"
            )
        except Exception as e:  # 捕获处理连接时可能发生的其他所有异常
            logger.error(f"连接处理器错误 ({websocket.remote_address}): {e}", exc_info=True)
        finally:
            # 确保在连接结束时（无论正常或异常）都注销适配器
            await self._unregister_adapter(websocket)

    async def start(self) -> None:
        """启动 WebSocket 服务器。"""
        if self.server is not None:
            logger.warning("服务器已在运行中。")
            return

        self._stop_event.clear()  # 重置停止事件，允许服务器启动
        logger.info(f"正在启动 AIcarus 核心 WebSocket 服务器，监听地址: ws://{self.host}:{self.port}")
        try:
            # 创建并启动 WebSocket 服务器
            self.server = await websockets.serve(
                self._connection_handler,  # 每个连接的处理函数
                self.host,
                self.port,
                # 可以根据需要增加其他参数，例如 ping_interval, ping_timeout, max_size 等
            )
            logger.info("AIcarus 核心 WebSocket 服务器已成功启动。")
            # 服务器将持续运行，直到 _stop_event 被设置
            await self._stop_event.wait()
        except OSError as e:  # 例如地址已被占用
            logger.critical(f"启动 WebSocket 服务器失败 (ws://{self.host}:{self.port}): {e}", exc_info=True)
            raise  # 将异常向上抛出，以便主程序可以捕获并处理
        except Exception as e:  # 捕获其他所有可能的启动错误
            logger.critical(f"启动或运行 WebSocket 服务器时发生意外错误: {e}", exc_info=True)
            raise
        finally:
            # 确保服务器停止后进行清理
            if self.server and self.server.is_serving():
                self.server.close()
                await self.server.wait_closed()  # 等待服务器完全关闭
                logger.info("AIcarus 核心 WebSocket 服务器已关闭。")
            self.server = None  # 清理服务器实例

    async def stop(self) -> None:
        """停止 WebSocket 服务器并关闭所有连接。"""
        if self._stop_event.is_set():
            logger.info("服务器已经在停止过程中。")
            return

        logger.info("正在停止 AIcarus 核心 WebSocket 服务器...")
        self._stop_event.set()  # 设置停止事件

        # 关闭所有连接
        if self.connected_adapters:
            logger.info(f"正在关闭 {len(self.connected_adapters)} 个连接...")
            for websocket in self.connected_adapters.copy():
                try:
                    await websocket.close()
                except Exception as e:
                    logger.warning(f"关闭连接时出错: {e}")

        # 等待服务器完全停止
        if self.server and self.server.is_serving():
            self.server.close()
            await self.server.wait_closed()

        logger.info("AIcarus 核心 WebSocket 服务器已停止。")

    async def broadcast_action_to_adapters(self, action_event: Event) -> bool:
        """向所有连接的适配器广播动作事件"""
        if not self.connected_adapters:
            logger.warning("没有连接的适配器，无法广播动作")
            return False

        try:
            # 将事件序列化为JSON
            action_data = action_event.to_dict()
            action_json = json.dumps(action_data, ensure_ascii=False)

            success_count = 0
            total_adapters = len(self.connected_adapters)

            # 向所有适配器发送动作
            for websocket in self.connected_adapters.copy():
                try:
                    await websocket.send(action_json)
                    success_count += 1
                    logger.debug(f"成功向适配器 {websocket.remote_address} 发送动作")
                except Exception as e:
                    logger.error(f"向适配器 {websocket.remote_address} 发送动作失败: {e}")
                    # 如果连接已断开，从列表中移除
                    await self._unregister_adapter(websocket)

            logger.info(f"动作广播完成: {success_count}/{total_adapters} 个适配器成功接收")
            return success_count > 0

        except Exception as e:
            logger.error(f"广播动作时发生错误: {e}", exc_info=True)
            return False

    async def send_action_to_specific_adapter(self, websocket: WebSocketServerProtocol, action_event: Event) -> bool:
        """向指定的适配器连接发送一个动作事件。"""
        if websocket not in self.connected_adapters:
            logger.warning(f"尝试向一个未注册或已断开的适配器发送动作: {websocket.remote_address}")
            return False

        try:
            message_json = json.dumps(action_event.to_dict(), ensure_ascii=False)
        except Exception as e_json:
            logger.error(f"序列化单个动作事件为 JSON 时出错: {e_json}", exc_info=True)
            return False

        logger.debug(f"核心 WebSocket 服务器向 {websocket.remote_address} 发送动作: {message_json[:200]}...")
        try:
            await websocket.send(message_json)
            logger.info(f"动作已发送给适配器 {websocket.remote_address}。")
            return True
        except ConnectionClosed:
            logger.warning(f"向特定适配器 {websocket.remote_address} 发送动作失败: 连接已关闭。")
            await self._unregister_adapter(websocket)  # 确保注销
            return False
        except Exception as e:  # 其他发送错误
            logger.error(f"向特定适配器 {websocket.remote_address} 发送动作时发生错误: {e}")
            return False
