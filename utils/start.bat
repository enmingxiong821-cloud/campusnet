@echo off
REM CampusNet - Windows start script (keep this file ASCII-only, CRLF newlines).
REM Backend serves port 2268; the web console is served by the same port.
REM
REM Why ASCII: cmd.exe parses .bat byte by byte. UTF-8 Chinese bytes get read as
REM GBK pairs, the odd byte eats the newline, and the for/if blocks below break.
REM The console is switched to UTF-8 (chcp 65001) because PYTHONIOENCODING=utf-8
REM makes the service print UTF-8; a 936 console would render that as mojibake.

setlocal
cd /d "%~dp0.."

chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONPATH=%CD%\project\backend

if not exist "%CD%\database\campusnet.db" (
  echo [init] first run, creating database...
)

REM Same probe order as install.bat: py launcher -> python -> python3, all 3.10+.
set "PYCMD="
for %%C in (py python python3) do (
  if not defined PYCMD (
    %%~C -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
    if not errorlevel 1 set "PYCMD=%%~C"
  )
)
if not defined PYCMD (
  echo.
  echo [FAILED] Python 3.10+ not found.
  echo   Install it from https://www.python.org/downloads/ and tick
  echo   "Add python.exe to PATH", then reopen this window.
  pause
  exit /b 1
)

%PYCMD% -m campusnet serve --port 2268
if errorlevel 1 (
  echo.
  echo [FAILED] CampusNet did not start. Likely causes, most likely first:
  echo   1. Port 2268 is already in use - usually CampusNet is already running.
  echo      Just open http://127.0.0.1:2268 in a browser. To run a second
  echo      instance instead:
  echo        %PYCMD% utils\run.py serve --port 2288
  echo   2. Python older than 3.10 - upgrade it and retry.
  echo   3. If the error above mentions playwright or chromium, that dependency
  echo      is only needed for browser login mode:
  echo        %PYCMD% -m pip install -r utils\requirements.txt
  echo        %PYCMD% -m playwright install chromium
  echo      The core program needs no third-party packages at all.
  echo   Diagnostics: %PYCMD% utils\run.py doctor
  pause
)
endlocal
