@echo off
chcp 65001 >nul
cd /d "%~dp0"
where python >nul 2>&1
if errorlevel 1 (
    echo [错误] 未找到 Python，请先安装 Python 3.11+ 并加入 PATH
    echo        或使用完整路径运行: "C:\Program Files\Python311\python.exe" main.py
    pause
    exit /b 1
)
python main.py
if errorlevel 1 pause
