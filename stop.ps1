$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
& "$PSScriptRoot\.venv\Scripts\python.exe" -m quantpilot.service stop
if ($LASTEXITCODE -ne 0) { throw 'QuantPilot shutdown failed.' }
