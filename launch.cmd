@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo QuantPilot environment is missing. Run setup.ps1 first. See README.md.
  if not "%~1"=="--no-browser" pause
  exit /b 1
)
powershell.exe -NoProfile -File "%~dp0start.ps1"
if errorlevel 1 (
  echo QuantPilot failed to start. Run doctor.cmd and inspect runtime logs.
  if not "%~1"=="--no-browser" pause
  exit /b 1
)
".venv\Scripts\python.exe" -c "import json,urllib.request; o=urllib.request.build_opener(urllib.request.ProxyHandler({})); r=json.load(o.open('http://127.0.0.1:8765/api/health',timeout=5)); raise SystemExit(0 if r.get('status')=='ok' and r.get('worker',{}).get('alive') else 1)"
if errorlevel 1 (
  echo Local service health check failed. Run stop.ps1 then try launch.cmd again.
  if not "%~1"=="--no-browser" pause
  exit /b 1
)
if not "%~1"=="--no-browser" start "" "http://127.0.0.1:8765/"
exit /b 0
