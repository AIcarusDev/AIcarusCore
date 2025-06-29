# run_core_logic.py
import asyncio
import os  # <-- 加上这个
import traceback

# 导入新的主启动函数
from src.main import start_core_system

if __name__ == "__main__":
    print("AIcarus Core 正在通过 run_core_logic.py 启动...")
    try:
        # --- 小懒猫的调试魔法，加在这里！ ---
        # 只有当环境变量 DEBUG_ASYNCIO 设置为 "true" 时才开启
        if os.environ.get("DEBUG_ASYNCIO", "false").lower() == "true":
            print("--- 启动 asyncio 调试模式，慢速回调检测已开启！ ---")
            loop = asyncio.get_event_loop()
            # slow_callback_duration 设置为 0.5 秒，任何阻塞超过这个时间的同步调用都会被揪出来！
            loop.set_debug(True)
            loop.slow_callback_duration = 0.5
        # --- 魔法结束 ---

        asyncio.run(start_core_system())
    except KeyboardInterrupt:
        print("\nAIcarus Core (run_core_logic.py): 收到 KeyboardInterrupt，程序正在退出...")
    except Exception as e:
        print(f"AIcarus Core (run_core_logic.py): 发生未处理的严重错误: {e}")
        traceback.print_exc()
    finally:
        print("AIcarus Core (run_core_logic.py): 程序执行完毕。")
