@echo off
rem Double-click this to start Slalom Timing. It just runs start.ps1, which
rem installs anything missing (uv, Python 3.14, the dependencies) and then
rem starts the server. Arguments are passed straight through, e.g.
rem   start.bat -Port 8080 -Bind 0.0.0.0
powershell -ExecutionPolicy Bypass -NoProfile -File "%~dp0start.ps1" %*
if errorlevel 1 (
    echo.
    echo The server stopped with an error. The message above says why.
    pause
)
