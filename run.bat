@echo off
REM Launcher for Windows. Put this next to morse.py and double-click it,
REM or run it from a Command Prompt in this folder.
cd /d "%~dp0"

where uv >nul 2>nul
if %errorlevel%==0 goto run

echo uv not found - installing it (one-time, no admin needed)...
powershell -ExecutionPolicy Bypass -c "irm https://astral.sh/uv/install.ps1 | iex"
set "PATH=%USERPROFILE%\.local\bin;%PATH%"

where uv >nul 2>nul
if %errorlevel%==0 goto run

echo.
echo Could not install uv automatically. Fallback:
echo   python -m venv .venv ^&^& .venv\Scripts\activate ^&^& pip install textual aiortc aiohttp ^&^& python morse.py
pause
exit /b 1

:run
uv run morse.py
pause
