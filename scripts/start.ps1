# Start the whole DefectDNA stack on this machine (no containers):
#   4 API services, 2 workers and the frontend dev server.
#   .\scripts\start.ps1            -> http://127.0.0.1:5173
#   .\scripts\stop.ps1             -> stops everything started here
# Logs go to .\logs\<name>.log; process IDs to .\logs\pids.json.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
$logs = Join-Path $root "logs"
New-Item -ItemType Directory -Force $logs | Out-Null
if (Test-Path (Join-Path $logs "pids.json")) {
    throw "logs\pids.json exists: the stack may already be running. Run .\scripts\stop.ps1 first."
}

$env:PYTHONPATH = $root
$env:PYTHONUNBUFFERED = "1"
$services = [ordered]@{
    "api-gateway"          = @("-m", "uvicorn", "services.api_gateway.app.main:app", "--host", "127.0.0.1", "--port", "8000")
    "ingestion-service"    = @("-m", "uvicorn", "services.ingestion_service.app.main:app", "--host", "127.0.0.1", "--port", "8001")
    "ai-knowledge-service" = @("-m", "uvicorn", "services.ai_knowledge_service.app.main:app", "--host", "127.0.0.1", "--port", "8002")
    "trends-service"       = @("-m", "uvicorn", "services.trends_service.app.main:app", "--host", "127.0.0.1", "--port", "8003")
    "ai-knowledge-worker"  = @("-u", "-m", "services.ai_knowledge_service.app.worker")
    "trends-worker"        = @("-u", "-m", "services.trends_service.app.worker")
}

$pids = [ordered]@{}
foreach ($name in $services.Keys) {
    $process = Start-Process -FilePath $python -ArgumentList $services[$name] -WorkingDirectory $root `
        -RedirectStandardOutput (Join-Path $logs "$name.log") -RedirectStandardError (Join-Path $logs "$name.err.log") `
        -WindowStyle Hidden -PassThru
    $pids[$name] = $process.Id
    Write-Host "started $name (pid $($process.Id))"
}
$frontend = Start-Process -FilePath "npm.cmd" -ArgumentList @("run", "dev", "--", "--host", "127.0.0.1") `
    -WorkingDirectory (Join-Path $root "frontend") -RedirectStandardOutput (Join-Path $logs "frontend.log") `
    -RedirectStandardError (Join-Path $logs "frontend.err.log") -WindowStyle Hidden -PassThru
$pids["frontend"] = $frontend.Id
$pids | ConvertTo-Json | Set-Content (Join-Path $logs "pids.json")

foreach ($port in 8000, 8001, 8002, 8003) {
    $ok = $false
    for ($i = 0; $i -lt 30 -and -not $ok; $i++) {
        try { Invoke-RestMethod "http://127.0.0.1:$port/health/ready" -TimeoutSec 2 | Out-Null; $ok = $true }
        catch { Start-Sleep -Milliseconds 700 }
    }
    Write-Host ("port {0}: {1}" -f $port, $(if ($ok) { "ready" } else { "NOT READY - see logs" }))
}
Write-Host "Open http://127.0.0.1:5173"
