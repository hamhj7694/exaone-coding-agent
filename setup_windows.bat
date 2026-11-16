@echo off
setlocal
cd /d "%~dp0"
echo [EXAONE Coding Agent] Preparing Python environment...
if exist ".venv\Scripts\python.exe" goto install
where py >nul 2>nul
if not errorlevel 1 (
    py -3 -m venv .venv
) else (
    python -m venv .venv
)
if errorlevel 1 goto failed
:install
".venv\Scripts\python.exe" -c "import sys, tkinter; assert sys.version_info >= (3, 10), 'Python 3.10+ required'"
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo Desktop automation dependency installation failed.
    echo Chat, file editing and terminal features still work.
    echo Re-run this file later to install desktop support.
)
echo Ready. Launching the application...
".venv\Scripts\python.exe" main.py
if errorlevel 1 goto failed
exit /b 0
:failed
echo Setup failed. Install Python 3.10+ with Tcl/Tk support and try again.
echo Recommended: Python 3.12 64-bit from https://www.python.org/downloads/windows/
pause
exit /b 1
