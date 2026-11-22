# Pre-release checks, run locally before tagging a release (replaces a CI server):
# lint, types, backend tests, dependency scans, frontend build and Playwright E2E.
#   .\scripts\check.ps1              # everything
#   .\scripts\check.ps1 -SkipE2E     # faster
param([switch]$SkipE2E)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$venv = Join-Path $root ".venv\Scripts"
$failed = @()

function Step([string]$name, [scriptblock]$block) {
    Write-Host "`n== $name"
    & $block
    if ($LASTEXITCODE -ne 0) { $script:failed += $name }
}

Push-Location $root
try {
    Step "ruff" { & "$venv\ruff.exe" check libs services migrations ops }
    Step "mypy" { & "$venv\mypy.exe" libs services }
    Step "pytest" { & "$venv\python.exe" -m pytest -q -p no:cacheprovider }
    Step "pip-audit" { & "$venv\pip-audit.exe" --progress-spinner off }
    Push-Location frontend
    try {
        Step "npm audit" { npm audit --audit-level=high }
        Step "frontend build" { npm run build }
        if (-not $SkipE2E) { Step "playwright e2e" { npm run e2e } }
    }
    finally { Pop-Location }
}
finally { Pop-Location }

if ($failed.Count) { Write-Host "`nFAILED: $($failed -join ', ')" -ForegroundColor Red; exit 1 }
Write-Host "`nAll checks passed." -ForegroundColor Green
