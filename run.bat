@echo off
echo �������� AIcarusCore...

REM �л����������ļ����ڵ�Ŀ¼ (�� AIcarusCore ��Ŀ¼)
cd /d "%~dp0"

REM ������⻷���Ƿ���ڲ����Լ���
IF EXIST "venv\Scripts\activate.bat" (
    echo ���ڼ������⻷��...
    call "venv\Scripts\activate.bat"
) ELSE (
    echo ���棺δ�ҵ� venv\Scripts\activate.bat��������ʹ��ϵͳ Python��
)

REM ���� Python ������ű�
echo ��������������ű� (run_core_logic.py)...
python run_core_logic.py

echo.
echo AIcarusCore �����ѽ�����
IF EXIST "venv\Scripts\deactivate.bat" (
    echo ����ͣ�����⻷��...
    call "venv\Scripts\deactivate.bat"
)
echo ��������رմ˴���...
pause