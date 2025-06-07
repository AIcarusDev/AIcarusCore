# src/core_communication/core_ws_server.py
import asyncio
import json
import time # 用于时间戳
import uuid # 用于生成事件ID
from typing import Any # 新增
from collections.abc import Awaitable, Callable

import websockets  # type: ignore
# Event 重命名为 ProtocolEvent, 导入 SegBuilder
from aicarus_protocols import Event as ProtocolEvent, SegBuilder, ConversationInfo # 移除了不存在的 ConversationType
from arango.database import StandardDatabase  # 保留用于类型提示，如果 db_instance 被使用
from websockets.exceptions import ConnectionClosed, ConnectionClosedError, ConnectionClosedOK  # 更具体的异常类型
from websockets.server import WebSocketServerProtocol  # type: ignore

from src.common.custom_logging.logger_manager import get_logger
# 假设 EventStorageService 的导入路径, 主人您可能需要确认一下哦
from src.database.services.event_storage_service import EventStorageService
from src.config import config # 导入全局配置获取bot_name


logger = get_logger("AIcarusCore.ws_server")  # 获取日志记录器

# 定义回调函数类型，用于处理从适配器收到的事件
# 参数：解析后的 Event 对象，发送此事件的 WebSocket 连接对象，是否需要持久化标志
AdapterEventCallback = Callable[[ProtocolEvent, WebSocketServerProtocol, bool], Awaitable[None]]


class CoreWebsocketServer:
    HEARTBEAT_CLIENT_INTERVAL_SECONDS = 30  # 客户端应该发送心跳的间隔 (参考值)
    HEARTBEAT_SERVER_TIMEOUT_SECONDS = 90   # 服务器判定超时的阈值
    HEARTBEAT_SERVER_CHECK_INTERVAL_SECONDS = 15 # 服务器检查超时的频率

    def __init__(
        self,
        host: str,
        port: int,
        event_handler_callback: AdapterEventCallback,
        event_storage_service: EventStorageService, # 注入EventStorageService
        db_instance: StandardDatabase | None = None, # db_instance 可能不再直接需要
    ) -> None:
        self.host: str = host
        self.port: int = port
        self.server: websockets.WebSocketServer | None = None
        self.event_storage_service = event_storage_service # 保存注入的service实例
        
        self.connected_adapters: dict[str, WebSocketServerProtocol] = {}
        self.adapter_clients_info: dict[str, dict[str, Any]] = {}
        # 结构: {"adapter_id": {"websocket": ws, "last_heartbeat": timestamp, "display_name": "Adapter Name"}}

        self._event_handler_callback: AdapterEventCallback = event_handler_callback
        self._stop_event: asyncio.Event = asyncio.Event()
        self.db_instance: StandardDatabase | None = db_instance 
        self._websocket_to_adapter_id: dict[WebSocketServerProtocol, str] = {}
        self._heartbeat_check_task: asyncio.Task | None = None 

    async def _generate_and_store_system_event(self, adapter_id: str, display_name: str, event_type: str, reason: str = "") -> None:
        """生成连接/断开类型的系统事件并存储。"""
        current_timestamp = time.time()
        
        if event_type == "meta.lifecycle.adapter_connected":
            event_content_text = f"[状态] {display_name}({adapter_id})连接成功"
        elif event_type == "meta.lifecycle.adapter_disconnected":
            event_content_text = f"[状态] {display_name}({adapter_id})断开({reason})"
        else:
            logger.warning(f"尝试生成未知的系统级生命周期事件类型: {event_type} for adapter {adapter_id}")
            return

        system_event = ProtocolEvent(
            event_id=f"core_event_{adapter_id}_{event_type.split('.')[-1]}_{int(current_timestamp)}_{uuid.uuid4().hex[:6]}",
            event_type=event_type, 
            platform="core_system", 
            bot_id=config.persona.bot_name, 
            time=int(current_timestamp * 1000),
            content=[SegBuilder.text(event_content_text)],
            conversation_info=ConversationInfo(conversation_id="system_events", type="system") # 使用字符串 "system"
        )
        
        if self.event_storage_service:
            try:
                await self.event_storage_service.save_event_document(system_event.to_dict())
                logger.info(f"已生成并存储系统事件: {event_content_text}")
            except Exception as e:
                logger.error(f"存储系统事件 for '{adapter_id}' (type: {event_type}) 失败: {e}", exc_info=True)
        else:
            logger.warning(f"EventStorageService 未初始化，无法存储系统事件 for '{adapter_id}'.")

    def _needs_persistence(self, event: ProtocolEvent) -> bool:
        """判断从客户端收到的事件是否需要持久化。"""
        non_persistent_types = [
            "meta.lifecycle.connect",  # 客户端发来的原始连接事件不持久化
            "meta.lifecycle.disconnect", # 客户端发来的原始断开事件不持久化 (我们会生成自己的系统事件)
        ]
        return event.event_type not in non_persistent_types

    async def _register_adapter(self, adapter_id: str, display_name: str, websocket: WebSocketServerProtocol) -> None:
        current_timestamp = time.time()
        self.connected_adapters[adapter_id] = websocket
        self._websocket_to_adapter_id[websocket] = adapter_id 
        self.adapter_clients_info[adapter_id] = {
            "websocket": websocket,
            "last_heartbeat": current_timestamp, 
            "display_name": display_name
        }
        logger.info(f"适配器 '{display_name}({adapter_id})' 已连接: {websocket.remote_address}. 当前连接数: {len(self.connected_adapters)}")
        await self._generate_and_store_system_event(adapter_id, display_name, "meta.lifecycle.adapter_connected")

    async def _unregister_adapter(self, websocket: WebSocketServerProtocol, reason: str = "连接关闭") -> None:
        adapter_id = self._websocket_to_adapter_id.pop(websocket, None)
        
        if adapter_id: 
            if adapter_id in self.connected_adapters:
                del self.connected_adapters[adapter_id]
            
            adapter_info = self.adapter_clients_info.pop(adapter_id, None)
            
            if adapter_info: 
                display_name = adapter_info.get("display_name", adapter_id)
                logger.info(f"适配器 '{display_name}({adapter_id})' 已断开 ({reason}): {websocket.remote_address}. 当前连接数: {len(self.connected_adapters)}")
                await self._generate_and_store_system_event(adapter_id, display_name, "meta.lifecycle.adapter_disconnected", reason)
            else: 
                logger.info(f"适配器 '{adapter_id}' (详细信息缺失) 连接关闭 ({reason}): {websocket.remote_address}. 当前连接数: {len(self.connected_adapters)}")
                await self._generate_and_store_system_event(adapter_id, adapter_id, "meta.lifecycle.adapter_disconnected", reason)
        else: 
            logger.debug(f"尝试注销一个未在ID映射中找到或已被注销的适配器连接 ({reason}): {websocket.remote_address}")


    async def _handle_registration(self, websocket: WebSocketServerProtocol) -> tuple[str, str] | None:
        try:
            registration_message_str = await asyncio.wait_for(websocket.recv(), timeout=10.0) 
            logger.debug(f"收到来自 {websocket.remote_address} 的连接/注册尝试消息: {registration_message_str[:200]}")
            message_dict = json.loads(registration_message_str)
            
            adapter_id_found: str | None = None
            display_name_found: str | None = None

            if message_dict.get("event_type") == "meta.lifecycle.connect":
                content_list = message_dict.get("content")
                if isinstance(content_list, list) and len(content_list) > 0:
                    first_seg = content_list[0]
                    if isinstance(first_seg, dict) and first_seg.get("type") == "meta.lifecycle":
                        data_dict = first_seg.get("data")
                        if isinstance(data_dict, dict):
                            details_dict = data_dict.get("details")
                            if isinstance(details_dict, dict):
                                adapter_id_candidate = details_dict.get("adapter_id")
                                if isinstance(adapter_id_candidate, str) and adapter_id_candidate.strip():
                                    adapter_id_found = adapter_id_candidate.strip()
                                
                                display_name_candidate = details_dict.get("display_name")
                                if isinstance(display_name_candidate, str) and display_name_candidate.strip():
                                    display_name_found = display_name_candidate.strip()
                                elif adapter_id_found: 
                                    display_name_found = adapter_id_found 
            
            if adapter_id_found and display_name_found:
                if adapter_id_found in self.connected_adapters:
                    logger.warning(f"适配器 '{adapter_id_found}' 尝试重复注册。旧连接将被新连接取代。")
                    old_websocket = self.connected_adapters.get(adapter_id_found)
                    if old_websocket and old_websocket != websocket:
                        await self._unregister_adapter(old_websocket, reason="被新连接取代")
                        try:
                            await old_websocket.close(code=1001, reason="Replaced by new connection")
                        except Exception: pass
                
                logger.info(f"适配器通过 meta.lifecycle.connect 注册成功: ID='{adapter_id_found}', DisplayName='{display_name_found}', 地址={websocket.remote_address}")
                return adapter_id_found, display_name_found
            else:
                logger.warning(f"未能从 meta.lifecycle.connect 消息中提取有效 adapter_id 和/或 display_name. 连接 {websocket.remote_address}.")
        except asyncio.TimeoutError:
            logger.warning(f"等待适配器 {websocket.remote_address} 发送连接/注册消息超时.")
        except json.JSONDecodeError:
            logger.error(f"解码来自 {websocket.remote_address} 的连接/注册消息JSON失败.")
        except Exception as e:
            logger.error(f"处理适配器 {websocket.remote_address} 连接/注册时发生意外: {e}", exc_info=True)
        
        await websocket.close(code=1008, reason="Invalid or missing registration information") 
        return None

    async def _connection_handler(self, websocket: WebSocketServerProtocol, path: str) -> None:
        adapter_id_for_handler: str | None = None 
        display_name_for_handler: str | None = None

        registration_info = await self._handle_registration(websocket)
        if not registration_info:
            return 

        adapter_id, display_name = registration_info
        adapter_id_for_handler = adapter_id 
        display_name_for_handler = display_name
        await self._register_adapter(adapter_id, display_name, websocket)
        
        try:
            async for message_str in websocket:
                if self._stop_event.is_set():
                    break
                logger.debug(f"核心 WebSocket 服务器收到来自适配器 '{display_name}({adapter_id})' 的原始消息: {message_str[:200]}...")
                try:
                    message_dict = json.loads(message_str)
                    msg_event_type = message_dict.get("event_type")

                    if message_dict.get("type") == "heartbeat": 
                        hb_adapter_id = message_dict.get("adapter_id")
                        if hb_adapter_id == adapter_id and adapter_id in self.adapter_clients_info:
                            self.adapter_clients_info[adapter_id]["last_heartbeat"] = time.time()
                            logger.debug(f"收到来自 '{display_name}({adapter_id})' 的心跳包.")
                        else:
                            logger.warning(f"收到来自 {websocket.remote_address} 的无效或不匹配的心跳包: {message_dict}")
                        continue 
                    
                    elif msg_event_type == "meta.lifecycle.disconnect": 
                        logger.info(f"收到来自适配器 '{display_name}({adapter_id})' 的主动断开通知 (meta.lifecycle.disconnect)。")
                        client_reason_detail = "适配器主动下线" 
                        try: 
                            content_list = message_dict.get("content")
                            if isinstance(content_list, list) and len(content_list) > 0:
                                first_seg = content_list[0]
                                if isinstance(first_seg, dict) and first_seg.get("type") == "meta.lifecycle":
                                    data_dict = first_seg.get("data")
                                    if isinstance(data_dict, dict) and data_dict.get("lifecycle_type") == "disconnect":
                                        details_dict = data_dict.get("details")
                                        if isinstance(details_dict, dict) and details_dict.get("reason"):
                                            client_reason_detail = f"适配器主动下线 ({details_dict.get('reason')})"
                        except Exception as e_reason_parse:
                            logger.warning(f"解析客户端 '{adapter_id}' 断开原因时出错: {e_reason_parse}")
                        
                        await self._unregister_adapter(websocket, reason=client_reason_detail)
                        logger.info(f"正在关闭与主动断开的适配器 '{display_name}({adapter_id})' 的连接。")
                        try:
                            await websocket.close(code=1000, reason="Client initiated disconnect acknowledged by server")
                        except Exception as e_close_ack:
                            logger.warning(f"关闭客户端 '{adapter_id}' 主动断开的连接时出错: {e_close_ack}")
                        break 

                    elif msg_event_type == "meta.lifecycle.connect": 
                        logger.warning(f"适配器 '{display_name}({adapter_id})' 在已连接状态下再次发送 meta.lifecycle.connect，已忽略。")
                        continue
                    
                    elif "event_id" in message_dict and msg_event_type and "content" in message_dict: 
                        try:
                            aicarus_event = ProtocolEvent.from_dict(message_dict)
                            if not hasattr(aicarus_event, "event_id") or not hasattr(aicarus_event, "event_type"):
                                logger.error(f"解析后的事件对象缺少必要属性: {vars(aicarus_event)}")
                                continue
                            
                            needs_persistence = self._needs_persistence(aicarus_event)
                            await self._event_handler_callback(aicarus_event, websocket, needs_persistence)
                        except Exception as e_parse:
                            logger.error(f"解析或处理 Event 时出错: {e_parse}. 数据: {message_dict}", exc_info=True)
                    else:
                        logger.warning(f"收到的消息结构不像 Event (缺少关键key) 或已知类型. 数据: {message_dict}")
                except json.JSONDecodeError:
                    logger.error(f"从适配器 '{display_name}({adapter_id})' 解码 JSON 失败. 原始消息: {message_str[:200]}")
                except Exception as e:
                    logger.error(f"处理来自适配器 '{display_name}({adapter_id})' 的消息时发生错误: {e}", exc_info=True)

        except ConnectionClosedOK:
            logger.info(f"适配器 '{display_name_for_handler or adapter_id_for_handler or '未知'}' 连接正常关闭.")
            await self._unregister_adapter(websocket, reason="连接正常关闭")
        except ConnectionClosedError as e_closed_err:
            logger.warning(f"适配器 '{display_name_for_handler or adapter_id_for_handler or '未知'}' 连接异常关闭 (错误码: {e_closed_err.code}, 原因: '{e_closed_err.reason}')")
            await self._unregister_adapter(websocket, reason=f"连接异常关闭 (code: {e_closed_err.code})")
        except ConnectionClosed as e_closed: 
            logger.warning(f"适配器 '{display_name_for_handler or adapter_id_for_handler or '未知'}' 连接被关闭 (代码: {e_closed.code}, 原因: '{e_closed.reason}')")
            await self._unregister_adapter(websocket, reason=f"连接被关闭 (code: {e_closed.code})")
        except Exception as e:
            logger.error(f"连接处理器错误 (适配器 '{display_name_for_handler or adapter_id_for_handler or '未知'}'): {e}", exc_info=True)
            await self._unregister_adapter(websocket, reason="未知错误导致断开")
        finally:
            if websocket in self._websocket_to_adapter_id: 
                 logger.info(f"连接处理循环结束 for '{display_name_for_handler or adapter_id_for_handler or '未知'}', 执行最终注销.")
                 await self._unregister_adapter(websocket, reason="连接处理结束")
    
    async def _check_heartbeat_timeouts(self) -> None:
        logger.info("心跳超时检查任务已启动。")
        while not self._stop_event.is_set():
            await asyncio.sleep(self.HEARTBEAT_SERVER_CHECK_INTERVAL_SECONDS)
            if self._stop_event.is_set(): break

            current_time = time.time()
            for adapter_id, info in list(self.adapter_clients_info.items()):
                if current_time - info.get("last_heartbeat", 0) > self.HEARTBEAT_SERVER_TIMEOUT_SECONDS:
                    display_name = info.get("display_name", adapter_id)
                    websocket_to_close = info.get("websocket")
                    logger.warning(f"适配器 '{display_name}({adapter_id})' 心跳超时.")
                    
                    if websocket_to_close:
                        await self._unregister_adapter(websocket_to_close, reason="心跳超时")
                        try:
                            await websocket_to_close.close(code=1000, reason="Heartbeat timeout by server")
                            logger.info(f"已关闭适配器 '{display_name}({adapter_id})' 的超时连接。")
                        except Exception as e_close:
                            logger.error(f"关闭适配器 '{display_name}({adapter_id})' 的超时连接时出错: {e_close}")
                    else: 
                         logger.warning(f"适配器 '{display_name}({adapter_id})' 在心跳超时检查中发现无websocket对象，但仍在追踪，尝试清理。")
                         self.adapter_clients_info.pop(adapter_id, None) 
                         if adapter_id in self.connected_adapters: del self.connected_adapters[adapter_id]
                         await self._generate_and_store_system_event(adapter_id, display_name, "meta.lifecycle.adapter_disconnected", "心跳超时 (无websocket对象)")
        logger.info("心跳超时检查任务已停止。")

    async def start(self) -> None:
        if self.server is not None: logger.warning("服务器已在运行中."); return
        self._stop_event.clear()
        logger.info(f"正在启动 AIcarus 核心 WebSocket 服务器，监听地址: ws://{self.host}:{self.port}")
        try:
            self.server = await websockets.serve(self._connection_handler, self.host, self.port)
            self._heartbeat_check_task = asyncio.create_task(self._check_heartbeat_timeouts())
            logger.info("AIcarus 核心 WebSocket 服务器已成功启动，心跳检查已部署。")
            await self._stop_event.wait()
        except OSError as e:
            logger.critical(f"启动 WebSocket 服务器失败 (ws://{self.host}:{self.port}): {e}", exc_info=True)
            if self._heartbeat_check_task and not self._heartbeat_check_task.done(): self._heartbeat_check_task.cancel()
            raise 
        except Exception as e:
            logger.critical(f"启动或运行 WebSocket 服务器时发生意外错误: {e}", exc_info=True)
            if self._heartbeat_check_task and not self._heartbeat_check_task.done(): self._heartbeat_check_task.cancel()
            raise
        finally:
            if self._heartbeat_check_task and not self._heartbeat_check_task.done():
                logger.info("正在取消心跳检查任务...")
                self._heartbeat_check_task.cancel()
                try: await self._heartbeat_check_task
                except asyncio.CancelledError: logger.info("心跳检查任务已成功取消。")
            if self.server and self.server.is_serving():
                self.server.close()
                await self.server.wait_closed()
                logger.info("AIcarus 核心 WebSocket 服务器已关闭。")
            self.server = None

    async def stop(self) -> None:
        if self._stop_event.is_set(): logger.info("服务器已经在停止过程中."); return
        logger.info("正在停止 AIcarus 核心 WebSocket 服务器...")
        self._stop_event.set() 

        if self._heartbeat_check_task and not self._heartbeat_check_task.done():
            logger.info("正在取消心跳检查任务 (来自stop方法)...")
            self._heartbeat_check_task.cancel()
            try: await self._heartbeat_check_task
            except asyncio.CancelledError: logger.info("心跳检查任务已成功取消 (来自stop方法)。")
            self._heartbeat_check_task = None 

        active_connections_ws_list = list(self.connected_adapters.values()) 
        if active_connections_ws_list:
            logger.info(f"正在关闭 {len(active_connections_ws_list)} 个活动的适配器连接...")
            for websocket in active_connections_ws_list: 
                try:
                    await websocket.close(code=1001, reason="Server shutting down")
                except Exception as e_close_conn:
                    adapter_id_log = self._websocket_to_adapter_id.get(websocket, "未知ID")
                    logger.warning(f"服务器停止时关闭适配器 '{adapter_id_log}' 的连接出错: {e_close_conn}")
        else:
            logger.info("服务器停止时没有活动的适配器连接需要关闭。")
        
        if self.server and self.server.is_serving():
            self.server.close() 
            await self.server.wait_closed() 

        logger.info("AIcarus 核心 WebSocket 服务器已停止。")

    async def broadcast_action_to_adapters(self, action_event: ProtocolEvent) -> bool:
        if not self.connected_adapters: logger.warning("没有连接的适配器，无法广播动作"); return False
        try:
            action_json = json.dumps(action_event.to_dict(), ensure_ascii=False)
            success_count = 0
            for adapter_id, websocket in list(self.connected_adapters.items()):
                try:
                    await websocket.send(action_json)
                    success_count += 1
                except Exception as e:
                    logger.error(f"向适配器 '{self.adapter_clients_info.get(adapter_id, {}).get('display_name', adapter_id)}' 发送广播动作失败: {e}")
            logger.info(f"动作广播完成: {success_count}/{len(self.connected_adapters)} 个适配器尝试发送")
            return success_count > 0
        except Exception as e: logger.error(f"广播动作时发生错误: {e}", exc_info=True); return False

    async def send_action_to_specific_adapter(self, websocket: WebSocketServerProtocol, action_event: ProtocolEvent) -> bool:
        adapter_id = self._websocket_to_adapter_id.get(websocket)
        display_name = self.adapter_clients_info.get(adapter_id, {}).get('display_name', adapter_id or "未知")

        if not adapter_id or self.connected_adapters.get(adapter_id) is not websocket:
            logger.warning(f"尝试向一个未注册、ID不匹配或已断开的适配器 '{display_name}' 发送动作: {websocket.remote_address}")
            return False
        try:
            message_json = json.dumps(action_event.to_dict(), ensure_ascii=False)
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

    async def send_action_to_adapter_by_id(self, adapter_id: str, action_event: ProtocolEvent) -> bool:
        websocket = self.connected_adapters.get(adapter_id)
        if not websocket:
            logger.warning(f"主人～ 没找到ID为 '{adapter_id}' 的适配器，它可能害羞跑掉了。")
            return False
        return await self.send_action_to_specific_adapter(websocket, action_event)
