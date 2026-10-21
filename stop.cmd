@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" -m quantpilot.service stop
if errorlevel 1 (
  echo Stop failed. Review the message above before closing this window.
  pause
  exit /b 1
)
