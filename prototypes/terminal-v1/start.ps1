param([switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
$previewUrl = 'http://127.0.0.1:8770/'
foreach ($requiredFile in @('index.html', 'app.js', 'styles.css')) {
    if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot $requiredFile))) { throw "Prototype file is missing: $requiredFile" }
}
$listener = Get-NetTCPConnection -LocalPort 8770 -State Listen -ErrorAction SilentlyContinue
if ($listener) {
    $owner = Get-CimInstance Win32_Process -Filter "ProcessId=$($listener[0].OwningProcess)"
    if (-not $owner.CommandLine -or -not $owner.CommandLine.Contains($PSScriptRoot) -or -not $owner.CommandLine.Contains('http.server')) {
        throw 'Port 8770 is used by another service. No process was stopped.'
    }
} else {
    if (-not (Test-Path -LiteralPath $pythonPath)) { throw 'Project Python environment is missing. Open index.html directly instead.' }
    $runtimePath = Join-Path $projectRoot 'runtime'
    New-Item -ItemType Directory -Path $runtimePath -Force | Out-Null
    $arguments = @('-m','http.server','8770','--bind','127.0.0.1','--directory',('"' + $PSScriptRoot + '"'))
    Start-Process -FilePath $pythonPath -ArgumentList $arguments -WindowStyle Hidden -WorkingDirectory $PSScriptRoot -RedirectStandardOutput (Join-Path $runtimePath 'terminal-prototype.stdout.log') -RedirectStandardError (Join-Path $runtimePath 'terminal-prototype.stderr.log') | Out-Null
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        Start-Sleep -Milliseconds 250
        if (Get-NetTCPConnection -LocalPort 8770 -State Listen -ErrorAction SilentlyContinue) { break }
    }
    if (-not (Get-NetTCPConnection -LocalPort 8770 -State Listen -ErrorAction SilentlyContinue)) { throw 'Prototype server did not start. Check runtime/terminal-prototype.stderr.log.' }
}
Write-Output "Prototype ready: $previewUrl (demo data only)"
if (-not $NoBrowser) { Start-Process $previewUrl }
