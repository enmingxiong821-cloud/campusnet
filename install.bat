@echo off
REM 校园网自连 - 一键配置（第一次使用双击这个）
chcp 65001 >nul
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo [错误] 没找到 python 命令。
  echo        请先安装 Python 3.10 或更高版本：https://www.python.org/downloads/
  echo        安装时记得勾选 "Add python.exe to PATH"。
  pause
  exit /b 1
)

python utils\run.py setup %*
echo.
pause
