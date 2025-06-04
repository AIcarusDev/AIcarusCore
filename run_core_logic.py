# run_core_logic.py
import asyncio
import traceback

# 导入新的主启动函数
from src.main import start_core_system #

if __name__ == "__main__":
    print("AIcarus Core 正在通过 run_core_logic.py 启动...") #
    try:
        asyncio.run(start_core_system()) #
    except KeyboardInterrupt:
        print("\nAIcarus Core (run_core_logic.py): 收到 KeyboardInterrupt，程序正在退出...") #
    except Exception as e:
        print(f"AIcarus Core (run_core_logic.py): 发生未处理的严重错误: {e}") #
        traceback.print_exc() #
    finally:
        print("AIcarus Core (run_core_logic.py): 程序执行完毕。") #