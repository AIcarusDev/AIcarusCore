@echo off
echo 正在启动 AIcarusCore...

REM 切换到批处理文件所在的目录 (即 AIcarusCore 根目录)
cd /d "%~dp0"

REM 检查虚拟环境是否存在并尝试激活
IF EXIST "venv\Scripts\activate.bat" (
    echo 正在激活虚拟环境...
    call "venv\Scripts\activate.bat"
) ELSE (
    echo 警告：未找到 venv\Scripts\activate.bat，将尝试使用系统 Python。
)

REM 运行 Python 主程序脚本
echo 正在运行主程序脚本 (run_core_logic.py)...
python run_core_logic.py

echo.
echo AIcarusCore 程序已结束。
IF EXIST "venv\Scripts\deactivate.bat" (
    echo 正在停用虚拟环境...
    call "venv\Scripts\deactivate.bat"
)
echo 按任意键关闭此窗口...
pause