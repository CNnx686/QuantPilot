$ErrorActionPreference = 'Stop'
$listeners = Get-NetTCPConnection -LocalPort 8770 -State Listen -ErrorAction SilentlyContinue
foreach ($listener in $listeners) {
    $owner = Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)"
    if (-not $owner.CommandLine -or -not $owner.CommandLine.Contains($PSScriptRoot) -or -not $owner.CommandLine.Contains('http.server')) {
        throw 'Port 8770 belongs to another service. No process was stopped.'
    }
    Stop-Process -Id $listener.OwningProcess
}
Write-Output 'Prototype preview stopped. The main application is unchanged.'
