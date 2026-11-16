@echo off
setlocal
set "EXAONE_INSTALL_TARGET=%USERPROFILE%\Documents\git\exaone-coding-agent"
set "EXAONE_SOURCE_DIR=%~dp0"
if /I "%EXAONE_SOURCE_DIR%"=="%EXAONE_INSTALL_TARGET%\" goto launch
if exist "%EXAONE_INSTALL_TARGET%\main.py" (
    echo Target already contains an application:
    echo %EXAONE_INSTALL_TARGET%
    echo Nothing was overwritten. Use run_windows.bat in that folder.
    pause
    exit /b 1
)
if not exist "%EXAONE_INSTALL_TARGET%" mkdir "%EXAONE_INSTALL_TARGET%"
if errorlevel 1 goto failed
robocopy "%~dp0." "%EXAONE_INSTALL_TARGET%" /E /XD .venv __pycache__ .git /XF *.pyc
if errorlevel 8 goto failed
:launch
cd /d "%EXAONE_INSTALL_TARGET%"
call setup_windows.bat
exit /b
:failed
echo Could not create/copy the application to the requested folder.
pause
exit /b 1
