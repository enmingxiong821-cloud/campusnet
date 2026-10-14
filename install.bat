@echo off
REM CampusNet setup - 本文件必须存成 GBK(cp936) + CRLF，别用 LF、也别转成 UTF-8：
REM   cmd.exe 是按字节解析批处理文件的，UTF-8 的中文在这里会被当成 GBK 双字节
REM   序列，多出来的那个字节会吃掉行尾的换行，整段 for/if 块随之错乱；
REM   只写 LF 不写 CR，同样会让换行被吃掉。
setlocal
cd /d "%~dp0"

REM 与 install.ps1 保持同一套探测顺序：py 启动器 -> python -> python3，都要求 3.10+。
REM 只 where python 会漏掉「装了 Python 但没勾 Add to PATH」的机器，那种机器上
REM py 启动器往往是有的。
set "PYCMD="
for %%C in (py python python3) do (
  if not defined PYCMD (
    %%~C -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
    if not errorlevel 1 set "PYCMD=%%~C"
  )
)
if not defined PYCMD (
  echo [错误] 没找到 Python 3.10 或更高版本。
  echo        安装地址：https://www.python.org/downloads/
  echo        安装时记得勾选 "Add python.exe to PATH"；装好后重开一个窗口再双击本文件。
  echo        已有的 Python 若版本低于 3.10，请升级后重试。
  pause
  exit /b 1
)

%PYCMD% utils\run.py setup %*
echo.
pause
