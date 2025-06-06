# master_chat_ui.py
# 哼，这是最终无敌修复版！再出问题我就……我就再帮你看看。

import asyncio
import json
import logging
import streamlit as st
import threading
import time
import uuid
import websockets
from websockets.exceptions import ConnectionClosed

from aicarus_protocols import Event as ProtocolEvent, SegBuilder, ConversationInfo, ConversationType

# --- 全局常量 ---
MASTER_CONVERSATION_ID = "master_chat"
USER_EVENT_TYPE = "message.master.input"
BOT_EVENT_TYPE = "message.master.output"
WEBSOCKET_URI = "ws://localhost:8077" # 确保端口正确

# 使用项目自己的 logger
from src.common.custom_logging.logger_manager import get_logger
logger = get_logger("UI_Master_Chat_Client") # 给UI客户端也搞个日志，方便调试

# --- Streamlit 会话状态管理 ---
def init_session_state():
    # 确保在会话开始时就彻底初始化
    if "messages" not in st.session_state:
        st.session_state.messages = []
        logger.info("st.session_state.messages 已初始化。")
    if "received_messages_queue" not in st.session_state: # 队列必须先初始化
        st.session_state.received_messages_queue = asyncio.Queue()
        logger.info("received_messages_queue 已初始化。")
    if "websocket_client" not in st.session_state:
        st.session_state.websocket_client = get_websocket_client(WEBSOCKET_URI)
        logger.info("WebSocketClient 已初始化并存储在 session_state 中。")
    if "main_execution_logged" not in st.session_state: # 初始化首次执行日志标记
        st.session_state.main_execution_logged = False
    # 确保 WebSocketClient 拿到队列的引用，只在连接时传递一次
    # 这里需要加一个判断，确保 client._streamlit_message_queue 不为 None，否则会报错
    # 之前是 client._streamlit_message_queue is None，这里改为 if client and client._streamlit_message_queue is None:
    if st.session_state.websocket_client and st.session_state.websocket_client._streamlit_message_queue is None:
        st.session_state.websocket_client.set_message_queue(st.session_state.received_messages_queue)
        logger.info("WebSocketClient 已设置消息队列。")


# --- WebSocket 客户端 (重构版) ---
class WebSocketClient:
    def __init__(self, uri):
        self._uri = uri
        self._connection = None
        self._listener_task = None
        self._lock = asyncio.Lock()
        # 创建一个独立的事件循环，给后台线程用，这样就不会和Streamlit冲突了
        self._loop = asyncio.new_event_loop()
        # 这里不再立即获取 st.session_state.received_messages_queue
        self._streamlit_message_queue = None # 先设为 None
        # 启动后台线程
        self._thread = threading.Thread(target=self._run_event_loop, daemon=True)
        self._thread.start()
        logger.info("WebSocketClient 后台线程已启动。")

    def set_message_queue(self, queue: asyncio.Queue):
        """在 Streamlit session state 初始化后，设置消息队列。"""
        self._streamlit_message_queue = queue
        logger.info("WebSocketClient 消息队列已设置。")

    def _run_event_loop(self):
        """后台线程的目标函数，专门跑我们自己的事件循环。"""
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()
        logger.info("WebSocketClient 事件循环已停止。")

    def is_connected(self):
        return self._connection is not None and not self._connection.closed

    def connect(self):
        """同步方法，从UI线程调用，用于触发后台连接。"""
        if self.is_connected():
            logger.info("WebSocket 已连接。")
            return True
        if self._streamlit_message_queue is None: # 连接前检查队列是否设置
            logger.error("WebSocketClient 的消息队列未设置，无法连接！")
            st.error("AI系统内部错误：消息队列未准备好，无法连接。")
            return False

        logger.info(f"尝试连接到 AI 核心: {self._uri}")
        # 使用 call_soon_threadsafe 把异步的 _connect 任务提交到后台循环里
        future = asyncio.run_coroutine_threadsafe(self._connect(), self._loop)
        try:
            connect_result = future.result(timeout=10) # 等待连接结果，最多10秒
            if connect_result:
                logger.info("成功连接到AI核心！")
                st.toast("成功连接到AI核心！", icon="🎉")
            return connect_result
        except Exception as e:
            logger.error(f"连接超时或失败: {e}")
            st.error(f"连接超时或失败: {e}")
            return False

    async def _connect(self):
        """真正的异步连接逻辑。"""
        async with self._lock:
            try:
                self._connection = await websockets.connect(self._uri)
                # 连接成功后，启动监听任务
                if self._listener_task is None or self._listener_task.done(): # 确保只启动一个监听任务
                    self._listener_task = self._loop.create_task(self._listen())
                logger.info("WebSocket 监听任务已启动。")
                return True
            except Exception as e:
                logger.error(f"连接AI核心失败: {e}")
                self._connection = None
                return False

    def send(self, message: str):
        """同步方法，从UI线程调用，用于发送消息。"""
        if not self.is_connected():
            logger.error("未连接，无法发送消息。")
            st.error("未连接，无法发送消息。")
            return
        logger.debug(f"通过 WebSocket 发送消息: {message[:100]}...")
        asyncio.run_coroutine_threadsafe(self._connection.send(message), self._loop)

    async def _listen(self):
        """持续监听消息，直到连接关闭。"""
        logger.info("启动后台消息监听器...")
        # 确保消息队列已设置
        if self._streamlit_message_queue is None:
            logger.critical("后台监听器无法运行：消息队列未设置！")
            return # 无法继续监听
        
        logger.info(f"后台监听器循环开始，连接状态: {self.is_connected()}")
        while self.is_connected():
            try:
                logger.debug(f"后台监听器: 等待 WebSocket 消息 (ID: {id(self._connection)})...")
                message_str = await self._connection.recv()
                logger.debug(f"后台监听器: 收到原始消息 (长度 {len(message_str)}): {message_str[:200]}...") # 增加原始消息日志
                
                message_data = json.loads(message_str)
                logger.debug(f"后台监听器: JSON 解析后数据: {message_data}") # 打印解析后的数据

                if message_data.get("event_type") == BOT_EVENT_TYPE:
                    logger.debug(f"后台监听器: 识别到 BOT_EVENT_TYPE ({BOT_EVENT_TYPE})")
                    text_content = ""
                    if message_data.get('content') and isinstance(message_data['content'], list):
                        text_parts = [
                            seg.get('data', {}).get('text', '')
                            for seg in message_data['content']
                            if seg.get('type') == 'text'
                        ]
                        text_content = "".join(text_parts)
                    
                    if text_content:
                        logger.info(f"后台监听器: 解析到机器人回复文本: '{text_content[:50]}...'")
                        # 将消息放入队列
                        queue_item = {"role": "assistant", "content": text_content}
                        logger.debug(f"后台监听器: 准备将消息放入队列: {queue_item}")
                        await self._streamlit_message_queue.put(queue_item)
                        logger.debug(f"后台监听器: 消息已成功放入队列, 当前队列大小: {self._streamlit_message_queue.qsize()}")
                    else:
                        logger.warning(f"后台监听器: 收到 BOT_EVENT_TYPE 但未解析出有效 text_content。原始数据: {message_data}")
                else:
                    logger.debug(f"后台监听器: 收到非 BOT_EVENT_TYPE 消息: {message_data.get('event_type')}")

            except ConnectionClosed:
                logger.warning("后台监听器: 与AI核心的连接已断开 (ConnectionClosed)。")
                break
            except json.JSONDecodeError as e:
                logger.error(f"后台监听器: JSON 解析 WebSocket 消息失败: {e}. 原始消息: {message_str[:200]}...", exc_info=True)
                # 发生JSON解析错误时，可以选择继续监听下一条，或者也break，取决于业务需求
                # 这里选择继续监听
            except Exception as e:
                logger.error(f"监听后台消息时出错: {e}", exc_info=True)
                await asyncio.sleep(1) # 短暂等待，避免无限循环报错
        logger.info("后台消息监听器已停止。")

    def close(self):
        """同步方法，用于关闭 WebSocket 连接和停止事件循环。"""
        if self.is_connected():
            logger.info("正在关闭 WebSocket 连接...")
            asyncio.run_coroutine_threadsafe(self._connection.close(), self._loop)
        if self._loop.is_running():
            logger.info("正在停止后台事件循环...")
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread.is_alive():
            self._thread.join(timeout=5) # 等待线程结束
            if self._thread.is_alive():
                logger.warning("后台线程未能及时停止。")
        logger.info("WebSocketClient 已关闭。")


# 使用 Streamlit 缓存来保存客户端实例
@st.cache_resource(ttl=None) # 设置ttl为None表示永不失效，除非Streamlit应用重启
def get_websocket_client(uri):
    logger.info("正在创建新的 WebSocketClient 实例或从缓存获取。")
    client = WebSocketClient(uri)
    return client

# --- 主应用 ---
def main():
    st.set_page_config(layout="centered", page_title="和AI主思维聊天")
    init_session_state() # 确保会话状态在最前面初始化

    client = st.session_state.websocket_client
    # logger.debug(f"WebSocket client 实例: {client}") # 注释掉，减少日志刷屏

    st.title("和 AI 主思维聊天")

    # 连接按钮
    if not client.is_connected():
        if st.button("🔗 连接到 AI 核心"):
            if client.connect(): # 调用连接方法
                st.rerun() # 连接成功后立即刷新UI
    else:
        st.success("已连接到 AI 核心。")

    # 从队列中获取消息并在主线程中处理
    # 这一步是关键！Streamlit 的 UI 更新必须在主线程中完成
    
    # 延迟修复：一次性从队列取出所有消息，避免多次rerun
    new_messages_received = False
    # logger.debug(f"主线程: 开始检查消息队列 (received_messages_queue), 当前大小: {st.session_state.received_messages_queue.qsize()}") # 注释掉，减少日志刷屏
    while not st.session_state.received_messages_queue.empty():
        try:
            logger.debug("主线程: 尝试从队列 get_nowait()")
            message = st.session_state.received_messages_queue.get_nowait()
            logger.info(f"主线程: 从队列成功获取消息: {message}")
            st.session_state.messages.append(message)
            logger.debug(f"主线程: 消息已追加到 st.session_state.messages, 当前共 {len(st.session_state.messages)} 条消息。")
            new_messages_received = True # 标记有新消息
        except asyncio.QueueEmpty:
            logger.debug("主线程: 消息队列已空 (QueueEmpty)。")
            break
        except Exception as e:
            logger.error(f"主线程: 从消息队列处理消息时出错: {e}", exc_info=True)

    if new_messages_received: # 只有当真正有新消息被添加到 session_state 时，才触发一次rerun
        logger.info("主线程: 检测到新接收的消息，准备调用 st.rerun() 刷新UI。")
        st.rerun()
    else:
        pass # logger.debug("主线程: 本次检查未从队列获取新消息，不调用 st.rerun()。") # 注释掉，减少日志刷屏


    for message in st.session_state.messages:
        with st.chat_message(message["role"], avatar="🧑‍💻" if message["role"] == "user" else "🤖"):
            st.markdown(message["content"])

    if prompt := st.chat_input("对AI说点什么...", disabled=not client.is_connected()):
        st.session_state.messages.append({"role": "user", "content": prompt})
        
        user_event = ProtocolEvent(
            event_id=str(uuid.uuid4()),
            event_type=USER_EVENT_TYPE,
            time=int(time.time() * 1000),
            platform="master_ui",
            bot_id="master_bot", 
            conversation_info=ConversationInfo(conversation_id=MASTER_CONVERSATION_ID, type=ConversationType.PRIVATE),
            content=[SegBuilder.text(prompt)]
        )
        logger.debug(f"主线程: 用户发送新消息，构造的 ProtocolEvent: {user_event.to_dict()}")
        
        json_message_to_send = json.dumps(user_event.to_dict(), ensure_ascii=False)
        logger.debug(f"主线程: 准备通过 WebSocket 发送 JSON: {json_message_to_send[:150]}...")
        client.send(json_message_to_send)
        logger.info("主线程: 用户消息已通过 WebSocket 发送。")
        st.toast("消息已发送，AI正在思考...", icon="🧠")
        # 用户发送消息后，立即刷新UI显示用户发送的消息
        logger.info("主线程: 用户发送消息后，准备调用 st.rerun() 刷新UI。")
        st.rerun()

    # 强制刷新逻辑，确保主线程持续检查队列
    time.sleep(0.5) # 短暂休眠，调整刷新频率以减少CPU占用和日志量
    st.rerun()

if __name__ == "__main__":
    if not st.session_state.get("main_execution_logged", False):
        logger.info("--- master_chat_ui.py 脚本首次执行 (if __name__ == '__main__') ---")
        st.session_state.main_execution_logged = True
    main()
