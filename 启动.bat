@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo   Talk to a Friend - Demo
echo ============================================
echo.
echo Two windows will open:
echo   1. web service  (keep open)
echo   2. cpolar tunnel - public URL is the "Tunnel established" line
echo.
echo Keep BOTH windows open.
echo.
start "web-service" cmd /k "python app\web.py"
start "cpolar-tunnel" /D "C:\Program Files\cpolar" cmd /k "cpolar.exe http -log stdout -log-level INFO 8100"
echo.
echo Local:  http://localhost:8100
pause
