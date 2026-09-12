@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Windows\Install-And-Start.ps1"
if errorlevel 1 pause
