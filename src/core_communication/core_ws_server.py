# src/core_communication/core_ws_server.py
import asyncio
import json
from collections.abc import Awaitable, Callable

import websockets  # type: ignore
from aicarus_protocols import MessageBase
from websockets.exceptions import ConnectionClosed, ConnectionClosedError, ConnectionClosedOK  # 更具体的异常类型
from websockets.server import WebSocketServerProtocol  # type: ignore

from src.common.custom_logging.logger_manager import get_logger  # 假设你的日志模块路径

logger = get_logger("AIcarusCore.ws_server")  # 获取日志记录器

# 定义回调函数类型，用于处理从适配器收到的消息
# 参数：解析后的 MessageBase 对象，发送此消息的 WebSocket 连接对象
AdapterMessageCallback = Callable[[MessageBase, WebSocketServerProtocol], Awaitable[None]]


class CoreWebsocketServer:
    def __init__(self, host: str, port: int, message_handler_callback: AdapterMessageCallback) -> None:
        self.host: str = host  # 服务器监听的主机地址
        self.port: int = port  # 服务器监听的端口
        self.server: websockets.WebSocketServer | None = None  # WebSocket 服务器实例
        self.connected_adapters: set[WebSocketServerProtocol] = set()  # 存储所有已连接的适配器客户端
        self._message_handler_callback: AdapterMessageCallback = message_handler_callback  # 处理接收到消息的回调函数
        self._stop_event: asyncio.Event = asyncio.Event()  # 用于优雅停止服务器的事件

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
                    # 简单验证消息结构是否符合 MessageBase 的基本形态
                    if "message_info" in message_dict and "message_segment" in message_dict:
                        try:
                            # 使用协议库的 from_dict 方法将字典转换为 MessageBase 对象
                            aicarus_msg = MessageBase.from_dict(message_dict)
                            # 调用注册的回调函数处理解析后的消息
                            await self._message_handler_callback(aicarus_msg, websocket)
                        except Exception as e_parse:
                            logger.error(
                                f"从字典解析 MessageBase 时出错: {e_parse}. 数据: {message_dict}", exc_info=True
                            )
                    else:
                        logger.warning(f"收到的消息结构不像 MessageBase: {message_dict}")
                except json.JSONDecodeError:
                    logger.error(f"从适配器解码 JSON 失败: {message_str}")
                except Exception as e:
                    logger.error(f"处理来自适配器的消息时发生错误: {e}", exc_info=True)

        except ConnectionClosedOK:
            logger.info(f"适配器连接正常关闭: {websocket.remote_address}")
        except ConnectionClosedError as e_closed_err:
            logger.warning(
                f"适配器连接异常关闭 (错误码: {e_closed_err.code}, 原因: '{e_closed_err.reason}'): {websocket.remote_address}"
            )
        except ConnectionClosed as e_closed:  # 通用 ConnectionClosed，应放在更具体的之后
            logger.warning(
                f"适配器连接被关闭 (代码: {e_closed.code}, 原因: '{e_closed.reason}'): {websocket.remote_address}"
            )
        except Exception as e:
            # 捕获处理连接时可能发生的其他所有异常
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
                # max_size=2**20,  # 示例：允许最大消息为 1MB
                # ping_interval=20, # 每20秒发送一次ping
                # ping_timeout=20,  # 20秒内未收到pong则认为连接超时
            )
            logger.info("AIcarus 核心 WebSocket 服务器已成功启动。")
            # 服务器将持续运行，直到 _stop_event 被设置
            await self._stop_event.wait()
        except OSError as e:  # 例如地址已被占用
            logger.critical(f"启动 WebSocket 服务器失败 (ws://{self.host}:{self.port}): {e}", exc_info=True)
            raise  # 将异常向上抛出，以便主程序可以捕获并处理
        except Exception as e:
            logger.critical(f"启动或运行 WebSocket 服务器时发生意外错误: {e}", exc_info=True)
            raise
        finally:
            # 确保服务器停止后进行清理
            if self.server and self.server.is_serving():
                self.server.close()
                await self.server.wait_closed()
                logger.info("AIcarus 核心 WebSocket 服务器已关闭。")
            self.server = None  # 清理服务器实例

    async def stop(self) -> None:
        """停止 WebSocket 服务器并关闭所有连接。"""
        logger.info("正在尝试停止 AIcarus 核心 WebSocket 服务器...")
        self._stop_event.set()  # 设置事件，通知 _connection_handler 和 start 方法中的 wait 退出

        # 关闭服务器主套接字，这将阻止新的连接
        if self.server:
            self.server.close()
            try:
                # 等待服务器完全关闭
                await asyncio.wait_for(self.server.wait_closed(), timeout=5.0)
                logger.info("WebSocket 服务器套接字已优雅关闭。")
            except TimeoutError:
                logger.warning("等待 WebSocket 服务器套接字关闭超时。可能已关闭或卡住。")
            except Exception as e:  # 处理其他可能的异常
                logger.error(f"关闭服务器套接字时发生错误: {e}", exc_info=True)
            self.server = None  # 清理服务器实例

        # 关闭所有当前活动的适配器连接
        if self.connected_adapters:
            logger.info(f"正在关闭 {len(self.connected_adapters)} 个活动的适配器连接...")
            # 创建关闭任务列表
            close_tasks = [
                adapter.close(code=1001, reason="服务器正在关闭")
                for adapter in list(self.connected_adapters)  # 使用副本进行迭代
            ]
            # 并发执行关闭任务，并收集结果（忽略异常，因为我们只是想确保尝试关闭）
            results = await asyncio.gather(*close_tasks, return_exceptions=True)
            for i, result in enumerate(results):
                adapter = list(self.connected_adapters)[i]  # 获取对应的 adapter
                if isinstance(result, Exception):
                    logger.warning(f"关闭适配器 {adapter.remote_address} 连接时发生错误: {result}")

            # 清空连接集合 (unregister 会在连接处理器中被调用，但这里也清一下以防万一)
            self.connected_adapters.clear()

        logger.info("AIcarus 核心 WebSocket 服务器停止流程完成。")

    async def broadcast_action_to_adapters(self, action_message: MessageBase) -> bool:
        """向所有已连接的适配器广播一个动作消息。"""
        if not self.connected_adapters:
            logger.warning("没有适配器连接，无法广播动作。")
            return False

        try:
            # 将 MessageBase 对象转换为 JSON 字符串
            message_json = json.dumps(action_message.to_dict(), ensure_ascii=False)
        except Exception as e_json:
            logger.error(f"序列化动作消息为 JSON 时出错: {e_json}", exc_info=True)
            return False

        logger.debug(f"核心 WebSocket 服务器广播动作: {message_json[:200]}...")

        disconnected_during_send: set[WebSocketServerProtocol] = set()
        successful_sends = 0

        # 遍历连接的适配器副本，因为在发送过程中可能有连接断开导致集合变化
        for websocket in list(self.connected_adapters):
            try:
                await websocket.send(message_json)
                successful_sends += 1
            except ConnectionClosed:
                logger.warning(f"向适配器 {websocket.remote_address} 发送动作失败: 连接已关闭。")
                disconnected_during_send.add(websocket)
            except Exception as e:
                logger.error(f"向适配器 {websocket.remote_address} 发送动作时发生错误: {e}")
                # 也可以考虑将此适配器标记为断开
                # disconnected_during_send.add(websocket)

        # 清理在发送过程中断开的适配器
        for ws in disconnected_during_send:
            await self._unregister_adapter(ws)  # unregister 会检查是否存在

        if successful_sends > 0:
            total_attempted = len(list(self.connected_adapters)) + len(disconnected_during_send)  # 计算尝试发送的总数
            logger.info(f"动作已广播给 {successful_sends}/{total_attempted} 个适配器。")
            return True
        else:
            logger.warning("动作广播失败，未能成功发送给任何适配器或当前没有有效连接。")
            return False

    async def send_action_to_specific_adapter(
        self, websocket: WebSocketServerProtocol, action_message: MessageBase
    ) -> bool:
        """向指定的适配器连接发送一个动作消息。"""
        if websocket not in self.connected_adapters:
            logger.warning(f"尝试向一个未注册或已断开的适配器发送动作: {websocket.remote_address}")
            return False

        try:
            message_json = json.dumps(action_message.to_dict(), ensure_ascii=False)
        except Exception as e_json:
            logger.error(f"序列化单个动作消息为 JSON 时出错: {e_json}", exc_info=True)
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
        except Exception as e:
            logger.error(f"向特定适配器 {websocket.remote_address} 发送动作时发生错误: {e}")
            return False
