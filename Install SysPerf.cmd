@echo off
rem Opens the SysPerf setup wizard (no administrator rights needed).
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-wizard.ps1"
if errorlevel 1 pause
