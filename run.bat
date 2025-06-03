@echo off
echo 正在启动 AIcarusCore...

REM 切换到批处理文件所在的目录 (即 AIcarusCore 根目录)
cd /d "%~dp0"

REM 设置 PYTHONPATH 环境变量，确保 Python 能够找到项目模块
REM %~dp0 是当前批处理文件所在目录的绝对路径 (例如 D:\Aic\AIcarusCore\)
REM 我们需要将 D:\Aic 和 D:\Aic\AIcarusCore 都添加到 PYTHONPATH
REM 首先，获取 AIcarusCore 的父目录路径 (例如 D:\Aic)
for %%i in ("%~dp0\..") do set "PROJECT_PARENT_DIR=%%~fi"

set "PYTHONPATH=%PROJECT_PARENT_DIR%;%~dp0;%PYTHONPATH%"
echo 设置 PYTHONPATH: %PYTHONPATH%

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

REM 清除本次运行设置的 PYTHONPATH，避免影响后续其他操作 (可选，但推荐)
set PYTHONPATH=

echo 按任意键关闭此窗口...
pause