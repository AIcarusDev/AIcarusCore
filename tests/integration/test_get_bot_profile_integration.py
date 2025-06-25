# 哼，这是给你写的集成测试，别再管它叫单元测试了！
import asyncio
import json
import os
import sys
import unittest
import uuid
from contextlib import suppress
from unittest.mock import AsyncMock, MagicMock, patch
import websockets

# 又是这个烦人的路径问题，不加这个就找不到模块
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from src.action.action_handler import ActionHandler
from src.core_communication.action_sender import ActionSender
from src.database.services.action_log_storage_service import ActionLogStorageService
from src.database.services.event_storage_service import EventStorageService
from src.database.services.thought_storage_service import ThoughtStorageService
from src.tools.platform_actions import get_bot_profile

# --- 创建一些假的依赖，因为我们只想测试这一个流程 ---
# 我们不需要真的数据库，所以用 MagicMock 假装一下
mock_thought_service = MagicMock(spec=ThoughtStorageService)
mock_event_service = MagicMock(spec=EventStorageService)
mock_action_log_service = MagicMock(spec=ActionLogStorageService)


class TestGetBotProfileIntegration(unittest.IsolatedAsyncioTestCase):
    """
    这是一个集成测试，用于验证从 Core -> Adapter -> Napcat 获取机器人信息的完整流程。
    运行此测试前，请确保：
    1. Napcat 客户端已启动并登录。
    2. AIcarus-Napcat-adapter 已启动，并配置为连接到本测试脚本启动的 WebSocket 服务器 (默认 ws://localhost:8099)。
       你需要临时修改 Napcat 适配器的 config.toml，把 core_connection_url 改成 ws://127.0.0.1:8099。
    """

    def setUp(self):
        # 准备工作，烦死了...
        self.action_sender = ActionSender()
        self.action_handler = ActionHandler()
        # 把假的数据库服务塞进去，这样 ActionHandler 就不会抱怨了
        self.action_handler.set_dependencies(
            thought_service=mock_thought_service,
            event_service=mock_event_service,
            action_log_service=mock_action_log_service,
            action_sender=self.action_sender,
        )
        
        # 我们需要一个事件来同步，告诉主测试线程“适配器已经连上啦！”
        self.adapter_ready_event = asyncio.Event()
        self.test_server_port = 8077 # 用一个不常用的端口，免得和真Core冲突
        self.adapter_id_from_test: str | None = None

    async def test_get_real_bot_profile(self):
        """
        核心测试方法：启动服务器，等待适配器连接，发送请求，验证响应。
        """
        server_task = None
        try:
            # --- 步骤1: 启动我们假扮的 Core WebSocket 服务器 ---
            server = await websockets.serve(
                self.adapter_connection_handler, "localhost", self.test_server_port
            )
            server_task = asyncio.create_task(asyncio.sleep(60)) # 给测试留出足够时间
            print(f"--- 假装是Core的服务器已在 ws://localhost:{self.test_server_port} 启动 ---")
            print("--- 请现在启动你的 Napcat 适配器，并确保它连接到这个地址 ---")

            # --- 步骤2: 等待适配器连接并注册成功 ---
            try:
                await asyncio.wait_for(self.adapter_ready_event.wait(), timeout=30.0)
                print("--- 适配器已成功连接并注册！ ---")
            except asyncio.TimeoutError:
                self.fail("适配器连接超时！请检查适配器配置和运行状态。")

            # --- 步骤3: 开始折磨！循环并并发地请求 ---
            test_rounds = 3  # 我们折磨它3轮
            concurrent_requests_per_round = 2 # 每轮同时发2个请求

            for i in range(test_rounds):
                print(f"\n--- 【测试第 {i + 1}/{test_rounds} 轮开始】 ---")
                
                tasks = []
                for j in range(concurrent_requests_per_round):
                    # 同时获取带群号和不带群号的机器人信息，增加测试覆盖度
                    group_id_for_test = "1041305886" if j % 2 == 0 else None # 你的测试群号，或者随便写一个
                    print(f"  > 准备第 {j + 1} 个并发请求 (group_id: {group_id_for_test})")
                    task = asyncio.create_task(
                        get_bot_profile(
                            action_handler=self.action_handler,
                            adapter_id=self.adapter_id_from_test,
                            group_id=group_id_for_test,
                        )
                    )
                    tasks.append(task)
                
                # 使用 asyncio.gather 来同时运行这些任务，然后等待所有结果
                print(f"  > 并发发送 {len(tasks)} 个请求...")
                results = await asyncio.gather(*tasks)
                print(f"  > 本轮所有请求已收到响应！")

                # --- 步骤4: 验证本轮的所有结果 ---
                for k, result in enumerate(results):
                    print(f"  > 验证第 {k + 1} 个响应: {result}")
                    self.assertIsNotNone(result, f"第 {i+1} 轮第 {k+1} 个请求的结果不应为 None")
                    self.assertIsInstance(result, dict, f"第 {i+1} 轮第 {k+1} 个请求的结果应为字典")
                    self.assertIn("user_id", result)
                    self.assertIn("nickname", result)

                print(f"--- 【测试第 {i + 1}/{test_rounds} 轮通过】 ---\n")
                if i < test_rounds - 1:
                    print(f"--- 等待 2 秒，模拟思考间隔... ---")
                    await asyncio.sleep(2)

            print("--- 所有测试轮次成功！你的适配器抗住了本猫的折磨！ ---")

        finally:
            # --- 清理工作，把服务器关掉 ---
            if server_task:
                server_task.cancel()
            if 'server' in locals() and server.is_serving():
                server.close()
                await server.wait_closed()
                print("--- 测试服务器已关闭 ---")

    async def adapter_connection_handler(self, websocket: websockets.WebSocketServerProtocol, path: str):
        """
        这个函数用来处理适配器的连接，它会假装自己是 Core 的连接处理器。
        """
        print(f"--- 收到一个来自 {websocket.remote_address} 的连接请求... ---")
        
        # 1. 等待并处理适配器的注册消息
        try:
            # 适配器连接后，会立刻发送一个 meta.lifecycle.connect 事件用于注册
            reg_msg_str = await asyncio.wait_for(websocket.recv(), timeout=10.0)
            reg_msg = json.loads(reg_msg_str)
            
            # 从注册消息里把 adapter_id 扒出来
            self.adapter_id_from_test = reg_msg.get("content", [{}])[0].get("data", {}).get("details", {}).get("adapter_id")
            display_name = reg_msg.get("content", [{}])[0].get("data", {}).get("details", {}).get("display_name", self.adapter_id_from_test)

            if not self.adapter_id_from_test:
                print("--- 错误：适配器发送的注册消息格式不正确，没找到 adapter_id ---")
                await websocket.close()
                return

            # 把这个连接注册到我们的 ActionSender 里，这样才能给它发消息
            self.action_sender.register_adapter(self.adapter_id_from_test, display_name, websocket)
            
            # 通知主测试线程：准备就绪，可以开始了！
            self.adapter_ready_event.set()

        except Exception as e:
            print(f"--- 处理适配器注册时出错: {e} ---")
            self.adapter_ready_event.set() # 也设置事件，让主测试线程知道出错了并失败
            await websocket.close()
            return

        # 2. 持续监听来自适配器的消息（主要是动作的响应）
        try:
            async for message in websocket:
                message_dict = json.loads(message)
                # 如果是动作响应，就交给 action_handler 处理
                if message_dict.get("event_type", "").startswith("action_response."):
                    print(f"--- 收到动作响应: {message_dict.get('event_type')} ---")
                    await self.action_handler.handle_action_response(message_dict)
                else:
                    # 其他消息暂时不管
                    print(f"--- 收到普通消息（已忽略）: {message_dict.get('event_type')} ---")
        except websockets.exceptions.ConnectionClosed:
            print("--- 适配器连接已断开 ---")
        finally:
            self.action_sender.unregister_adapter(websocket)


if __name__ == "__main__":
    # 这样你就可以直接运行这个文件来测试了
    unittest.main()