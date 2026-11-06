# Build portabile di GDownloader (un solo file .exe).
# Uso: powershell -ExecutionPolicy Bypass -File .\build_portable.ps1
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

python -m py_compile downloader.py
if ($LASTEXITCODE -ne 0) { throw "py_compile fallito" }

python -m PyInstaller --noconfirm --clean --onefile --windowed `
    --name DOWNLOADVERSE `
    --icon assets\downloadverse.ico `
    --version-file version_info.txt `
    --add-data "assets\downloadverse.png;." `
    --add-data "assets\downloadverse.ico;." `
    --collect-all libtorrent `
    --collect-all curl_cffi `
    --collect-all pystray `
    --collect-all PIL `
    --hidden-import Crypto.Cipher.AES `
    --hidden-import certifi `
    --hidden-import fluid_scrollbar `
    --hidden-import ui_render `
    downloader.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller fallito" }

Write-Host ""
Write-Host "Build completata: $PSScriptRoot\dist\DOWNLOADVERSE.exe" -ForegroundColor Green
