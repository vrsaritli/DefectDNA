# Stop everything .\scripts\start.ps1 started (process trees, using logs\pids.json).
$root = Split-Path -Parent $PSScriptRoot
$file = Join-Path $root "logs\pids.json"
if (-not (Test-Path $file)) { Write-Host "Nothing to stop (no logs\pids.json)."; return }
$pids = Get-Content $file -Raw | ConvertFrom-Json
foreach ($entry in $pids.PSObject.Properties) {
    & taskkill /PID $entry.Value /T /F 2>$null | Out-Null
    Write-Host "stopped $($entry.Name) (pid $($entry.Value))"
}
Remove-Item $file
