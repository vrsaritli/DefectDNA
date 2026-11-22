# Apply all migrations (001-006) to the database in .env. Safe to re-run: every
# migration only creates indexes / starter data and records itself in schema_migrations.
#   .\scripts\migrate.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
$env:PYTHONPATH = $root
Push-Location $root
try {
    Get-ChildItem (Join-Path $root "migrations") -Filter "0*.py" | Sort-Object Name | ForEach-Object {
        Write-Host "== $($_.Name)"
        & $python $_.FullName
        if ($LASTEXITCODE -ne 0) { throw "Migration $($_.Name) failed" }
    }
}
finally {
    Pop-Location
}
