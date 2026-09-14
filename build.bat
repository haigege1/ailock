@echo off
REM Build single-file exe: dist\AiLock.exe  (Qt / PySide6 edition)
REM NOTE: keep this file ASCII-only. cmd.exe parses .bat with the ANSI
REM codepage (GBK on Chinese Windows); UTF-8 Chinese text breaks parsing.
setlocal
set "HERE=%~dp0"
set "VENV=%HERE%..\.venv\Scripts\python.exe"
if not exist "%VENV%" (
    echo [ERROR] Python venv not found: %VENV%
    echo Create it in the project root:
    echo     python -m venv .venv
    echo     .venv\Scripts\python.exe -m pip install pillow pywin32 pyside6
    pause
    exit /b 1
)

echo [1/3] Regenerating icon and default wallpaper...
"%VENV%" "%HERE%tools\make_assets.py" || goto :err

echo [2/3] Installing PyInstaller + PySide6...
"%VENV%" -m pip install --quiet --disable-pip-version-check pyinstaller pyside6 || goto :err

echo [3/3] Building from AiLock.spec (first run can be slow)...
cd /d "%HERE%"
"%VENV%" -m PyInstaller --noconfirm "%HERE%AiLock.spec" || goto :err

echo.
echo ============================================================
echo  Build OK: %HERE%dist\AiLock.exe
echo  Usage:
echo    AiLock.exe                     background tray app
echo    AiLock.exe --lock              lock now
echo    AiLock.exe --settings          open settings
echo    AiLock.exe --notify --state done
echo    AiLock.exe --run -t "task" -- python train.py
echo    AiLock.exe --selftest 5
echo  See README.md for AI-tool integration (notify / --run).
echo ============================================================
endlocal
exit /b 0

:err
echo Build FAILED.
endlocal
exit /b 1
