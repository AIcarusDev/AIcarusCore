# run_core_logic.py
import asyncio
import os
import traceback

# 导入主启动函数
from src.main import start_core_system

if __name__ == "__main__":
    print("AIcarus Core 正在通过 run_core_logic.py 启动...")
    try:
        # 调试模式配置：仅当环境变量 DEBUG_ASYNCIO 设置为 "true" 时启用 asyncio 调试模式
        if os.environ.get("DEBUG_ASYNCIO", "false").lower() == "true":
            print("启用 asyncio 调试模式，将检测耗时超过 0.5 秒的同步回调！")
            loop = asyncio.get_event_loop()
            # slow_callback_duration 设置为 0.5 秒，用于检测阻塞时间超过 0.5 秒的同步调用
            loop.set_debug(True)
            loop.slow_callback_duration = 0.5
        # 调试模式配置结束

        asyncio.run(start_core_system())
    except KeyboardInterrupt:
        print("\nAIcarus Core (run_core_logic.py): 收到 KeyboardInterrupt 信号，程序正在退出...")
    except Exception as e:
        print(f"AIcarus Core (run_core_logic.py): 发生未处理的严重错误: {e}")
        traceback.print_exc()
    finally:
        print("AIcarus Core (run_core_logic.py): 程序执行完毕。")
