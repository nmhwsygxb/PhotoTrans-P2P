@echo off
cd /d "%~dp0"
echo ================================================
echo   PhotoTrans-P2P - PUBLIC mode
echo   URL stays the same while this window is open.
echo   Close this window to stop everything.
echo ================================================
echo.

set PYCMD=
where python >nul 2>nul && set PYCMD=python
if not defined PYCMD ( where py >nul 2>nul && set PYCMD=py )
if defined PYCMD goto havepy
echo [ERROR] Python not found. Install Python 3, check Add to PATH.
echo Download: https://www.python.org/downloads/
pause
exit /b

:havepy
if exist "%~dp0cloudflared.exe" goto havetun
echo [ERROR] cloudflared.exe not found in this folder.
echo Download and rename it: cloudflared-windows-amd64.exe -^> cloudflared.exe
echo   https://github.com/cloudflare/cloudflared/releases/latest
pause
exit /b

:havetun
:restart
netstat -an 2>nul | findstr /r ":8082.*LISTENING" >nul
if not errorlevel 1 goto tunnel
echo [1/3] Starting local server, hidden...
powershell -NoProfile -Command "Start-Process -WindowStyle Hidden -FilePath '%PYCMD%' -ArgumentList 'server.py' -WorkingDirectory '%~dp0'"
:wait8082
netstat -an 2>nul | findstr /r ":8082.*LISTENING" >nul
if errorlevel 1 goto wait1
goto tunnel
:wait1
timeout /t 1 >nul
goto wait8082

:tunnel
echo [2/3] Server up. Starting Cloudflare tunnel, same URL while running...
"%~dp0cloudflared.exe" tunnel --no-autoupdate --retries 100 --url http://localhost:8082
echo [tunnel] exited, restarting in 3 seconds...
timeout /t 3 >nul
goto restart
