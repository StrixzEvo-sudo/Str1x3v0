$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptDir

$config = Get-Content "$scriptDir\release_config.json" | ConvertFrom-Json
if ($config.github_owner -like "your-*" -or $config.github_repo -like "your-*") {
    Write-Warning "release_config.json still contains placeholder values. Auto-update will stay disabled until you replace them."
}

python -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --uac-admin `
    --add-data "release_config.json;." `
    --name SystemCleanupUtility `
    app.py

Write-Host ""
Write-Host "Built: $scriptDir\dist\SystemCleanupUtility.exe"
