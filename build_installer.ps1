# Build del setup MSI di DOWNLOADVERSE.
# Uso: powershell -ExecutionPolicy Bypass -File .\build_installer.ps1
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$versionMatch = Select-String -Path ".\downloader.py" -Pattern '^APP_VERSION = "([^"]+)"' |
    Select-Object -First 1
if (-not $versionMatch) { throw "APP_VERSION non trovata in downloader.py" }
$version = $versionMatch.Matches[0].Groups[1].Value

if (-not (Test-Path ".\dist\DOWNLOADVERSE.exe")) {
    throw "Eseguibile non trovato. Esegui prima build_portable.ps1."
}

wix build installer.wxs -arch x64 `
    -d Version=$version `
    -o "dist\DOWNLOADVERSE-$version.msi"
if ($LASTEXITCODE -ne 0) { throw "Build MSI fallita" }

Write-Host ""
Write-Host "Setup MSI completato: $PSScriptRoot\dist\DOWNLOADVERSE-$version.msi" -ForegroundColor Green
