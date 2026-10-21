@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo QuantPilot environment is missing. Run setup.ps1 first. See README.md.
  if not "%~1"=="--json" pause
  exit /b 1
)
".venv\Scripts\python.exe" -m quantpilot.doctor %*
set "QUANTPILOT_CHECK_EXIT=%ERRORLEVEL%"
if not "%~1"=="--json" pause
exit /b %QUANTPILOT_CHECK_EXIT%
