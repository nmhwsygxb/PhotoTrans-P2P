@echo off
cd /d "%~dp0"

echo ================================================
echo   PhotoTrans-P2P - LAN mode
echo   Open the "LAN" URL below on your phone
echo   (phone must be on the same WiFi)
echo ================================================
echo.

where python >nul 2>nul
if %errorlevel%==0 (
    python server.py
    goto end
)
where py >nul 2>nul
if %errorlevel%==0 (
    py server.py
    goto end
)

echo [ERROR] Python not found. Install Python 3 and check "Add to PATH".
echo Download: https://www.python.org/downloads/

:end
pause
