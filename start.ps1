$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
& "$PSScriptRoot\.venv\Scripts\python.exe" -m quantpilot.service start
if ($LASTEXITCODE -ne 0) { throw 'QuantPilot failed to start; see runtime logs.' }
