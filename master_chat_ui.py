# master_chat_ui.py
# 哼，这是最终无敌修复版！再出问题我就……我就再帮你看看。

import asyncio
from collections import deque # <--- 小懒猫加的，为了日志队列
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
CORE_LOG_EVENT_TYPE = "core.log.output" # <--- 小懒猫加的，为了接收AI核心的日志！
DEFAULT_OWNER_NAME = "主人"
DEFAULT_CONTEXT_WINDOW_SIZE = 20
DEFAULT_WEBSOCKET_URI = "ws://localhost:8077" # 默认值还是得有一个的

# 使用项目自己的 logger
from src.common.custom_logging.logger_manager import get_logger

# --- UI Logger Setup ---
if "ui_log_list" not in st.session_state:
    st.session_state.ui_log_list = deque(maxlen=200)

logger = get_logger("UI_Master_Chat_Client") 

def streamlit_log_sink(message):
    log_entry = message.strip() 
    if hasattr(st, 'session_state') and "ui_log_list" in st.session_state:
        st.session_state.ui_log_list.append(log_entry)

def activate_ui_log_sink():
    if "ui_log_sink_added" not in st.session_state:
        try:
            logger.add(
                streamlit_log_sink,
                format="{time:YYYY-MM-DD HH:mm:ss} - {level.name:<8} - {message}",
                level="DEBUG",
                enqueue=True 
            )
            st.session_state.ui_log_sink_added = True
        except Exception as e:
            st.error(f"关键错误：无法初始化UI日志捕获器: {e}")
            print(f"控制台错误：添加UI日志捕获器到Loguru失败: {e}")

# --- Streamlit 会话状态管理 ---
def init_session_state():
    # 确保这些列表在任何可能访问它们之前都已初始化
    if "messages" not in st.session_state:
        st.session_state.messages = []
    if "core_log_display_list" not in st.session_state:
        st.session_state.core_log_display_list = deque(maxlen=200)
    if "ui_log_list" not in st.session_state: # 再次确保
        st.session_state.ui_log_list = deque(maxlen=200)

    if "owner_name" not in st.session_state:
        st.session_state.owner_name = DEFAULT_OWNER_NAME
    if "context_window_size" not in st.session_state:
        st.session_state.context_window_size = DEFAULT_CONTEXT_WINDOW_SIZE
    if "websocket_uri" not in st.session_state:
        st.session_state.websocket_uri = DEFAULT_WEBSOCKET_URI
    
    if "core_log_queue" not in st.session_state:
        st.session_state.core_log_queue = asyncio.Queue()
    
    if not st.session_state.get("messages_initialized_log", False): # 使用新的标志位
        logger.info("st.session_state.messages 已初始化 (或确认已存在)。")
        st.session_state.messages_initialized_log = True

    if "received_messages_queue" not in st.session_state:
        st.session_state.received_messages_queue = asyncio.Queue()
        logger.info("received_messages_queue 已初始化。")
    
    if "websocket_client" not in st.session_state:
        st.session_state.websocket_client = get_websocket_client(st.session_state.websocket_uri)
        logger.info(f"WebSocketClient 已初始化 (URI: {st.session_state.websocket_uri}) 并存储在 session_state 中。")
    
    if "main_execution_logged" not in st.session_state:
        st.session_state.main_execution_logged = False
    
    ws_client_instance = st.session_state.get("websocket_client")
    msg_queue_instance = st.session_state.get("received_messages_queue")

    if ws_client_instance and msg_queue_instance and ws_client_instance._streamlit_message_queue is None:
        ws_client_instance.set_message_queue(msg_queue_instance)
        logger.info("WebSocketClient 已设置消息队列。")
    
    if "session_state_summary_logged" not in st.session_state:
        logger.info(f"Session state fully initialized for the first time. Owner: {st.session_state.get('owner_name', DEFAULT_OWNER_NAME)}, Context Size: {st.session_state.get('context_window_size', DEFAULT_CONTEXT_WINDOW_SIZE)}, WS URI: {st.session_state.get('websocket_uri', DEFAULT_WEBSOCKET_URI)}")
        st.session_state.session_state_summary_logged = True

# --- WebSocket 客户端 ---
class WebSocketClient:
    def __init__(self, uri):
        self._uri = uri
        self._connection = None
        self._listener_task = None
        self._lock = asyncio.Lock()
        self._loop = asyncio.new_event_loop()
        self._streamlit_message_queue = None
        self._thread = threading.Thread(target=self._run_event_loop, daemon=True)
        self._thread.start()
        logger.info("WebSocketClient 后台线程已启动。")

    def set_message_queue(self, queue: asyncio.Queue):
        self._streamlit_message_queue = queue
        logger.info("WebSocketClient 消息队列已设置。")

    def _run_event_loop(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()
        logger.info("WebSocketClient 事件循环已停止。")

    def is_connected(self):
        return self._connection is not None and not self._connection.closed

    def connect(self):
        if self.is_connected():
            logger.info("WebSocket 已连接。")
            return True
        if self._streamlit_message_queue is None:
            logger.error("WebSocketClient 的消息队列未设置，无法连接！")
            st.error("AI系统内部错误：消息队列未准备好，无法连接。")
            return False

        logger.info(f"尝试连接到 AI 核心: {self._uri}")
        future = asyncio.run_coroutine_threadsafe(self._connect(), self._loop)
        try:
            connect_result = future.result(timeout=10)
            if connect_result:
                logger.info("成功连接到AI核心！")
                st.toast("成功连接到AI核心！", icon="🎉")
            return connect_result
        except Exception as e:
            logger.error(f"连接超时或失败: {e}")
            st.error(f"连接超时或失败: {e}")
            return False

    async def _connect(self):
        async with self._lock:
            try:
                self._connection = await websockets.connect(self._uri)
                if self._listener_task is None or self._listener_task.done():
                    self._listener_task = self._loop.create_task(self._listen())
                logger.info("WebSocket 监听任务已启动。")
                return True
            except Exception as e:
                logger.error(f"连接AI核心失败: {e}")
                self._connection = None
                return False

    def send(self, message: str):
        if not self.is_connected():
            logger.error("未连接，无法发送消息。")
            st.error("未连接，无法发送消息。")
            return
        logger.debug(f"通过 WebSocket 发送消息: {message[:100]}...")
        asyncio.run_coroutine_threadsafe(self._connection.send(message), self._loop)

    async def _listen(self):
        logger.info("启动后台消息监听器...")
        if self._streamlit_message_queue is None:
            logger.critical("后台监听器无法运行：消息队列未设置！")
            return
        
        logger.info(f"后台监听器循环开始，连接状态: {self.is_connected()}")
        while self.is_connected():
            try:
                message_str = await self._connection.recv()
                message_data = json.loads(message_str)
                logger.debug(f"后台监听器: JSON 解析后数据: {message_data}")

                if message_data.get("event_type") == BOT_EVENT_TYPE:
                    text_content = ""
                    if message_data.get('content') and isinstance(message_data['content'], list):
                        text_parts = [
                            seg.get('data', {}).get('text', '')
                            for seg in message_data['content']
                            if seg.get('type') == 'text'
                        ]
                        text_content = "".join(text_parts)
                    
                    actual_text_to_add = text_content if text_content is not None else "" 
                    if actual_text_to_add: 
                        logger.info(f"后台监听器: 解析到机器人回复文本: '{str(actual_text_to_add)[:50]}...'")
                        queue_item = {"role": "assistant", "content": actual_text_to_add}
                        await self._streamlit_message_queue.put(queue_item)
                    else: 
                        logger.info(f"后台监听器: 机器人回复内容为空或无效，已忽略。原始数据: {message_data}")

                elif message_data.get("event_type") == CORE_LOG_EVENT_TYPE:
                    core_log_content = ""
                    payload = message_data.get('payload')
                    if isinstance(payload, dict):
                        ts = payload.get("timestamp", time.strftime("%Y-%m-%d %H:%M:%S"))
                        level = payload.get("level", "LOG")
                        msg = payload.get("message", "")
                        if msg:
                             core_log_content = f"[{ts}][{level.upper()}] {msg}"
                        else:
                            core_log_content = str(payload)
                    elif isinstance(payload, str):
                        core_log_content = payload
                    else:
                        core_log_content = f"收到未知格式核心日志: {message_data}"

                    if core_log_content:
                        if "core_log_queue" in st.session_state:
                            await st.session_state.core_log_queue.put(core_log_content)
                else:
                    logger.debug(f"后台监听器: 收到未识别的事件类型消息: {message_data.get('event_type')}")
            except ConnectionClosed:
                logger.warning("后台监听器: 与AI核心的连接已断开 (ConnectionClosed)。")
                break
            except json.JSONDecodeError as e:
                logger.error(f"后台监听器: JSON 解析 WebSocket 消息失败: {e}. 原始消息: {message_str[:200]}...", exc_info=True)
            except Exception as e:
                logger.error(f"监听后台消息时出错: {e}", exc_info=True)
                await asyncio.sleep(1)
        logger.info("后台消息监听器已停止。")

    def close(self):
        if self.is_connected():
            asyncio.run_coroutine_threadsafe(self._connection.close(), self._loop)
        if self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread.is_alive():
            self._thread.join(timeout=5)
        logger.info("WebSocketClient 已关闭。")

@st.cache_resource(ttl=None)
def get_websocket_client(uri):
    logger.info(f"正在创建新的 WebSocketClient 实例 (URI: {uri}) 或从缓存获取。")
    return WebSocketClient(uri)

# --- 主应用 ---
def main():
    if "owner_name" not in st.session_state:
         st.session_state.owner_name = DEFAULT_OWNER_NAME 
    st.set_page_config(layout="wide", page_title=f"和 {st.session_state.owner_name} 的秘密基地")
    activate_ui_log_sink()
    init_session_state() # 确保所有 session_state 变量在UI渲染前已准备好

    # --- 侧边栏配置 ---
    with st.sidebar:
        st.header("⚙️ UI 配置")
        current_owner_name = st.session_state.owner_name
        new_owner_name = st.text_input("主人称呼:", value=current_owner_name, key="owner_name_input")
        if new_owner_name != current_owner_name:
            st.session_state.owner_name = new_owner_name
            logger.info(f"主人称呼已更新为: {new_owner_name}")
            st.rerun()

        current_context_size = st.session_state.context_window_size
        new_context_window_size = st.number_input("上下文窗口大小:", min_value=1, max_value=200, value=current_context_size, step=1, key="context_window_input")
        if new_context_window_size != current_context_size:
            st.session_state.context_window_size = new_context_window_size
            logger.info(f"上下文窗口大小已更新为: {new_context_window_size}")
            st.toast(f"上下文窗口大小已设为 {new_context_window_size} (AI核心需适配)", icon="⚙️")
            st.rerun()

        current_ws_uri = st.session_state.websocket_uri
        new_websocket_uri_input = st.text_input("WebSocket URI:", value=current_ws_uri, key="websocket_uri_input")
        
        if st.button("应用新URI并重新连接", key="apply_uri_button"):
            if new_websocket_uri_input != current_ws_uri:
                if "websocket_client" in st.session_state and st.session_state.websocket_client:
                    st.session_state.websocket_client.close()
                    del st.session_state["websocket_client"]
                st.session_state.websocket_uri = new_websocket_uri_input
                # get_websocket_client 会在下次访问时创建新实例
                st.toast("WebSocket URI 已更新，下次连接将使用新地址。", icon="ℹ️")
                st.rerun() 
            else:
                st.toast("WebSocket URI 未改变。", icon="ℹ️")
        
        if st.button("强制刷新UI", key="force_refresh_ui_sidebar"):
            st.rerun()

    # --- 主界面 ---
    left_column, right_column = st.columns([2, 1]) 

    with left_column:
        client = st.session_state.websocket_client 
        st.title(f"与 {st.session_state.owner_name} 的聊天室")

        if not client.is_connected():
            if st.button("🔗 连接到 AI 核心", key="connect_button_main"):
                if client.connect(): 
                    st.rerun() 
        else:
            st.success(f"已连接到 AI 核心 ({client._uri})")

        # --- 消息处理与显示 ---
        new_messages_received = False
        if "received_messages_queue" in st.session_state:
            while not st.session_state.received_messages_queue.empty():
                try:
                    message = st.session_state.received_messages_queue.get_nowait()
                    st.session_state.messages.append(message)
                    new_messages_received = True 
                except asyncio.QueueEmpty:
                    break
        
        chat_container = st.container()
        with chat_container:
            # 确保 st.session_state.messages 总是列表
            for message_data in st.session_state.get("messages", []):
                with st.chat_message(message_data["role"], avatar="🧑‍💻" if message_data["role"] == "user" else "🤖"):
                    st.markdown(message_data["content"])
        
        prompt_text = f"对 {st.session_state.owner_name} 说点什么..."
        is_chat_disabled = not (client and client.is_connected())
        if prompt := st.chat_input(prompt_text, disabled=is_chat_disabled, key="chat_input_main"):
            st.session_state.messages.append({"role": "user", "content": prompt})
            user_event = ProtocolEvent(
                event_id=str(uuid.uuid4()), event_type=USER_EVENT_TYPE, time=int(time.time() * 1000),
                platform="master_ui", bot_id="master_bot", 
                conversation_info=ConversationInfo(conversation_id=MASTER_CONVERSATION_ID, type=ConversationType.PRIVATE),
                content=[SegBuilder.text(prompt)]
            )
            client.send(json.dumps(user_event.to_dict(), ensure_ascii=False))
            st.rerun() # 用户发送消息后，必须 rerun

    with right_column:
        st.subheader("🖥️ 系统日志区")
        
        new_core_logs_received_this_run = False
        if "core_log_queue" in st.session_state: 
            while not st.session_state.core_log_queue.empty():
                try:
                    log_item = st.session_state.core_log_queue.get_nowait()
                    st.session_state.core_log_display_list.append(log_item) 
                    new_core_logs_received_this_run = True
                except asyncio.QueueEmpty:
                    break
        
        core_log_text_to_display = "\n".join(list(st.session_state.get("core_log_display_list", [])))
        st.text_area("来自AI核心的实时日志:", value=core_log_text_to_display, height=200, disabled=True, key="core_program_log_display")
        
        log_list_to_display = list(st.session_state.get("ui_log_list", []))
        log_display_content = "\n".join(log_list_to_display) 
        st.text_area("UI内部操作日志记录:", value=log_display_content, height=350, disabled=True, key="ui_client_log_display")

    # --- 统一刷新逻辑 ---
    if new_messages_received or new_core_logs_received_this_run:
        st.rerun()
    else:
        # 最后的救命稻草：如果没有任何新消息或新日志，但为了确保UI其他部分（如日志区在没有新日志时也能滚动）
        # 或者应对一些Streamlit的怪癖，我们还是加上这个最终的刷新。
        # 但要注意，这个是UI重复bug的最大嫌疑犯。
        # 如果UI重复问题依然存在，首先考虑注释掉下面这两行。
        time.sleep(0.05) # 稍微减少一点等待时间
        st.rerun()

if __name__ == "__main__":
    if not st.session_state.get("main_execution_logged", False):
        logger.info("--- master_chat_ui.py 脚本首次执行 (if __name__ == '__main__') ---")
        st.session_state.main_execution_logged = True
    main()
