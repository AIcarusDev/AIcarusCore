# 修改：AIcarusCore/run_core_logic.py

import asyncio

# 现在直接从 src 包导入 main 模块
from src import main as core_main_module 

if __name__ == "__main__":
    # main_run_logger = get_logger("AIcarusCore.Run") # 如果有需要，可以保留这个logger，但现在main.py已经有自己的logger了
    # main_run_logger.info("AIcarus Core 正在通过 run_core_logic.py 启动...")

    try:
        asyncio.run(core_main_module.start_consciousness_flow())
    except KeyboardInterrupt:
        print("\nAIcarus Core (run_core_logic.py): 收到 KeyboardInterrupt，程序正在退出...")
    except Exception as e:
        print(f"AIcarus Core (run_core_logic.py): 发生未处理的严重错误: {e}")
        import traceback

        traceback.print_exc()
    finally:
        print("AIcarus Core (run_core_logic.py): 程序执行完毕。")
