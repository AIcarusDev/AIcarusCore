import asyncio
import os

from src.core_logic import main as core_main_module  # 假设 main.py 在 src/core_logic/ 下

project_root = os.path.abspath(os.path.dirname(__file__))
src_dir_path = os.path.join(project_root, "src")  # 指向 src 目录

if __name__ == "__main__":
    asyncio.run(core_main_module.start_consciousness_flow())
