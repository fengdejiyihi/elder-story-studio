@echo off
powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "%~dp0stop_service.ps1"
exit /b %errorlevel%
