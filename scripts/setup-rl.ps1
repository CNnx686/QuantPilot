$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$runtimePath = Join-Path $projectRoot 'runtime/rl-venv'
$pythonPath = Join-Path $runtimePath 'Scripts/python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    & (Join-Path $projectRoot '.venv/Scripts/python.exe') -m venv $runtimePath
    if ($LASTEXITCODE -ne 0) { throw 'Could not create isolated RL environment' }
& $pythonPath -c "import importlib.util; raise SystemExit(0 if importlib.util.find_spec('pip') else 1)"
if ($LASTEXITCODE -ne 0) {
    & $pythonPath -m ensurepip --upgrade
    if ($LASTEXITCODE -ne 0) { throw 'Could not bootstrap pip in isolated RL environment' }
}
}
& $pythonPath -m pip install --extra-index-url https://download.pytorch.org/whl/cpu -r (Join-Path $projectRoot 'requirements-rl.lock')
if ($LASTEXITCODE -ne 0) { throw 'RL dependency installation failed' }
Push-Location $projectRoot
try {
    & (Join-Path $projectRoot '.venv/Scripts/python.exe') -c "import json; from quantpilot.rl_api import readiness; r=readiness(); print(json.dumps(r, indent=2)); raise SystemExit(0 if r['available'] else 1)"
    if ($LASTEXITCODE -ne 0) { throw 'RL compatibility or dataset check failed' }
} finally { Pop-Location }
