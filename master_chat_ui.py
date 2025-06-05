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

# --- Streamlit 会话状态管理 ---
def init_session_state():
    if "messages" not in st.session_state:
        st.session_state.messages = []
    if "websocket_client" not in st.session_state:
        st.session_state.websocket_client = get_websocket_client(WEBSOCKET_URI)

# --- WebSocket 客户端 (重构版) ---
class WebSocketClient:
    def __init__(self, uri):
        self._uri = uri
        self._connection = None
        self._listener_task = None
        self._lock = asyncio.Lock()
        # 创建一个独立的事件循环，给后台线程用，这样就不会和Streamlit冲突了
        self._loop = asyncio.new_event_loop()
        # 启动后台线程
        self._thread = threading.Thread(target=self._run_event_loop, daemon=True)
        self._thread.start()

    def _run_event_loop(self):
        """后台线程的目标函数，专门跑我们自己的事件循环。"""
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def is_connected(self):
        return self._connection is not None and not self._connection.closed

    def connect(self):
        """同步方法，从UI线程调用，用于触发后台连接。"""
        if self.is_connected():
            return True
        # 使用 call_soon_threadsafe 把异步的 _connect 任务提交到后台循环里
        future = asyncio.run_coroutine_threadsafe(self._connect(), self._loop)
        try:
            return future.result(timeout=10) # 等待连接结果，最多10秒
        except Exception as e:
            st.error(f"连接超时或失败: {e}")
            return False

    async def _connect(self):
        """真正的异步连接逻辑。"""
        async with self._lock:
            try:
                self._connection = await websockets.connect(self._uri)
                # 连接成功后，启动监听任务
                self._listener_task = self._loop.create_task(self._listen())
                st.toast("成功连接到AI核心！", icon="🎉")
                return True
            except Exception as e:
                st.error(f"连接AI核心失败: {e}")
                self._connection = None
                return False

    def send(self, message: str):
        """同步方法，从UI线程调用，用于发送消息。"""
        if not self.is_connected():
            st.error("未连接，无法发送消息。")
            return
        asyncio.run_coroutine_threadsafe(self._connection.send(message), self._loop)

    async def _listen(self):
        """持续监听消息，直到连接关闭。"""
        st.toast("启动后台消息监听器...", icon="📡")
        while self.is_connected():
            try:
                message_str = await self._connection.recv()
                message_data = json.loads(message_str)
                
                if message_data.get("event_type") == BOT_EVENT_TYPE:
                    text_content = ""
                    if message_data.get('content') and isinstance(message_data['content'], list):
                        text_parts = [
                            seg.get('data', {}).get('text', '')
                            for seg in message_data['content']
                            if seg.get('type') == 'text'
                        ]
                        text_content = "".join(text_parts)
                    
                    if text_content:
                        st.session_state.messages.append({"role": "assistant", "content": text_content})
                        # 在后台线程里，我们不能直接操作UI，但st.rerun是线程安全的
                        st.rerun()
            except ConnectionClosed:
                st.warning("与AI核心的连接已断开。")
                break
            except Exception as e:
                logging.error(f"监听后台消息时出错: {e}")
                await asyncio.sleep(1)

# 使用 Streamlit 缓存来保存客户端实例
@st.cache_resource
def get_websocket_client(uri):
    return WebSocketClient(uri)

# --- 主应用 ---
def main():
    st.set_page_config(layout="centered", page_title="和AI主思维聊天")
    init_session_state()

    client = st.session_state.websocket_client

    st.title("和 AI 主思维聊天")

    if not client.is_connected():
        if st.button("🔗 连接到 AI 核心"):
            if client.connect():
                st.rerun()
    else:
        st.success("已连接到 AI 核心。")

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
        
        json_message_to_send = json.dumps(user_event.to_dict(), ensure_ascii=False)
        client.send(json_message_to_send)
        st.toast("消息已发送，AI正在思考...", icon="🧠")
        # 直接刷新UI显示用户发送的消息
        st.rerun()

if __name__ == "__main__":
    main()