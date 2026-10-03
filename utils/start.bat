@echo off
REM 校园网自连 - Windows 启动脚本
REM 后端端口 2268（2001-3000），前端由后端同端口托管

setlocal
cd /d "%~dp0.."

set PYTHONIOENCODING=utf-8
set PYTHONPATH=%CD%\project\backend

if not exist "%CD%\database\campusnet.db" (
  echo [初始化] 首次运行，创建数据库...
)

python -m campusnet serve --port 2268
if errorlevel 1 (
  echo.
  echo 启动失败。请确认已安装 Python 3.10+：
  echo   python -m pip install -r utils\requirements.txt
  echo   python -m pip install playwright ^&^& python -m playwright install chromium
)
endlocal