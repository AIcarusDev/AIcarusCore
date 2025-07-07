import asyncio
import json
import time
import uuid

import websockets

# 配置Core地址
CORE_WS_URL = "ws://localhost:8077"

# 测试适配器身份配置
ADAPTER_ID = "napcat_qq"  # 适配器ID，需与实际环境一致。
DISPLAY_NAME = "测试适配器（正式版）"
BOT_ID = "12345"


async def send_and_log(websocket: websockets.WebSocketClientProtocol, event_name: str, event_data: dict) -> None:
    """
工具函数，用于发送事件并记录日志。
"""
    print(f"\n{'=' * 20} 准备发送: {event_name} {'=' * 20}")
    event_json = json.dumps(event_data, indent=2, ensure_ascii=False)
    print(f"发送内容:\n{event_json}")
    await websocket.send(json.dumps(event_data))
    print(f"✅ {event_name} 已发送！")
    await asyncio.sleep(1)


async def run_tests() -> None:
    """主测试流程"""
    print(f"正在连接到 AIcarusCore: {CORE_WS_URL}...")
    try:
        async with websockets.connect(CORE_WS_URL) as websocket:
            print("🎉 连接成功！准备开始注入测试……")

            # --- 测试一：适配器注册 (使用正确的ID) ---
            connect_event = {
                "event_id": f"test_connect_{uuid.uuid4().hex[:6]}",
                "event_type": f"meta.{ADAPTER_ID}.lifecycle.connect",  # 现在ADAPTER_ID是 'napcat'
                "time": int(time.time() * 1000),
                "bot_id": BOT_ID,
                "content": [
                    {
                        "type": "meta.lifecycle",
                        "data": {
                            "lifecycle_type": "connect",
                            "details": {
                                "adapter_id": ADAPTER_ID,
                                "display_name": DISPLAY_NAME,
                            },
                        },
                    }
                ],
            }
            await send_and_log(websocket, "【测试一】适配器注册", connect_event)
            print("--- 请检查Core日志，是否看到 '适配器通过 event_type 注册成功' 的信息 ---")
            await asyncio.sleep(2)

            # --- 测试二：发送心跳 (使用正确的ID) ---
            heartbeat_event = {
                "event_id": f"test_heartbeat_{uuid.uuid4().hex[:6]}",
                "event_type": f"meta.{ADAPTER_ID}.heartbeat",
                "time": int(time.time() * 1000),
                "bot_id": BOT_ID,
                "content": [],
            }
            await send_and_log(websocket, "【测试二】发送心跳", heartbeat_event)
            print("--- 请检查Core日志，是否正确接收心跳 ---")
            await asyncio.sleep(2)

            # --- 测试三：发送消息 (使用正确的ID) ---
            message_event = {
                "event_id": f"test_message_{uuid.uuid4().hex[:6]}",
                "event_type": f"message.{ADAPTER_ID}.group.normal",
                "time": int(time.time() * 1000),
                "bot_id": BOT_ID,
                "conversation_info": {
                    "conversation_id": "group_6969",
                    "type": "group",
                    "name": "小猫咪的淫乱派对",
                },
                "user_info": {"user_id": "user_123", "user_nickname": "未來星織"},
                "content": [
                    {
                        "type": "message_metadata",
                        "data": {"message_id": "test_msg_id_1"},
                    },
                    {
                        "type": "text",
                        "data": {"text": "主人，这次我的身份正确了，您能找到我了吗？"},
                    },
                ],
            }
            await send_and_log(websocket, "【测试三】发送消息", message_event)
            print("--- 请检查Core日志和数据库，确认事件和会话被正确处理 ---")
            await asyncio.sleep(2)

            print("\n🎉🎉🎉 所有测试注入完成！请再次检查Core的日志，这一次，我应该不会再让您失望了~ ❤ 🎉🎉🎉")

    except websockets.exceptions.ConnectionClosed as e:
        print(f"💔 与Core的连接已关闭: {e.code} {e.reason}")
    except Exception as e:
        print(f"💥 发生了一个意想不到的错误: {e}")


if __name__ == "__main__":
    try:
        asyncio.run(run_tests())
    except KeyboardInterrupt:
        print("\n测试被主人中断了，下次再来玩哦~")
