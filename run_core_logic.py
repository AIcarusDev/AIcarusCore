# run_core_logic.py
import asyncio

from src.core_logic import main as core_main_module

if __name__ == "__main__":
    # 初始化日志记录器 (可选，如果你的 get_logger 首次调用时会自动配置)
    # main_run_logger = get_logger("AIcarusCore.Run")
    # main_run_logger.info("AIcarus Core 正在通过 run_core_logic.py 启动...")

    # 确保 .env 文件和 config.toml 文件位于正确的位置
    # config_manager 会在 PROJECT_ROOT (基于它自己的位置) 查找它们
    # 如果 run_core_logic.py 的位置与 config_manager.py 计算 PROJECT_ROOT 的方式不一致，
    # 可能需要调整 config_manager.py 中的 PROJECT_ROOT 定义，或确保运行环境正确。

    try:
        asyncio.run(core_main_module.start_consciousness_flow())
    except KeyboardInterrupt:
        # logger 在 asyncio.run 内部的 finally 中处理关闭信息
        print("\nAIcarus Core (run_core_logic.py): 收到 KeyboardInterrupt，程序正在退出...")
    except Exception as e:
        # 对于顶层未捕获的异常，打印出来帮助调试
        print(f"AIcarus Core (run_core_logic.py): 发生未处理的严重错误: {e}")
        import traceback

        traceback.print_exc()
    finally:
        print("AIcarus Core (run_core_logic.py): 程序执行完毕。")
