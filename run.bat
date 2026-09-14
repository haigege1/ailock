@echo off
REM One-click launcher for AiLock (run from source).
REM NOTE: keep this file ASCII-only. cmd.exe parses .bat with the ANSI
REM codepage (GBK on Chinese Windows); UTF-8 Chinese text breaks parsing.
setlocal
set "HERE=%~dp0"
set "VENV=%HERE%..\.venv\Scripts\python.exe"
if not exist "%VENV%" (
    echo.
    echo   [ERROR] Python venv not found: %VENV%
    echo   Create it first in the project root:
    echo     python -m venv .venv
    echo     .venv\Scripts\python.exe -m pip install pillow pywin32
    echo.
    pause
    exit /b 1
)
cd /d "%HERE%"
"%VENV%" main.py %*
endlocal
